# 5. Hardening plan

How to get this backend from "works on the developer's machine" to something
you'd leave running unattended. Ordered by risk removed per hour spent.

**Baseline:** 39 Python files, ~7,000 lines, 0 tests.

**What not to touch:** `connectors/email_store.py` is the best-engineered file
here — the gap-proof sweep, the conservative watermark, the audit. Leave it
alone. And resist introducing an ORM or a DI framework; the problems below are
all solvable with plain functions and one module boundary.

---

## Phase 0 — Stop the bleeding (1–2 days)

Nothing else matters until these are done.

| # | Task | Why now |
|---|---|---|
| 0.1 | Lazy `get_supabase()` in `core/db.py`; replace the 4 import-time clients | One bad env var currently kills the whole API, including endpoints that never touch the DB |
| 0.2 | Server-side hydration for `POST /feedback` + a shared `apiFetch()` in the frontend | Every dashboard label correction 422s and the UI reports success |
| 0.3 | Auth: shared-secret header, or bind to `127.0.0.1` | `/send-rfq` emails real vendors; `/agents` leaks 110 real contacts; there is no auth at all |

0.1 is the keystone. A large amount of the awkwardness below — inline imports
scattered through function bodies, `try/except ImportError` dances — exists
purely to dodge import-time crashes. Fix the cause and the workarounds can go.

---

## Phase 1 — Foundations (3–4 days)

### 1.1 One config object

Today `os.environ.get(...)` appears in ~10 modules, several with their own
default for the same key, and `load_dotenv()` is called in eight places (some
with a path, some without — so behaviour depends on your working directory).

```python
# backend/core/config.py
@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_key: str
    openai_api_key: str
    llm_provider: str = "openai"
    email_redirect: str = ""
    attachment_bucket: str = "rate-card-attachments"
    run_scheduler: bool = True
    scan_batch: int = 50
    log_level: str = "INFO"

    @classmethod
    def load(cls) -> "Settings": ...   # reads env once, validates, fails loudly
```

One `load_dotenv(PROJECT_ROOT / ".env")`, at the entrypoint only. A missing
required key fails at startup with a message naming the key — not at 3am inside
a scheduler thread.

### 1.2 Logging (detailed below — this is the big one)

### 1.3 Kill the inline imports

Once 0.1 lands, hoist the `from backend.x import y` statements currently buried
inside `_process_rate_card`, `_store_quotations`, `run_scan`, `_run_ingest_job`
and friends back to module scope. They were defensive; the defence is no longer
needed.

---

## Phase 2 — Structure (4–5 days)

### The problem

`app/api.py` is ~1,100 lines holding: 18 endpoints, every Pydantic model, the
scheduler wiring, three exception handlers, the CORS config, and helpers like
`_agent_email_for`. Endpoints talk to Supabase directly, passing raw dicts
around, so there is no place to put a business rule that two endpoints share —
and no way to test one without a database.

### The target

```
backend/
├── app/
│   ├── main.py              create_app() — wiring only, ~60 lines
│   ├── lifespan.py          scheduler + startup jobs
│   ├── errors.py            AppException + handlers
│   └── routes/
│       ├── inbox.py         /fetch-inbox, /email-body, /email-attachments
│       ├── jobs.py          /jobs/*, /customer-request
│       ├── rfq.py           /agents, /extract-details, /preview-rfq, /send-rfq
│       ├── automation.py    /automation/*
│       └── ops.py           /health, /feedback
├── services/                ← NEW: business rules, no HTTP, no FastAPI
│   ├── rfq_service.py       draft → send → persist jobs
│   ├── reply_service.py     link a reply, list replies, list unlinked
│   └── inbox_service.py     paging, label resolution
├── repositories/            ← NEW: the only modules that know Supabase exists
│   ├── email_repo.py
│   ├── job_repo.py
│   └── agent_repo.py
├── domain/                  ← NEW: dataclasses, not dicts
│   └── models.py            Email, RfqJob, AgentContact
└── (connectors / agents / classifier / core unchanged)
```

**Three rules that make it stick:**

1. A route function does three things: validate input, call one service, shape
   the response. If it contains `supabase.table(...)`, it's in the wrong layer.
2. Repositories return **domain dataclasses**, never raw Supabase dicts. Right
   now `r.get("shipment_weight_kg", 0)` appears in several places and each
   caller re-guesses the shape.
