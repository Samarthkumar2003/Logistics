# Code Review — Graceful Failure & Production Readiness
_Date: 2026-07-07. Scope: full backend + frontend._

Severity: 🔴 blocker · 🟠 high · 🟡 medium · ⚪ low

---

## 1. Not failing gracefully

### 🔴 App won't boot if any credential is missing (import-time crashes)
Every agent module instantiates its client at import time:
- `intake_agent.py:21`, `rfq_agent.py:7`, `quotation_agent.py:17`, `price_predictor.py:14`, `history_agent.py:31` — `client = OpenAI()` at module scope. Missing/invalid `OPENAI_API_KEY` → `api.py` import fails → **entire API dead**, including endpoints that never touch OpenAI (`/fetch-inbox`, `/jobs`, `/agents`).
- `history_agent.py:27-28` — `raise RuntimeError` at import if Supabase env missing. Same blast radius.
- **Fix:** lazy client factories (`_get_client()` cached), so failures occur per-request and return a clean 503 with detail, not a dead server.

### 🔴 `automation_state.json` read-modify-write race
`run_scan` (scheduler thread) and `POST /automation/run-now` (request thread) can run concurrently. Both `_load_state()` → mutate → `_save_state()`. Lost updates → the same email processed twice → **duplicate RFQ emails to real vendors**. No file lock, no `threading.Lock`.
- **Fix:** module-level lock around scan, or move state to a Supabase table with unique constraint on processed email id.

### 🟠 Rate card "fallback to latest job" attaches quotes to the wrong job
`automation.py::_process_rate_card`: if no open job matches the sender, it stores the quotation against `open_jobs[:1]` — and that query has **no `.order()`**, so "latest" is actually unspecified row order. Quotes silently land on arbitrary jobs.
- **Fix:** order by `created_at desc`; better — if no match, store to a `quotations_unmatched` table or skip with a visible log, never guess.

### 🟠 `check-quotations` fuzzy match can cross-contaminate jobs
`api.py::check_quotations`: unseen emails from any contacted agent address are attached to *this* reference, even if the vendor was replying to a different RFQ. With the new one-reference-per-agent model this gets worse (same agent, many open references).
- **Fix:** require the reference in subject/body for auto-attach; fuzzy matches go to a review queue.

### 🟠 IMAP connections leak on error (`email_connector.py`)
All three fetch functions: `mail.close()/logout()` are not in `finally`. Any exception mid-fetch (malformed message, network blip) leaks the socket, and repeated failures exhaust Gmail's IMAP connection cap (15). Login failures raise raw `imaplib.error` upstream.
- **Fix:** `try/finally` or a context-manager wrapper; catch and translate auth errors.

### 🟠 Frontend: unchecked `res.json()` on non-ok responses
- `dashboard/page.tsx:330` (`/quotations`): `const d = await r.json(); setQuotes(d ?? [])` — a 500 returns `{detail}` object → `setQuotes({detail})` → `.map` crash (white screen).
- `dashboard/page.tsx:150` and `page.tsx:819` (`/feedback`): fire-and-forget, no `.ok` check, no user feedback if it fails — correction silently lost.
- **Fix:** shared `apiFetch()` helper that throws on `!res.ok` with parsed `detail`, used by all pages.

### 🟡 `GET /email-body/{id}` returns 500 for a bad id
Should be 404; frontend can then show "message no longer exists" instead of a generic failure.

### 🟡 `seed_agents_table.py` crashes with KeyError
`os.environ["SUPABASE_URL"]` — missing env gives a bare KeyError instead of a clear message.

### 🟡 No Gmail API retry/backoff
`gmail_connector.py::_gmail_get` → `raise_for_status()` with no handling for 429/5xx. Under the 20-worker `ThreadPoolExecutor` bursts, rate-limit errors kill individual fetches (logged + skipped — OK) but ingest gets partial batches with no retry. `retry_utils.with_retry` exists but isn't applied here.

### 🟡 Health endpoint was removed
`/health` 404s now. Deploy probes, uptime monitors, and quick local sanity checks have nothing to hit. Re-add — it's four lines.

---

## 2. Correctness risks

### 🟠 Two different email-id spaces feed the same classification cache
- Ingest/scan path (gmail_connector): ids are **Gmail message ids** (`19f3...`).
- `/classify-inbox` path (email_connector IMAP): ids are **Message-ID headers** (`<...@mail>`).
Same email classified twice under two keys; feedback corrections applied to one key don't reach the other. Standardize on one id space (Gmail msg id, since ingest owns persistence).

### 🟠 `/jobs` UI not updated for the per-agent-reference model
One customer email now creates N job rows (one per agent). The dashboard job list (limit 20, no pagination) fills up fast, and "agents_contacted" per job is now always a single name. Either group jobs by customer email in the UI or add a parent `rfq_batches` table.

