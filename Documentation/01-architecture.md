# 1. Architecture

## What problem this solves

A freight-forwarding desk gets a few hundred emails a day. Buried in them are
two things that matter:

1. **Customer requirements** — someone asking "what would it cost to ship X
   from A to B?"
2. **Rate cards** — a carrier or agent replying with prices.

Everything else — bills of lading, arrival notices, invoices, "noted thanks" —
is noise. The desk's job is to spot (1), ask several freight agents for a
price, collect (2), compare, and pick a winner. This system does the reading,
the drafting, and the filing. A human still decides.

## The three flows

The system is not one pipeline. It is three loops that meet in the database.

```
                    ┌───────────────────────────────────────────┐
   Gmail  ────────► │  A. INGEST — every 5 min + on startup      │
                    │  gmail_connector → classify → emails table │
                    └───────────────────┬───────────────────────┘
                                        │
                     ┌──────────────────┴───────────────────┐
                     ▼                                      ▼
    ┌────────────────────────────────┐   ┌──────────────────────────────────┐
    │  B. SCAN — every 5 min         │   │  C. HUMAN RFQ — operator-driven  │
    │  unprocessed emails:           │   │  dashboard → Send Request:       │
    │   • rate card → link to the    │   │   extract → draft → EDIT → send  │
    │     RFQ it answers             │   │   one RFQ reference per agent    │
    │   • customer req → count only  │   │   → rfq_jobs rows                │
    │   • other → count only         │   └──────────────────────────────────┘
    └────────────────────────────────┘
```

### A. Ingest — `connectors/email_store.py`

Pulls mail from Gmail into the `emails` table. Two properties matter:

**Idempotent.** Keyed on `provider_msg_id`. Re-running never duplicates, so
each email is classified exactly once, ever.

**Gap-proof.** It does not trust a cursor. Each run sweeps *every* page of
message ids newer than the watermark and diffs them against what's already
stored. This exists because a naive "stop at the first page I recognise"
approach once silently lost ten days of mail — a gap can hide *below*
already-ingested mail when messages arrive out of order. The watermark is held
a day behind the newest mail and is only a performance bound, never the
correctness mechanism. A daily audit job compares Gmail's per-day counts to the
database and logs any day where they disagree.

**Bounded initial load (intentional).** One run ingests at most
`MAX_INGEST_BATCH` (5000) new emails, keeping the **newest** 5000 and stopping.
On a very large backlog — weeks of downtime, or a brand-new account whose whole
mailbox is "new" — this is a deliberate cap: pulling an entire mailbox history is
load nobody asked for. The trade-off is equally deliberate — after a capped
(`truncated`) run the watermark **still advances** past the older, un-ingested
window, so incremental ingest will **not** backfill it. That mail was never
pulled (it is not lost), and pulling it is the job of the manual scripts
(`backfill_3months.py` / `ingest_window.py`), which take an explicit date window.
Both the cap and the watermark advance log a `WARNING` when they trigger, so the
case is visible in the logs. This is by design — see the note in
[BUGS.md](BUGS.md#by-design); do not "fix" it by removing the ceiling or holding
the watermark.

**Bounded sweep (`MAX_SWEEP_IDS`, 20k).** A second, separate ceiling, because
`MAX_INGEST_BATCH` counts a different thing: mail we *lack*. A window full of mail
we already have never trips it — `unknown_ids` stays at 0 while the pager walks to
the end of the window. So a watermark that reads back fine but sits far in the past
(seeded long ago, edited by hand, a timezone slip) paged all ~195k ids, roughly 390
sequential Gmail requests, every five minutes. The sweep is therefore capped on ids
*seen* as well as ids *new*. Unlike the bounded load this does **not** suppress the
watermark advance: with 0 unknown ids there is no `newest_seen` to advance to, so
the `_max_received_at()` promotion is the only thing that lets a lagging watermark
move, and suppressing it would re-page the cap every five minutes forever — a
permanent stall, strictly worse than one expensive self-healing run. The un-swept
region below the cap is older mail, and the daily gap audit already widens its
window by how far the watermark lags, so that region has an owner.

**Bounded self-heal (`AUTO_HEAL_MAX_TOTAL_MISSING`, 2000).** The gap heal has three
guardrails, not two, because the original pair multiplied: "at most 14 gap days"
and "at most 1000 missing per day" were checked independently, so 14 × 1000 = 14,000
mails — each one a Gmail full-fetch plus a real LLM classification — cleared both
while nothing ever looked at the product. The total is enforced twice: once up front
against the audit's estimate, then again as a running budget charged for mail
actually pulled. Both are needed because the two numbers measure different things —
the audit's `missing` is a count subtraction (Gmail's count for the day minus DB
rows dated that day) while the backfill performs a set difference over
`provider_msg_id`. Rows the DB holds for a day that Gmail no longer lists in INBOX
(archived, deleted, moved) inflate the DB count and shrink `missing` without
affecting the set difference, so a day reporting 50 missing can hand back 950
unknown ids. `backfill_window` takes a `max_new` cap for exactly that reason: it
bounds the pull at the point where the real quantity is finally known. Days left
unattempted when the budget runs out are returned as `deferred` and retried on the
next nightly run.