3. Pydantic at the HTTP boundary, dataclasses inside. This is already in your
   `.claude/rules/python.md`; the code doesn't follow it yet.

The payoff is testability: a service takes a repository, so a fake repository
gives you real unit tests with no network.

### Also in this phase

- Split `automation.py` (state / helpers / scan loop are currently one file).
- Delete `POST /process-email` and the office-view animation engine
  (`frontend/src/app/page.tsx`) — roughly 1,400 lines of dead code.
- Frontend: one `lib/api.ts` exporting `API_BASE` and `apiFetch`, replacing the
  constant duplicated across four route files.

---

## Phase 3 — Logging and observability ✅ DONE

> Implemented 2026-08-10/11. What follows describes the problem that was fixed;
> the design notes are kept because they explain *why* each piece is shaped the
> way it is. Remaining: Sentry (needs a DSN), and a scraper for `/metrics`.

The state this replaced:

- `logging.basicConfig()` is called in **two** entrypoints (`api.py`,
  `daily_report.py`), so whichever imports first wins and the other's format is
  ignored.
- Loggers are inconsistent: `getLogger("logistics_copilot")`,
  `getLogger("logistics_copilot.automation")`, and `getLogger(__name__)` all
  coexist, so you cannot filter by subsystem reliably.
- **httpx logs every Supabase REST call at INFO.** A single scan produces
  hundreds of lines of URL noise that drown the application's own messages —
  you saw this in every command run today.
- `classify_with_cache` emits 3–4 lines *per email* (`CACHE HIT`, `API CALL`,
  `API RESULT`). A 500-email backfill is ~1,500 lines.
- A secret was being logged: `daily_report` printed the first 8 characters of
  `OPENAI_API_KEY` on every run, into CI logs. Removed today — worth a grep for
  others.
- Nothing correlates. To answer "what happened to this email?" you grep for a
  message id and hope.
- No rotation. `backend.log`, `uvicorn.log`, `watchdog.log` and friends are
  manual shell redirects that grow without bound.

### 3.1 One logging setup module ✅

```python
# backend/core/logging_config.py
def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Call ONCE, from an entrypoint. Never call basicConfig anywhere else."""
    # - root handler with a consistent format
    # - RotatingFileHandler (10 MB × 5) alongside the console
    # - silence the noisy third parties:
    for noisy in ("httpx", "httpcore", "hpack", "urllib3", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
```

That one loop is the highest-value logging change in this document. It makes
the logs readable today, for ten minutes of work.

### 3.2 Structured logs with correlation ✅

Move to JSON in production (`python-json-logger`, one dependency) and attach a
correlation id via `contextvars`:

- **`scan_id`** — a UUID per `run_scan`, on every line that run emits.
- **`email_id`** — the `provider_msg_id`, on every line about one email.
- **`request_id`** — a middleware-generated id per HTTP request, returned in an
  `X-Request-ID` response header.

Then "what happened to this email?" becomes one query, and "why was that scan
slow?" becomes a filter. This is what turns logs from a narrative you read into
data you query.

### 3.3 Log levels that mean something ✅

Agree the contract and apply it:

| Level | Use for | Example |
|---|---|---|
| ERROR | A human must act | Job persist failed after a send succeeded |
| WARNING | Degraded but self-healing | LLM rate-limited, retrying; reply parked unlinked |
| INFO | State changes worth an audit trail | RFQ sent, reply linked, job approved |
| DEBUG | Per-item detail | Cache hit/miss, per-email classification |

Demote `classify_with_cache`'s per-email lines to DEBUG and log one summary line
at INFO: `"Classified 500: 480 cached, 20 API calls, 0 errors"`.

### 3.4 What to log at INFO, always ✅

The audit trail a freight desk will eventually be asked for: **who sent what to
whom, and when.** Every outbound email (recipient, reference, subject,
redirected-or-not), every approval, every automation toggle. These are business
events, not debugging — they should survive a log-level change.

### 3.5 Errors and metrics — `/metrics` ✅, Sentry ⏸ deferred

`GET /metrics` covers the unprocessed backlog, emails stuck at
`processing_attempts >= 3`, `pending` classifications, unlinked rate cards, and
the last scan. Every one is a cheap COUNT. A failed query returns `-1`, so a
broken metric never reads as a healthy zero.

**Tracking today is pull-only and manual** — the endpoint computes on request,
stores nothing, and nothing polls it. That is a deliberate stopping point, not
an oversight: the numbers are all derivable from the database at any time, so
there is no history to lose. The one exception is `last_scan`, which is real
stored history (`automation_state.last_stats`, written by `_save_stats` after
every completed run).