### 🟡 `normalize_port` substring matching is aggressive
After exact match fails it scans `if alias in cleaned` — multi-stop strings ("Rotterdam via Antwerp") match whichever alias sorts first. Also conflicts with the intake prompt's "copy verbatim" rule — the normalizer rewrites what the model preserved. Tema→Accra was one bug of this class; audit pending (chip created).

### 🟡 `EMAIL_REDIRECT` is a silent global
If left set in a production `.env`, every vendor email quietly goes to the test inbox with only a log line. Make it loud: banner in `/automation/status` + returned in send results.

### ⚪ `retry_utils._is_retryable` defaults to retry on unknown errors
Content-policy or JSON-schema errors from OpenAI get retried 3× for nothing. Default-deny is safer with an explicit allowlist.

---

## 3. Production readiness

### 🔴 Zero authentication
Every endpoint is open: `/send-rfq` sends real emails, `/automation/toggle` disables the pipeline, `/fetch-inbox` exposes the full mailbox, `/email-body/{id}` reads any message. Anyone who can reach port 8001 owns the mailbox.
- **Minimum:** static API key header checked by middleware + frontend env var. Proper: Supabase Auth JWT.

### 🔴 Scheduler runs per process
`api.py` module scope starts `BackgroundScheduler` + ingest thread at import. With `--reload` (used in dev) the reloader parent + worker both import → **two schedulers**. With `uvicorn --workers N` in prod → N schedulers → N× duplicate scans/sends racing on the same state file.
- **Fix:** move scheduling to a FastAPI lifespan handler guarded by an env flag (`RUN_SCHEDULER=1` on exactly one instance), or run the scan loop as a separate process/cron.

### 🟠 No tests
Only eval scripts for the classifier. Nothing covers: intake sanitization, RFQ reference format, dedup keys, quotation matching, agents lookup merge, the API contract. The regressions found this week (weight hallucination, Tema mapping, commodity crash) are exactly the kind a 30-case pytest suite would have caught.

### 🟠 Frontend hardcodes `API_BASE = 'http://localhost:8001'` in 3 files
Duplicated constant (violates the project's own rule) and breaks any non-local deployment. Move to `NEXT_PUBLIC_API_BASE` env with one shared module.

### 🟠 Long-running LLM work in synchronous request handlers
`/process-email`, `/send-rfq`, `/check-quotations` each do multiple LLM calls + SMTP inside the request. Slow (10-30s), no idempotency key — a user double-click or client retry sends **duplicate RFQ emails**. Add an idempotency token from the form, and consider a job queue for sends.

### 🟡 Ops hygiene
- No Dockerfile / process manager config; `run_backend.sh` only.
- No structured logging or error alerting (errors vanish into uvicorn.log).
- No metrics on scan runs beyond the state-file stats.
- CORS pinned to localhost:3000/3001 — correct today, breaks first deploy.
- `.env`/`service_account.json` properly gitignored ✅.

---

## 4. Code quality

- **`page.tsx` is a 1450-line single component.** Inbox list, animation flow, jobs panel, feedback control all in one. Extract `EmailCard`, `JobsPanel`, `useInbox()` hook.
- **Duplication:** feedback POST implemented twice (page.tsx, dashboard); email-card UI twice; `fetch_emails_by_subject`/`fetch_unseen_emails` in email_connector are near-identical copies (extract a `_search_and_fetch(criteria)` helper); `_process_customer_email` (automation) vs `/process-email` (api) are 80% the same pipeline — extract a shared service function so fixes land once.
- **Dead/legacy code:** `app/main.py` is an obsolete CLI duplicating the pipeline with `print()`; Qwen/SVM machinery (~250 lines in email_classifier.py) unused at runtime — move to `evals/`; `CLF_MIN_CONFIDENCE`, `_build_mlp` aliases kept only for eval imports.
- **`dashboard.css` exists despite the "inline styles only" project rule** — pick one and update the rule or the code.
- **Naming drift:** `rfq_jobs.agents_contacted` is now always a single-element array; `reference` semantics changed from per-customer-email to per-agent — update `sql/setup_database_v2.sql` comments and CLAUDE.md so the schema doc matches reality.
- **`ExtractionError` class retained but nothing raises it** — delete or wire into caller-boundary validation.
- Type hints are generally good; Pydantic-at-boundary/dataclass-internal convention is followed consistently ✅. Logging discipline is good in backend (except legacy main.py) ✅.

---

## Suggested priority order

1. Auth middleware (🔴, ~1h)
2. Lifespan-guarded scheduler + automation state lock (🔴, ~2h)
3. Lazy LLM/Supabase clients → graceful 503s (🔴, ~1h)
4. Frontend `apiFetch()` helper + fix unchecked json/feedback calls (🟠, ~1h)
5. Rate-card matching: ordered query + no wrong-job fallback (🟠, ~1h)
6. Idempotency keys on send endpoints (🟠, ~2h)
7. IMAP try/finally, `/health` restore, 404 for bad email id (🟡, ~1h)
8. pytest suite for agents + API contract (🟠, ongoing)
