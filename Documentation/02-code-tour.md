# 2. Code tour

## The layers

Requests flow down; nothing flows back up.

```
routes/        HTTP only. Validate input, call one service, shape the response.
services/      Business rules. No FastAPI import, no Supabase.
repositories/  The only modules that touch Supabase.
domain/        The dataclasses those pass around.
```

Two rules keep it honest: a route containing `supabase.table(...)` is in the
wrong layer, and repositories return **dataclasses, never raw Supabase dicts**.
That second one is what makes services testable — swap a repository for a fake
and the rules run with no network.

## If you read five files, read these

In this order. About an hour, and you'll understand 80% of the system.

1. **`backend/app/routes/`** — five small modules, one per area. Start with
   `inbox.py`, then `rfq.py`. Together they are the whole API surface.
2. **`backend/services/rfq_service.py`** — the only path by which mail reaches a
   vendor. Read `send_rfqs` and `approve`.
3. **`backend/connectors/email_store.py`** — how mail gets in. The comments
   explain *why* the sweep works the way it does; they're worth reading.
4. **`backend/automation/automation.py`** — the 5-minute loop and rate-card
   linking.
5. **`backend/classifier/email_classifier.py`** — the classification prompt is
   the highest-leverage text in the repo. Changing it changes everything
   downstream.

## Layout

```
backend/
├── app/            The web layer — wiring and HTTP, nothing else
│   ├── api.py            create_app(). ~50 lines: CORS, handlers, routers.
│   ├── errors.py         AppException + the handlers that make every response JSON
│   ├── lifespan.py       Scheduler ownership: 3 startup jobs, 4 repeating ones
│   ├── routes/
│   │   ├── inbox.py      /fetch-inbox, /email-body, /email-attachments,
│   │   │                 /rate-cards/unlinked
│   │   ├── jobs.py       /jobs/*, /customer-request, approve
│   │   ├── rfq.py        /agents, /extract-details, /preview-rfq, /send-rfq
│   │   ├── automation.py /automation/*
│   │   └── ops.py        /health, /feedback
│   ├── main.py           One-shot CLI walk of the pipeline. Read-only, prints
│   │                     drafts, sends nothing. For local debugging.
│   └── daily_report.py   Nightly Excel report (GitHub Action, 02:30 UTC).
│                         Reads the emails table — no mailbox credentials, and
│                         reuses stored labels rather than re-classifying.
│
├── services/       Business rules. Testable with fake repositories.
│   ├── rfq_service.py    Draft → send → persist; and awarding a job
│   ├── reply_service.py  Link a reply to its RFQ; list replies and unlinked
│   └── inbox_service.py  Paging and which label to show
│
├── repositories/   The only package that knows Supabase exists
│   ├── email_repo.py · job_repo.py · agent_repo.py
│
├── domain/
│   └── models.py         Email, RfqJob, AgentContact, Attachment
│
├── connectors/     Everything that talks to a mail provider
│   ├── email_store.py       Gmail → Supabase ingest, watermark, retry queue,
│   │                        gap audit. The most carefully-written file here.
│   ├── gmail_connector.py   Gmail REST. User OAuth preferred; service-account
│   │                        + domain-wide delegation is the legacy fallback
│   │                        being retired.
│   ├── google_oauth.py      Refresh-token plumbing for the above
│   ├── email_connector.py   IMAP. Legacy — only backend/app/main.py uses it.
│   ├── email_sender.py      SMTP send. Honours EMAIL_REDIRECT (see below).
│   ├── outlook_connector.py \  Microsoft Graph equivalents. Wired but not the
│   ├── outlook_sender.py     > active path; EMAIL_PROVIDER=outlook selects them.
│   └── graph_auth.py        /
│
├── agents/         Business logic. Plain functions, one LLM call each.
│   ├── intake_agent.py      Email → ShipmentDetails (origin, destination,
│   │                        weight, commodity, mode). Every field optional.
│   └── rfq_agent.py         Draft the RFQ email. Uses gpt-4o.
│
├── classifier/
│   ├── email_classifier.py     3 rules + one LLM call. Holds the big prompt.
│   ├── classification_cache.py Supabase-backed label cache; the LLM runs once
│   │                           per email ever.
│   └── llm_provider.py         Provider registry (openai, gemini). Add one by
│                               writing a class + @register_provider.
│
├── automation/
│   └── automation.py       The 5-minute scan. Atomic claim with retry,
│                           rate-card linking, run stats.
│
├── core/
│   ├── config.py           Every env var, read once, frozen. The ONLY
│   │                       load_dotenv() in the codebase.
│   ├── db.py               get_db() — the lazy Supabase client. Never build
│   │                       one at import time.
│   ├── logging_config.py   configure_logging(), called from entrypoints only
│   ├── paths.py            All filesystem paths. Import from here, never
│   │                       compute __file__-relative paths.
│   ├── rfq_reference.py    The RFQId token: inject it into an outgoing subject,
│   │                       extract it from a reply, detect it for the
│   │                       classifier. The only link between a reply and its
│   │                       shipment. Matches the legacy bare form too.
│   └── retry_utils.py      with_retry() decorator.
│
└── scripts/        One-off maintenance. Run by hand, never by the server.
```