**Attachments are tiered, not queued wholesale.** Ingest writes attachment
metadata only; a worker downloads the bytes every two minutes, 150 at a time, and
stands down whenever an ingest holds the lock. That budget was being spent almost
entirely on decoration: of 36,205 queued rows, 25,484 were images under 20 kB —
signature logos, icons, spacers, tracking pixels — 70% of the queue for 3% of its
bytes, and because the queue drained strictly oldest-first, every one of them made
a vendor's rate-card PDF wait behind it. Roughly a day's delay on the document the
desk was actually waiting for.

Gmail's own part headers settle which is which, and they arrive in the
`format=full` response ingest already fetches, so the test is free:

| tier | test | treatment |
|---|---|---|
| 1 | no `Content-ID` (`Content-Disposition: attachment`) | queued, drained **first** |
| 2 | `Content-ID` present, ≥ 20 kB | queued, drained after tier 1 |
| 3 | `Content-ID` present, < 20 kB | recorded as `skipped`, never downloaded |

Both halves of the tier-3 test are load-bearing. Size alone is wrong: the queue
holds genuine 1.8 kB payment PDFs and 300-byte CSVs. Embedded-ness alone is wrong
too: an agent pasting a rate table into the message body produces an embedded
image that, by disposition, is indistinguishable from a logo — and in this trade
that screenshot *is* the quotation, so the 20 kB floor is what separates them. A
part whose size Gmail does not report is kept; guessing in the discard direction is
the one guess that cannot be recovered from.

Tier 3 is **recorded, not dropped** — the row still appears in the email's
attachment list, keeping the mail's true contents honest, and the whole decision
reverses with one `UPDATE` because no bytes were ever fetched. The 25,316 rows
already queued when this landed were retired the same way by
`scripts/retire_inline_attachment_backlog.py` (matched on `image/*` under 20 kB,
since the header was not captured at the time they were enqueued), taking the
queue from ~36k to ~10.5k. That script is dry-run by default and carries the
reversal SQL in its docstring.

### B. Scan — `automation/automation.py`

Every 5 minutes, claims unprocessed rows from `emails` and acts on the label.

The claim is atomic (`UPDATE ... WHERE processed_at IS NULL`), so two workers
can never both process the same email. If the work then throws, the claim is
**handed back** and `processing_attempts` increments — up to 3, after which the
row is left alone with `processing_error` set. (Before that, one exception
retired an email permanently.)

Only **one** branch does anything: `quotation_rate_card`, which links the reply
to the RFQ it answers — see below. The `customer_requirement` branch counts the
email and stops. That is intentional.

A run reports why it produced its numbers via `ScanStats.status`
(`completed` / `already_running` / `disabled` / `error`), and only `completed`
runs are saved as the last result.

### C. Human RFQ — the dashboard

The only path by which an email leaves this system.

```
dashboard → click a customer request
  → POST /extract-details    intake_agent reads the email, prefills the form
  → POST /preview-rfq        rfq_agent drafts ONE sample email
  → operator edits it
  → POST /send-rfq           every selected agent gets THAT text, each with its
                             own RFQ-YYYYMMDD-xxxx reference in the subject
  → one rfq_jobs row per agent
```

## The human gate

`automation.py` used to auto-generate and auto-send RFQs the moment it saw a
customer requirement. That is switched off, and the function that did it has
been deleted.

**Why it matters:** these emails go to real vendors under the company's name.
An LLM misreading a "please quote" in a quoted signature block and blasting 40
carriers is not a recoverable mistake. The cost of a missed RFQ is an operator
noticing it late; the cost of a wrong RFQ is a damaged vendor relationship.

If you are asked to "automate the last step", push back or make it opt-in per
customer. Do not quietly re-enable it.

## Rate-card attribution

When an agent replies with prices, the system does exactly one thing: work out
*which RFQ the reply answers*, and record that link. It matches on the **RFQ
reference**, which exists in two forms:

| | | |
|---|---|---|
| canonical | `RFQ-20260101-a1b2` | what `rfq_jobs.reference` stores |
| subject | `RFQId:20260101-a1b2` | what goes out in the subject line |

The subject form is labelled so it survives a human retyping it and is obvious
to an agent skimming their inbox. Matching accepts both — plus case and spacing
variants — because ~150 RFQs went out in the bare form before the token existed.
Extraction always returns the canonical form.

