# 3. Data model

Everything lives in Supabase (Postgres). There is no local database and no
ORM — every call is a `supabase.table(...)` query.

## Tables

| Table | Holds | Created by |
|---|---|---|
| `emails` | Every ingested email: headers, body, label, processing state, and the RFQ it answers | `setup_email_store.sql` + `link_replies_to_jobs.sql` |
| `attachments` | Attachment metadata; the bytes live in Storage | `setup_email_store.sql` |
| `email_classifications` | The label cache, one row per email | `setup_classification_cache.sql` |
| `classification_feedback` | Human corrections, as an accuracy log | `setup_classification_feedback.sql` |
| `rfq_jobs` | One row per **agent per request** — the RFQ you sent | `setup_database_v2.sql` + `add_customer_request_link.sql` |
| `agents` | Freight agents, seeded from the CSV | `setup_agents_table.sql` |
| `sync_state` | Ingest watermark, one row per provider | `setup_email_store.sql` |
| `automation_state` | Single row (`id=1`): enabled flag + last run stats | `setup_scan_state.sql` |

**`quotations` still exists but is no longer written to.** It held rates parsed
out of agent replies by an LLM. That whole path was removed — the operator reads
the reply itself — and the historical rows were cleared, because the transit
times in them were partly fabricated and the price verdicts assumed USD
regardless of the quote's currency. The table is kept so the schema history is
intact; treat it as dead.

`setup_database.sql` is the original schema and still defines a `shipments`
table plus pgvector search functions. **Nothing reads them either** — they were
the history agent's corpus. Left in place so you can decide; safe to drop.

## Identity — the part that causes bugs

Three different ids refer to an email. Getting them confused is the single most
common mistake in this codebase.

| Id | Looks like | Where it comes from |
|---|---|---|
| `emails.id` | UUID | Postgres. Internal only. Used by `attachments.email_id`. |
| `provider_msg_id` | `19f3a2b...` | Gmail's message id. **This is the id everything else keys on.** |
| `message_id` | `<abc@mail.example>` | The RFC-2822 header. Can be null. Stored, not used for lookups. |

**Rule: key on `provider_msg_id`.** The label cache, the dashboard, the request
page and `rfq_jobs.customer_email_id` all use it. An earlier version keyed the
cache on `message_id`, which is sometimes null, so the same email ended up
split across two id spaces and corrections applied to one never reached the
other.

`thread_id` is Gmail's thread. It drives the rule that stops follow-ups in a
quote thread from spawning duplicate jobs.

## Life of one email

```
1.  Gmail
      ↓  email_store.ingest_new_emails()  — sweeps ids, diffs against DB
2.  emails row inserted
      classification        = the label, or '' on failure
      classification_status = 'classified' | 'pending'
      processed_at          = NULL          ← not yet seen by the scan
      processing_attempts   = 0
      ↓
3.  email_classifications row cached (label, confidence, method)
      ↓
4.  Scan claims it:  UPDATE emails SET processed_at = now()
                     WHERE id = ? AND processed_at IS NULL
      ↓
5.  Branch on label
      customer_requirement → counted; waits for a human
      quotation_rate_card  → rfq_reference set, if the reply quoted one
      general              → counted, done
      ↓  on exception: claim released, processing_attempts += 1,
                       processing_error set. Retried up to 3 times.
```

Four states worth recognising when debugging:

- `processed_at IS NULL, processing_attempts = 0` — the scan hasn't reached it.
  Backlog.
- `processed_at IS NULL, processing_attempts >= 3` — the scan gave up. Read
  `processing_error`. These are excluded from selection, so they sit still.
- `classification_status = 'pending'` — the LLM call failed. The retry job will
  re-run it. The UI shows "⏳ Pending classification", *not* a label.
- `classification = 'quotation_rate_card', rfq_reference IS NULL` — a rate card
  that didn't quote its reference back. Visible in the inbox, absent from the
  request page.

## How a request hangs together

