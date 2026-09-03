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

You will be bounced to `/login`, because auth is on by default. Give yourself an
account first — there is no signup route:

```bash
python -m backend.scripts.create_user --email you@yourdomain.com
```

It prompts for the password twice without echo (minimum 12 characters) and needs
`setup_app_users.sql` to have been run. `JWT_SECRET` must be set in `.env` and at
least 32 characters, or the API refuses to boot:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Deploying is [06-deploying.md](06-deploying.md).

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
setup_app_users.sql                 app_users — required, the API refuses logins without it
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

With `RUN_SCHEDULER=1` (the default), `app/lifespan.py` starts four one-shot
threads — LLM warmup, an ingest, a classification backfill, and one pass of the
attachment worker — then registers seven repeating jobs:

| Job | Interval | `job=` id prefix |
|---|---|---|
| Inbox scan | 5 min | `scan` |
| Email ingest | 5 min | `ingest` |
| Attachment worker | 2 min | `attachment_worker` |
| Retry pending classifications | 15 min | `retry_pending` |
| Recover RFQs stuck at `sending` | 15 min | `stuck_sends` |
| Metrics snapshot | 1 h | `metrics_snapshot` |
| Sync-gap audit + heal | 24 h | `gap_heal` |

Each job runs inside a correlation context, so every line it emits carries
`job=<name>:<hex>` — `grep 'job=ingest:'` gives you one run of one job and
nothing else. See [logging](#logging-and-tracing-a-request) below.

The startup ingest and backfill both hit the LLM, so a cold boot on a large
inbox costs real money. `RUN_SCHEDULER=0` avoids it.

### `RUN_SCHEDULER` is the one setting you must get right when scaling out

The scheduler starts in the lifespan handler, so **every process that runs
lifespan gets its own copy of every one of those jobs.** That includes:

- `uvicorn --workers 3` — lifespan runs three times, three schedulers
- three container replicas behind a load balancer — three schedulers
- any separate worker or cron container that imports the app

Three schedulers scanning the same inbox on the same 5-minute cadence is *safe*:
the atomic claim in the scan means no email is processed twice. But the claim
happens **after** classification, so you pay OpenAI three times for identical
work — silently, indefinitely, and in proportion to how far you have scaled.

**Rule: `RUN_SCHEDULER=1` on exactly one instance, `0` on every other.** Scale
with replicas rather than `--workers`, because `RUN_SCHEDULER` cannot tell one
worker of a process from another.

Forgetting `=0` costs money quietly. Forgetting `=1` is loud — nothing gets
ingested, and boot logs say so:

```
INFO backend.app.lifespan RUN_SCHEDULER=0 — no background jobs in this process
```

which is why the `Dockerfile` defaults it to `0` and makes you opt in.

Splitting the scheduler into its own process removes the footgun entirely; that
is Phase 5 of [05-hardening-plan.md](05-hardening-plan.md).

## Logging and tracing a request

Four correlation ids are stamped on every line, by a filter on the handlers
(`core/logging_context.py`):

| id | Set by | Answers |
|---|---|---|
| `request_id` | HTTP middleware; also returned as `X-Request-ID` | which request |
| `job_id` | the `_job()` wrapper in `lifespan.py` | which scheduled run |
| `scan_id` | `run_scan` | which scan pass |
| `email_id` | the classifier, per message | which email a decision was about |

In text mode they render as a suffix — `... [req=a1b2c3d4 email=18f2...]`. Two
things worth knowing:

- **Ids cross thread pools.** They would not by default: a contextvar is
  per-thread, so anything handed to a `ThreadPoolExecutor` starts with an empty
  context and logs with no ids at all. Pool workers are wrapped in
  `carry_context` for this reason. If you add a pool, wrap its worker or its
  output becomes unattributable.
- **`logger.exception`, not `logger.error`, inside an `except`.** The message
  alone is often useless — `str(e)` on a `KeyError` is a bare key name, and on a
  bare `RuntimeError` it is the empty string.

To follow one operator action all the way through the scan it triggered:

```bash
curl -X POST localhost:8001/automation/run-now -D - -o /dev/null   # note X-Request-ID
grep 'req=<that id>' logs/backend.log
```

Set `LOG_JSON=1` for one JSON object per line, with the four ids as queryable
fields rather than text inside the message. Use this anywhere you have log
aggregation; the text format is for reading by eye.

## Running in production

Everything above assumes a developer's machine. For an unattended deployment:

```bash
docker build -t logistics-copilot .
docker run -p 8001:8001 --env-file .env -e RUN_SCHEDULER=1 logistics-copilot
```

What the image does differently, and why:

| | Local | Image |
|---|---|---|
| Dependencies | `requirements.txt` (all 16 unpinned `>=`) | `requirements.lock`, `pip install --no-deps` |
| Reload | `--reload` | none — the source in an image does not change |
| Logs | rotating file | stdout (`LOG_TO_FILE=0`), JSON (`LOG_JSON=1`) |
| Scheduler | on by default | **off** by default — opt in on one replica |

**`requirements.lock`** is generated, not hand-edited. Every dependency in
`requirements.txt` is an unpinned `>=`, so two installs a week apart get
different code — and the nightly report workflow reinstalls on every run, which
means a breaking `openai` or `supabase` release would kill it with no commit to
blame. Regenerate after changing `requirements.txt`:

```bash
uv pip compile requirements.txt --python-version 3.12 -o requirements.lock
```

`--no-deps` in the Dockerfile is deliberate: the lock already names every
transitive package, so pip installs exactly what is listed and resolves nothing.
A dependency missing from the lock then fails the build loudly instead of at
import time in production.

**Secrets** are injected at runtime. `.env` and `service_account.json` are in
`.dockerignore` — an image layer is readable by anyone who can pull the image,
and deleting a file in a later layer does not remove it from the earlier one.

### Not done yet

Ordered by risk removed. The first is a blocker, the rest are hardening.

- **Authentication.** There is none, and the image binds `0.0.0.0`.
  `POST /send-rfq` and `POST /jobs/{ref}/approve` mail real freight vendors;
  `GET /agents` returns 110 real contacts. CORS restricts browsers only — it
  stops nothing with `curl`. Do not expose this publicly. ([BUGS.md](BUGS.md#p1))
- **`EMAIL_REDIRECT` does not work on the Outlook path.** `send_rfq_email`
  returns to the Outlook sender before the redirect is applied, so with
  `EMAIL_PROVIDER=outlook` safe mode is inert while `/health` reports it active.
- **CI on push.** The only workflow is the nightly report; nothing runs the
  suite on push or PR. 193 tests, no secrets, one second — the cheapest job
  available, and the thing that stops the lock file above from rotting.
- **Hash pinning.** `requirements.lock` pins versions but not hashes. Add
  `--generate-hashes` and `pip install --require-hashes` once there is CI to
  prove the build still works.
- **A declared Python version.** The floor is 3.10 (several modules annotate
  `int | None` in signatures evaluated at runtime, so 3.9 fails at import) and
  nothing states it — no `pyproject.toml`, no `requires-python`. Only the
  Dockerfile and the nightly workflow pin 3.12.
- **Log aggregation.** `LOG_JSON=1` produces the right shape; nothing collects
  it yet. Sentry is Phase 3's remaining item and needs a DSN.
- **A scheduler process of its own,** so `RUN_SCHEDULER` stops being a footgun.
  Phase 5 of [05-hardening-plan.md](05-hardening-plan.md).

## Troubleshooting

**Requests that touch the database return 503, error mentions Supabase.**
`SUPABASE_URL`/`SUPABASE_KEY` are missing or wrong. Since 2026-08-10 the client
is built lazily in `core/db.py::get_db()`, so a bad value fails the individual
request rather than the whole server — `/health` still answers (with 503), and
endpoints that never touch the database keep working. `check_connectivity()`
also logs a diagnosis once at startup, so look there first.

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