**Every outgoing subject is passed through `inject_reference()`.** The RFQ
drafting prompt also asks for the token, but asking is not enough: when the model
deviated, the RFQ left unmatchable and its reply could never be attributed to a
shipment. Injection replaces whatever the model produced, so exactly one
reference is present and it is the right one.

```
reply arrives
  → classifier: subject carries one of our references?  →  quotation_rate_card
                (certain, no model call — we issued the token)
  → scan: resolve the reference to a job
      found     → emails.rfq_reference = ref, job → quotes_received
      otherwise → left unlinked, visible in the inbox and the Needs Linking tab
```

That's the whole thing. **Rates are not extracted and prices are not
predicted.** The operator opens the reply and reads it — body and attachments,
as the agent wrote it.

**Why it works this way.** The system used to run the reply through an LLM to
pull out structured rates, then estimate a fair price and grade the quote
against it. Three things went wrong with that, all of them quiet:

- The extraction schema made `transit_time_days` a required integer and the
  prompt said "use reasonable defaults" — so a reply that never mentioned
  transit got an invented number, stored and displayed as if quoted.
- The price verdict compared a bare number against a USD range while the
  parsed currency sat unused next to it, so an INR quote always read "above
  expected".
- One reply quoting three container sizes became three rows, and everything
  downstream that treated a row as an agent double-counted.

A freight desk reads rate cards for a living. Handing them the agent's own
email is both cheaper and more trustworthy than handing them a summary that
might be fabricated.

Matching is case-insensitive and the reference is normalised to its stored form,
because agents retype it by hand.

**Consequence you must plan for:** an agent who drops the reference is not
linked to their job. Nothing is lost — the email is in the inbox, labelled
💰 Rate Card — but it won't appear on the request page.

Those replies are collected in the dashboard's **Needs Linking** tab
(`GET /rate-cards/unlinked`), which is simply the query
`classification = 'quotation_rate_card' AND rfq_reference IS NULL`. Each row
carries a reason worked out at read time, because the two cases want different
responses:

| Reason | What it means |
|---|---|
| *No RFQ reference in the reply* | Routine. The agent replied fresh or stripped the subject. |
| *Cites `RFQ-…`, which matches no job* | Worth a look — a typo, a deleted job, or a reference from somewhere that isn't this system. |

The tab currently lists; it does not yet let you assign a reply to a job. That
is the obvious next step.

## Classification

`classifier/email_classifier.py`. Three cheap rules short-circuit; anything
left goes to one LLM call.

| Step | Rule | Result |
|---|---|---|
| 1 | Sender is an internal domain | `general`, no API call |
| 2 | **Subject carries an RFQId we issued** | `quotation_rate_card`, no API call |
| 3 | Job-reference subject with no rate/quote keyword | `general`, no API call |
| 4 | Rate-card subject + "please find attached" | `quotation_rate_card`, no API call |
| 5 | One LLM call via `llm_provider` | the label |

Labels: `customer_requirement`, `quotation_rate_card`, `general`.

Rule 2 is the strongest signal in the system: an external sender quoting back a
token *we generated* is, by construction, an agent answering an RFQ we sent. No
model can know that better than we do, so it returns confidence 1.0 and skips the
call entirely. It matches the **subject only** — the reference appears in the
quoted original of every later message in a thread, so matching the body would
relabel operational follow-ups months afterwards.

Two design points worth understanding:

**Failure is not `general`.** When the LLM call fails (quota, outage), the row
is marked `classification_status='pending'`, not silently labelled `general`.
An unclassified customer enquiry that *looks* like a confident "general" is an
RFQ that never gets sent and nobody notices. A 15-minute retry job drains the
pending queue when the provider recovers, and the UI shows "⏳ Pending".

**The thread rule.** Once a thread has produced a `customer_requirement`, every
later email in it is forced to `general`. Without this, every "kind reminder"
in a chain spawns another RFQ job and another Send button.

## Model usage

Three calls, total. Everything else is plain Python.

| Where | Model | Notes |
|---|---|---|
| Classification | `gpt-4o-mini` via `llm_provider` | Once per email, cached. Swap with `LLM_PROVIDER=openai\|gemini` |
| Intake extraction | `gpt-4o-mini` | Structured output into `ShipmentDetails`, on demand from the Send Request form |
| RFQ drafting | **`gpt-4o`** | The only place the expensive model is used |

Only classification goes through the `llm_provider` abstraction; the other two
call the OpenAI SDK directly with a hard-coded model. That inconsistency is
known, not intentional.

Note what is **not** on this list: nothing reads an agent's reply with a model,
and nothing estimates a price. Rate-card handling is a regex for the reference
plus one `UPDATE`.
