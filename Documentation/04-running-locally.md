# 4. Running locally

## Before anything else

```bash
# in .env
EMAIL_REDIRECT=your.name@example.com
```

Every outgoing email then goes to you instead of a real vendor, subject-prefixed
`[TEST → real@vendor.com]`. `GET /automation/status` will report
`safe_mode: true`. **Do this first.** The Send Request flow talks to real
freight companies.

## Setup

```bash
python -m venv venv && source venv/bin/activate    # Windows: venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env                                # then fill it in
```

All Python commands run **from the repo root**, so `backend` resolves as a
package.

## Run it

```bash
./run_backend.sh                 # API on :8001  (uvicorn backend.app.api:app)
```

```bash
cd frontend && npm install && npm run dev    # http://localhost:3000
```

Go to **http://localhost:3000/dashboard** — not `/`. The root route is the
legacy office view.

Quieter backend, no background jobs:

```bash
RUN_SCHEDULER=0 uvicorn backend.app.api:app --port 8001 --reload
```

## Database setup

Run these in the Supabase SQL editor, in order. All are idempotent.

```
setup_email_store.sql               emails, attachments, sync_state, bucket
setup_classification_cache.sql      email_classifications
setup_classification_feedback.sql   classification_feedback
setup_database_v2.sql               rfq_jobs, quotations
add_customer_request_link.sql       customer_email_id / customer_thread_id
setup_agents_table.sql              agents
setup_scan_state.sql                automation_state
link_replies_to_jobs.sql            emails.rfq_reference + scan retry columns
```

`link_replies_to_jobs.sql` also contains a commented-out `DELETE FROM
quotations;`. Run it once you've taken any backup you want — those rows are
LLM-parsed rates that are no longer produced or displayed.

Then seed the agents:

```bash
python -m backend.scripts.seed_agents_table
```

`setup_database.sql` is the original schema. You do **not** need it — its
`shipments` table and vector functions are unused.

## Gmail access

Two paths. Prefer the first.

**User OAuth** — the mailbox owner consents; no Workspace admin involved.

```bash
python -m backend.scripts.authorize_gmail     # writes GMAIL_REFRESH_TOKEN
```

Needs `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` from an OAuth client (type: Web
application) with `http://localhost:8080/` registered. Create it in a GCP
project owned by the mailbox's own domain and set the consent screen to
**Internal** — an External app in Testing status expires refresh tokens after
7 days.

**Service account + domain-wide delegation** — legacy, being retired. Needs
`service_account.json` and admin-granted delegation. The connector warns when
it falls back to this.

Either way the connector verifies the token actually opens `GMAIL_MAILBOX` and
fails loudly on a mismatch, so you can't silently ingest the wrong inbox.

## Useful commands

```bash
python -m backend.app.main                      # one-shot pipeline walk, sends nothing
python -m backend.scripts.ingest_window         # ingest a specific date window
python -m backend.scripts.audit_sync_gaps       # days where Gmail has mail we don't
python -m backend.scripts.reconcile_labels      # re-sync labels against the cache
python -m backend.scripts.seed_agents_table     # CSV → Supabase agents
```

```bash
curl "http://localhost:8001/automation/status"           # scheduler + safe_mode
curl "http://localhost:8001/fetch-inbox?limit=5"         # inbox page + labels
curl -X POST "http://localhost:8001/automation/run-now"  # 202, runs in background
```

`run-now` returns immediately — it does not wait for the scan. Poll
`/automation/status` and watch `last_run.run_at` change. A 409 means a scan is
already in flight.

## What happens on startup

With `RUN_SCHEDULER=1` (the default), `api.py`'s lifespan handler starts three
one-shot threads — LLM warmup, an ingest, and a classification backfill — then
registers four repeating jobs:

| Job | Interval |
|---|---|
| Inbox scan | 5 min |
| Email ingest | 5 min |
| Retry pending classifications | 15 min |
| Sync-gap audit | 24 h |

The startup ingest and backfill both hit the LLM, so a cold boot on a large
inbox costs real money. `RUN_SCHEDULER=0` avoids it.

If you ever run more than one instance, set `RUN_SCHEDULER=0` on all but one —
the atomic claim makes double-processing safe, but you'd be paying twice.

## Troubleshooting

**API won't start, error mentions Supabase.** `SUPABASE_URL`/`SUPABASE_KEY` are
missing or wrong. The client is built at import time, so a bad value kills the
whole server rather than one endpoint — including endpoints that never touch
the database. Known bug ([BUGS.md](BUGS.md#p0)).

**Everything is labelled "⏳ Pending".** The LLM provider is failing — usually
quota. The retry job will drain the queue once it recovers. Check the logs for
`insufficient_quota`.

**Emails aren't appearing.** Check `sync_state.last_received_at`, then run
`audit_sync_gaps`. If Gmail's count exceeds the database's for a given day,
`ingest_window` that range.

**A rate card didn't show up on the request page.** It wasn't linked. Either the
reply dropped the RFQ reference or it cited one that matches no job — the scan
logs which. The email itself is fine; find it in the inbox:

```sql
select provider_msg_id, sender, subject from emails
where classification = 'quotation_rate_card' and rfq_reference is null
order by received_at desc;
```

**The scan keeps skipping an email.** It failed three times and was retired:

```sql
select provider_msg_id, subject, processing_attempts, processing_error
from emails where processed_at is null and processing_attempts >= 3;
```

Fix the cause, then `update emails set processing_attempts = 0` for those rows
to put them back in the queue.

**Frontend loads but everything is empty.** Backend isn't up, or CORS. Only
`http://localhost:3000` and `:3001` are allowed origins.