Wiring a scraper is Phase 5 work — see *Log and metric storage* below.

**Sentry — deferred by decision (2026-08-12), not rejected.** Revisit when the
service moves off the laptop. What makes it the go-to when that happens:

- Unhandled exceptions in scheduler daemon threads currently land only in a log
  file nobody watches. That is the actual gap; `/metrics` does not cover it.
- `core/logging_context.current_ids()` already exists to attach `request_id` /
  `scan_id` / `email_id` to a report — written for this integration.
- Integration is roughly ten lines: `sentry_sdk.init(dsn=..., traces_sample_rate=0)`
  plus a `before_send` that merges `current_ids()` into the event tags.
- Blocker is a DSN and the decision to send data off-site, nothing technical.

### 3.6 Log retention ✅

`RotatingFileHandler` at **10 MB × 50** (`LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` in
`core/logging_config.py`). ~510 MB worst case; at current volume roughly a year
of history. Rotation deletes the oldest file, so a small backup count silently
destroys exactly the history an audit would ask for — hence the generous count.

---

## Phase 4 — Tests and CI (3–4 days)

### Tier 1 — pure functions ✅ done 2026-08-12

134 tests in `tests/`, offline, ~1 second: `python -m pytest`.

`extract_rfq_reference` got the most cases, because **all** rate-card
attribution depends on it. Also covered: the classifier rule tiers,
`_parse_llm_label`, `retry_utils`, `domain/models` row parsing, the correlation
contexts, secret redaction, and `ScanStats` persistence.

The suite blocks outbound sockets. The first run made a real OpenAI call — a
classifier test fell through the rules into the LLM path — which is the kind of
thing a unit suite must fail on rather than quietly pay for.

It earned its keep immediately: `_COVER_NOTE_RE` did not match "find attached",
only the literal "find attach", so rate cards arriving as attachments were being
labelled `general`.

### Tier 2 — services against fakes ⬜ next

Then, once Phase 2 gives you repository interfaces: service tests against fakes
(`test_reply_service_links_by_reference`,
`test_reply_service_leaves_unmatched_unlinked`), and FastAPI `TestClient` tests
with dependency-overridden repositories.

CI (`.github/workflows/ci.yml`): ruff + mypy + pytest on every push;
`tsc --noEmit` + `next build` for the frontend. You already have the Actions
setup — this is one more file.

**Target: 60% coverage on `services/` and `core/`.** Don't chase a number on
`connectors/`; those are integration seams better covered by the existing audit
jobs.

---

## Phase 5 — Productionise (3–5 days)

| Area | Now | Target |
|---|---|---|
| Deploy | `run_backend.sh` + `--reload` on a laptop | Dockerfile, gunicorn + uvicorn workers, `--reload` off |
| Scheduler | In-process, `RUN_SCHEDULER=1` | Separate container/process. Multiple API replicas then become safe |
| Secrets | `.env` in a OneDrive-synced folder next to `service_account.json` | A secret manager. At minimum, move the repo out of a synced directory |
| Migrations | 9 loose `.sql` files applied by hand in a documented order | Numbered, tracked, idempotent — a `schema_migrations` table or Supabase's own migration tooling |
| Backups | None | Supabase PITR + a verified restore drill |
| Health | `/health` (added today) | Wire it to an uptime monitor that pages someone |
| Rate limits | None | Per-IP limits on the LLM-backed endpoints (`/preview-rfq`, `/extract-details`) |

### Log and metric storage

Four stages. Each is useful on its own; none requires the next.

**Stage 0 — get the log out of OneDrive (do this now, 1 minute)**

`logs/backend.log` currently sits inside a OneDrive-synced folder. A rotating
log is a file rewritten continuously; the sync client uploads every revision,
and at 10 MB × 50 that is a lot of pointless upload traffic and version history.
`LOG_DIR` now exists for exactly this:

```
LOG_DIR=C:\ProgramData\logistics-copilot\logs
```

**Stage 1 — stop writing files at all, once containerised**

Twelve-factor: a process writes to stdout and knows nothing about storage.
`LOG_TO_FILE=0` + `LOG_JSON=1` gives that. Docker/systemd/ECS collects the
stream. Rotation, retention, and shipping stop being this codebase's problem —
which is the point, because a container's disk is ephemeral and a log file on it
is lost on every restart.