```
frontend/src/app/
├── dashboard/page.tsx   THE UI. Inbox / Customer Requests / Rate Cards /
│                        Shipments, plus theme toggle and automation switch.
├── send-request/page.tsx The RFQ composer. Extract → draft → edit → send.
├── request/[id]/page.tsx One customer request: original email, agents
│                        contacted, rate cards received.
└── page.tsx             Legacy "office view". See below.
```

## The API surface

16 endpoints. Grouped by what actually calls them:

**Dashboard reads**
`GET /fetch-inbox` · `GET /email-body/{id}` · `GET /email-attachments/{id}` ·
`GET /jobs` · `GET /jobs/{ref}/replies` · `GET /rate-cards/unlinked` ·
`GET /customer-request/{id}` · `GET /automation/status`

**Operations**
`GET /health` — 200 when Supabase is reachable, 503 when it isn't

**Send Request flow**
`GET /agents` · `POST /extract-details` · `POST /preview-rfq` · `POST /send-rfq`

**Actions**
`POST /jobs/{ref}/approve` (no body — one job is one agent) · `POST /feedback` ·
`POST /automation/toggle` · `POST /automation/run-now` (202, or 409 if a scan is
already running)

**Legacy — only the office view calls it**
`GET /jobs/{ref}`

**Dead — nothing calls it**
`POST /process-email`. Still live, still unauthenticated, still burns a gpt-4o
call per request. Should be deleted.

## Dead weight — don't be misled

**`frontend/src/app/page.tsx`** (~1400 lines). An animated pixel-art office
with four characters walking between desks. The state that drives it
(`apiResult`, `processResult`) is only ever set to `null`, so
`buildFlow`, `runFlow`, the sprite renderer and four of the seven render
branches never execute. The sidebar and the inbox list work; everything else is
scenery.

**`backend/scripts/classify_domains.py`** — a scratch file that reads
`domain_candidates.json` at module scope. That file doesn't exist, so it fails
on import, and nothing consumes its output.

## Conventions that are actually enforced

From `CLAUDE.md` and `.claude/rules/`:

- **Port 8001.** 8000 is occupied. Everywhere.
- **Raise `AppException`, never `HTTPException`.** Every endpoint returns JSON,
  including errors, so the frontend can always read `.detail`.
- **Column names bite.** `rfq_jobs.reference`, but `emails.rfq_reference` is the
  link back to it. `agents_contacted` is `text[]` of agent *names*, not objects.
- **Frontend: inline styles only**, all calls through the `API_BASE` constant,
  a TypeScript interface for every shape.
- **Python: type hints everywhere, `logging` not `print`, functions under 50
  lines**, Pydantic at API boundaries and dataclasses internally.
- **Paths come from `core/paths.py`.**

## Safety valves

**`EMAIL_REDIRECT`** — set it to your own address and *every* outgoing email
goes there instead of the vendor, with `[TEST → real@vendor.com]` prefixed to
the subject. `GET /automation/status` reports `safe_mode: true` when it's on.
**Set this before you touch anything that sends.**

**`RUN_SCHEDULER=0`** — starts the API without the background jobs. Use it when
you want to poke endpoints without ingest and scan running underneath you.

## Two sources of agent truth

`data/agents_database.csv` (110 rows) and the Supabase `agents` table are both
live. The table is seeded *from* the CSV by
`python -m backend.scripts.seed_agents_table`, and they drift if you edit one
and forget the other.

- `GET /agents` — the dropdowns on Send Request — reads the **table**.
- Rate-card sender resolution reads the **CSV**.

The CSV is one row **per office**, so a company with several branches appears
several times with different mailboxes — DP World 4×, ATC 3×, MSC 3×. Code that
keys on `agent_name` alone cannot tell them apart, which is
[a known bug](BUGS.md#p2).