One customer email fans out into several jobs — **one per agent**, each with
its own reference so replies are attributable.

```
emails.provider_msg_id  "19f3a2b"          ← the customer's enquiry
   │
   │  rfq_jobs.customer_email_id
   ├──────────────► rfq_jobs  reference = RFQ-20260101-a1b2   agents_contacted = ['MSC']
   ├──────────────► rfq_jobs  reference = RFQ-20260101-c3d4   agents_contacted = ['CMA CGM']
   └──────────────► rfq_jobs  reference = RFQ-20260101-e5f6   agents_contacted = ['Maersk']
                                   │
                                   │  emails.rfq_reference
                                   └──► emails  "MSC's reply, body + attachments"
```

`GET /customer-request/{provider_msg_id}` reassembles this whole picture;
`GET /jobs/{reference}/replies` returns just one branch of it.

The link is a plain string column on `emails`, not a join table, because a reply
answers exactly one RFQ. An agent who sends three follow-ups produces three
linked emails, all pointing at the same reference — the request page groups them
under that agent.

## Job status

`rfqs_sent` → `quotes_received` → `approved`

Set by: `/send-rfq` on creation; the scan when it **links a reply**;
`/jobs/{ref}/approve` on approval. `awaiting_quotes` appears in the frontend's
colour map but nothing ever writes it.

A job only moves to `quotes_received` if it was already open, so a late reply
cannot reopen an approved job.

**The name is now slightly wrong.** It means "at least one reply has been linked
to this RFQ", not "we hold parsed quotes" — the evidence used to be rows in
`quotations` and is now a row in `emails` with a matching `rfq_reference`.

**Watch for stale ones.** Jobs flipped to `quotes_received` by the old
parse-and-store code have no linked reply, so the dashboard shows "Quotes
Received" while View Replies is empty. To find them:

```sql
select j.reference, j.status
from rfq_jobs j
where j.status = 'quotes_received'
  and not exists (select 1 from emails e where e.rfq_reference = j.reference);
```

`GET /jobs` returns `reply_count` per job, which is the honest signal — the
dashboard uses it rather than the status for "who has answered".

## Constraints that shape the code

- `emails.rfq_reference` is a plain text column with **no** foreign key to
  `rfq_jobs`. Deliberate: the scan verifies the job exists before writing it, and
  a hard FK would mean a deleted job cascaded into rewriting email rows.
- `agents` has `unique (agent_name, email)` — the schema models multi-office
  agents correctly even though `rfq_jobs.agents_contacted` (names only) throws
  that distinction away.
- Linking is idempotent: re-running the scan over the same reply writes the same
  reference.
- A job only advances to `quotes_received` if it was already open, so a late
  reply cannot reopen an approved job.

## Attachments

Bytes go to a Supabase Storage bucket (`ATTACHMENT_BUCKET`, default
`rate-card-attachments`) keyed by UUID; the table holds metadata and the storage
path. `GET /email-attachments/{id}` returns 5-minute signed URLs.

`processing_status` is plain text with no CHECK constraint, and carries four
values: `pending` (queued, bytes not fetched yet), `stored` (in the bucket),
`failed` (5 attempts exhausted), and `skipped` — an embedded image under 20 kB
that ingest decided is body furniture rather than a document, recorded so the
email's attachment list stays honest but never downloaded. See
[architecture](01-architecture.md#a-ingest--connectorsemail_storepy) for the tier
rules. `skipped` rows have an empty `storage_path` and no bucket object, so
`GET /email-attachments/{id}` excludes them: an empty path already meant "not
downloaded yet, no URL", and a `skipped` row would have sat in that state forever
as an attachment the operator cannot open.

Nothing parses attachment *contents*, and nothing needs to: the operator opens
the PDF or spreadsheet themselves from the request page.
`scripts/measure_attachment_quotes.py` still measures how often rate cards
arrive as attachment-only, which is useful for sizing the problem even though
there is no longer a text parser to lose them.