The file handler stays for the laptop and for anyone running `python -m` scripts
directly. Both modes are one env var apart.

**Stage 2 — one searchable store**

Two log sources to unify: the API process and the scheduler process (they split
in this phase). Grepping two containers' stdout is not a workflow.

| Option | Cost | Why / why not |
|---|---|---|
| **Grafana Loki + Promtail** | free self-host, ~$0 at this volume | Indexes labels not full text, so it is cheap and fast for `{app="copilot"} \| json \| email_id="..."`. Recommended: matches how the correlation ids are already shaped. |
| Better Stack / Axiom | free tier covers a few GB/month | Zero ops. Good if nobody wants to run Loki. |
| Datadog | expensive per GB | Only if the business already pays for it. |
| Postgres table | "free" | Do not. Log writes would compete with the application's own database. |

Whichever is chosen, the JSON formatter already emits `request_id`, `scan_id`,
`email_id` as top-level fields, so the queries work on day one with no parsing
rules to write.

**Stage 3 — separate the audit trail from the logs**

The most important point in this section: **the business record must not live
only in logs.**

"Which agents did we contact for RFQ-20260101-a1b2, and when?" is a question a
freight desk will eventually be asked by a customer or an auditor. Answering it
from a log store means that answer disappears at whatever retention the log tier
happens to have, and depends on nobody having changed a log level.

Business events — RFQ sent, reply linked, job approved, automation toggled —
belong in an `audit_events` table:

```sql
create table audit_events (
  id           bigserial primary key,
  occurred_at  timestamptz not null default now(),
  event        text not null,          -- rfq_sent | reply_linked | job_approved
  reference    text,                   -- RFQ-YYYYMMDD-xxxx
  actor        text,                   -- operator email, or 'system'
  detail       jsonb not null default '{}',
  request_id   text,                   -- joins back to the logs
  scan_id      text
);
create index on audit_events (reference);
create index on audit_events (occurred_at desc);
```

Keep logging these at INFO too — the log is for debugging *how*, the table is
for answering *what*. `request_id` on the row is the join between them.

**Retention, once there is somewhere to retain things**

| Tier | Keep | Where |
|---|---|---|
| Hot, searchable | 30 days | Loki / hosted store |
| Cold archive | 1 year | Compressed to object storage (Supabase Storage or S3), never queried, exists for an incident |
| Audit events | indefinite | Postgres — it is business data, not telemetry |
| Local rotating file | ~1 year at current volume | Laptop / VM disk, unchanged |

**Two things to settle before shipping logs anywhere**

1. **PII.** These logs are about customer emails. Subjects are logged; bodies
   are not, and should stay that way. Before any log leaves the building, decide
   whether sender addresses and subjects going to a third-party store is
   acceptable — for a freight desk handling client shipment details, that is a
   real question, not a formality.
2. **Secrets.** One key prefix was being logged and was removed. Add a
   `before_send`-style scrub, or at minimum a grep in CI for `API_KEY` near a
   log call, so it cannot come back.

**Metrics, same trajectory**

`/metrics` is pull-only and computes on demand — no history. That is fine while
the answer to "is anything stuck?" is only ever asked in the present tense. When
trend matters ("has the unlinked backlog been growing for a week?"), the cheapest
step is a scheduler job that snapshots the same counts into a
`metrics_snapshots` table every hour. Prometheus + Grafana is the fuller answer
and needs the endpoint re-rendered in Prometheus text format — a formatting
change, since the data is already there.

**The scheduler split is the important one.** While ingest and scan live inside
the API process, you cannot run two API instances without duplicating LLM spend,
and a slow scan competes with HTTP requests for the same interpreter. Pulling it
into its own process makes the API horizontally scalable and lets you restart it
without pausing ingestion.

---

## Suggested sequence

```
✅ Phase 0   done, except 0.3 (auth) — a deployment decision, still open
✅ Phase 1   config, lazy db, logging setup
✅ Phase 2   routes / services / repositories / domain
✅ Phase 3   correlation ids, JSON logs, /metrics.  Sentry still open
⬜ Phase 4   tests + CI  ← the largest remaining gap
⬜ Phase 5   Docker, scheduler split, tracked migrations, backups
```

**Phase 4 is now the constraint.** The structure exists to make services testable
with fake repositories, and nothing yet uses it. Start with the pure functions —
`extract_rfq_reference` carries all rate-card attribution and has no test.
