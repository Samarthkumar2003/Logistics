# Known bugs

_Last reviewed: 2026-08-10. Scope: full backend + frontend._

Priority is **impact × silence**. A defect that corrupts data without anyone
noticing outranks one that throws a visible error.

| | Meaning | Expectation |
|---|---|---|
| **P0** | Broken in production, or one bad env var from taking the app down | Fix now |
| **P1** | Wrong data, real money, or silent loss | Fix this cycle |
| **P2** | Visible malfunction, degraded UX, missing safety net | Schedule |
| **P3** | Hygiene, dead code, stale docs | Opportunistic |

---

<a id="by-design"></a>

## By design — not bugs (don't "fix" these)

### Bounded initial load — `ingest_new_emails` caps at `MAX_INGEST_BATCH` and advances the watermark anyway
On a backlog larger than 5000 new emails (weeks of downtime, or a brand-new
account's entire mailbox), one ingest run keeps only the **newest 5000** and
stops (`truncated`), and the watermark **still advances past** the older,
un-ingested window. So incremental ingest does **not** backfill that history.

This looks like the "watermark leapfrogs un-ingested mail" gap, and it is — but
it is **intentional**: pulling a whole mailbox history on a new account is load
nobody asked for. Older history is pulled deliberately with the manual scripts
(`backfill_3months.py` / `ingest_window.py`), which take an explicit date window.
Both the cap and the truncated-advance emit a `WARNING`, so it is visible in the
logs. Decision: 2026-08-16. Do not remove the ceiling or hold the watermark to
"fix" it. See [01-architecture.md](01-architecture.md) → "Bounded initial load".

---

<a id="p0"></a>

## P0 — fix now

_None open._

---

<a id="p1"></a>

## P1 — wrong data, real money, silent loss

### P1-1 · No authentication, bound to 0.0.0.0
`run_backend.sh` exposes the API on every interface. `POST /send-rfq` and
`POST /jobs/{ref}/approve` send real email to real vendors; `GET /agents`
returns the full contact database. CORS restricts browsers only — it stops
nothing with `curl`.

**Fix:** a shared-secret header at minimum, or bind to `127.0.0.1` until there
is real auth.

---

<a id="p2"></a>

## P2 — visible malfunction, missing safety nets

### P2-1 · Multi-office agents share one name
`agents_database.csv` is one row per **office** — DP World 4×, ATC 3×, MSC 3×,
each with its own mailbox — and `rfq_jobs.agents_contacted` stores names only.

Downgraded from P1 on 2026-08-10: rate cards now attach by RFQ reference, so a
reply can no longer be filed against the wrong branch's job, and
`_agent_email_for()` refuses to guess when a name maps to several addresses —
approval fails with a clear 422 instead of emailing the wrong office. What
remains is the inability to *tell them apart* in the UI.

**Fix:** `ALTER TABLE rfq_jobs ADD COLUMN agent_email text`, populate it in
`/send-rfq` (already one row per agent), and key the CSV loaders on
`(agent_name, email)`.

### P2-2 · "Load more" does nothing on first click
`emailOffset` starts at 0 and `fetchData` never advances it, so
`loadMoreEmails` re-requests `offset=0`, `dedupe` drops all 20, *then* sets
offset to 20. The "N remaining" count is also 20 too high.
**Fix:** set `emailOffset = PAGE` after the initial load.

### P2-3 · Startup thundering herd
`_run_ingest_job` and `_run_backfill` launch simultaneously at boot and both
classify; backfill re-scans the newest 500 emails every time. A cold boot on a
large inbox costs real money.

### P2-4 · No tests — *partly closed 2026-08-12*
134 unit tests now cover the pure functions: `tests/` run with
`python -m pytest`, offline, in about a second. Covered:
`extract_rfq_reference` / `inject_reference` / `subject_token`, the classifier
rule tiers and `_parse_llm_label`, `retry_utils`, `domain/models` row parsing,
the correlation-id contexts, secret redaction, `ScanStats` persistence, and
`_extract_country`.

Still open: **services against fake repositories, and any HTTP-level test.**
`reply_service.link_reply` — the function that decides which job a rate card
attaches to — has no test. Also no CI: nothing runs the suite on push.

`_strip_quoted` was deliberately left uncovered. `normalize_port` and
`_extract_country` were covered, then deleted along with their modules.

### P2-5 · `_parse_llm_label` fallback is too loose
`email_classifier.py` — a bare `"rate"` substring in any malformed reply yields
`quotation_rate_card`.

Now pinned by `test_p2_5_bare_word_rate_in_prose_becomes_a_rate_card`, so
tightening it is a deliberate change against a failing test. Note the
`"customer"` branch is checked first, so *"the customer asked about rates"*
resolves to `customer_requirement` — arguably the worse of the two, since that
label is what spawns an RFQ job.

### P2-6 · `with_retry` retries permanent errors
`retry_utils.py` defaults unknown exceptions to retryable, so
`run_intake_agent` burns three attempts with backoff on schema refusals and
content filters.

Pinned by `test_p2_6_unrecognised_errors_are_retried_by_default`. The tests also
confirm the parts that *are* right: a permanent error consumes exactly one
attempt, and "invalid api key … please try again" is correctly treated as
permanent despite matching both keyword lists.

---

<a id="p3"></a>

## P3 — hygiene

- **Secrets in a synced folder.** `.env`, `.env.dwd-backup` and
  `service_account.json` sit in a OneDrive-synced directory. `.gitignore` does
  not stop cloud sync. `data/agents_database.csv` is committed with 110 real
  vendor emails and mobile numbers; `data/historical_shipments.json` holds ten
  real completed shipments with the rates paid.
- **`next_run` is never returned.** Both frontends type and render it;
  `automation.get_status()` doesn't provide it. Always "—". The office view also
  captions the schedule "Daily at 07:00 UTC" when it's every 5 minutes.
- **Dead code.** The office-view animation engine in `page.tsx`;
  `scripts/classify_domains.py` (fails on import — its input file doesn't
  exist); the `shipments` table and vector functions in `setup_database.sql`;
  the `quotations` table, kept but no longer written.
- **`anthropic` in requirements** with no importer — `llm_provider` only
  mentions it in a docstring example.
- **Agent data quality.** "Emu Lines" and "Emulines" are listed as separate
  companies; two DP World rows carry `@unifeeder.com` addresses while Unifeeder
  is also its own entry.

---

## Fixed on 2026-08-12

**Port normalisation removed (1,116 lines).** `intake_agent`'s prompt told the
model to copy locations verbatim — *"if the email says 'Tema', output 'Tema'"* —
and the next two lines ran `normalize_port()` over both, mapping Mumbai to Nhava
Sheva. The customer asked about one port and the RFQ went out for another. The
prompt is now the whole policy. `port_normalizer.py` (921 lines, `normalize_port`
and `normalize_shipment`) is gone.

**Agent lookup removed (195 lines).** `agents_lookup.lookup_agents` filtered the
CSV by destination country and mode. Nothing in the live path called it — the
send-request page reads every agent from the `agents` table and the operator
picks. Its only caller was the debug CLI, now pointed at `agent_repo`. It also
carried a dormant copy of P2-1: it deduped on `agent_name` alone, so one office
of a multi-office agent silently won.

**Rate cards saying "please find attached" were not caught by the cover-note
rule.** `_COVER_NOTE_RE` ended in `attach\b`, so it matched the literal *"find
attach"* but not *"find attached"* or *"kindly find attached"* — the two most
common phrasings in the inbox. Those emails fell through to the LLM, which sees
a one-line cover note with no rate table and answers `general`, so an attached
rate sheet was labelled as ordinary mail. Found by the first run of the new test
suite. Now `find\s+attach\w*`, with all six live phrasings covered.

**Every entrypoint except the API bypassed the logging setup.** Eight scripts
and `email_connector` called `logging.basicConfig` directly, so none of them got
httpx silencing, correlation ids, rotation, or a log file — including
`backfill_3months` and `ingest_window`, the two bulk jobs where the noise was
worst. All now call `configure_logging()`; the data-mutating ones also write to
the rotating file.

**`daily_report` logged to the console only.** An unattended 02:30 job whose
failures left nothing on disk. Now writes to the rotating log.

**Nothing prevented a secret from being logged again.** `OPENAI_API_KEY`'s first
8 characters were once printed on every report run. `SecretRedactingFilter` now
masks any credential-shaped environment value, plus recognisable token shapes,
on both handlers.

---

## Fixed on 2026-08-11

**RFQ references were only enforced on one of two send paths.** `inject_reference`
ran when the operator sent an edited draft, but the model-generated path shipped
whatever subject gpt-4o produced — the prompt *asked* for the reference, nothing
checked. Any deviation put an RFQ into the world that its reply could never be
matched to. Both paths now inject, so exactly one reference is present and it is
the right one. This is the likeliest explanation for 248 rate cards in the
database and zero linked.

**The subject token is now labelled: `RFQId:20260101-a1b2`.** The canonical
stored form (`RFQ-20260101-a1b2`) is unchanged, because 172 jobs use it and ~150
RFQs are already in the wild; matching accepts both, plus case and spacing
variants, and extraction always returns the canonical form.

**Replies quoting an RFQId classify as rate cards without a model call.** An
external sender echoing a token we generated is, by construction, answering an
RFQ we sent — certainty the classifier previously spent an LLM call to guess at,
sometimes wrongly. Subject-only, so a reference buried in quoted history cannot
relabel an operational follow-up months later.

**The duplicated linking logic is gone.** `automation._process_rate_card` carried
its own copy of the rule while `reply_service.link_reply` — written for the
services layer during the restructure — had no callers at all. The scan now
delegates to the service, so correlation has one implementation and is testable
with a fake repository.

## Fixed on 2026-08-10

**IMAP connections are always released.** All three fetchers in
`email_connector.py` closed on their happy path only, so any exception mid-fetch
leaked the socket — and one early-return branch leaked even on success. A
`_imap_inbox()` context manager now handles it, with teardown failures swallowed
so a failing `close()` cannot mask the original error. Verified against a stubbed
socket across four paths: success, exception mid-fetch, a teardown that itself
raises, and missing credentials. The three fetchers also collapsed onto one
`_fetch_and_parse()` helper, removing the triplicated batch-fetch loop.

**`pending` no longer renders as "General" in the office view.** Three chained
ternaries each had `general` as their else-branch, so an email the classifier
never managed to label displayed as a confident verdict — the exact mislabel the
pending state exists to prevent. Replaced with one `LABEL_STYLE` lookup that has
an explicit `pending` entry (⏳, amber) and falls back to General only for
genuinely unknown labels. The confidence caption now reads "retrying shortly"
rather than attributing a percentage to a call that never succeeded.

**The backend was restructured into layers.** `api.py` was 1,100 lines holding
18 endpoints, every model, the scheduler and the exception handlers, with routes
querying Supabase directly. It is now ~50 lines of wiring over
`routes/` → `services/` → `repositories/` → `domain/`. Business rules are
reachable without an HTTP client, and repositories return dataclasses rather
than raw rows, so callers stop re-guessing the schema.

**A bad Supabase env var no longer kills the API.** Five modules each built a
client at import time; one lazy `core/db.py::get_db()` replaced them all. The
client is now created on first use — confirmed in the smoke test, where
"Supabase client initialised" appears on the first request rather than at
startup — so a credential problem fails one request as a 503 instead of taking
down endpoints that never touch the database.

**Configuration is one frozen object.** `os.environ.get(...)` appeared in ten
modules, several with different defaults for the same key, and `load_dotenv()`
in eight — some with a path, some without, so behaviour depended on the working
directory. `core/config.py` now reads the environment once, and
`backend/__init__.py` guarantees it loads before any leaf module.

**Dashboard label corrections work.** The payload the dashboard sends now
validates, and the server hydrates the email body from `email_id` rather than
trusting a client that was never given one. The UI also checks the response —
a rejected correction shows "⚠️ Not saved" instead of "✓ Corrected".

**Logging is configured in one place.** `basicConfig` was called from two
entrypoints, so whichever imported first won. `core/logging_config.py` is now
the single setup, with rotation, and it silences httpx/httpcore/urllib3 —
which is what removed the wall of Supabase URL noise from every log.
`classify_with_cache`'s per-email lines dropped to DEBUG behind one INFO
summary, which also now warns rather than informs when the batch had errors.

**`POST /process-email` deleted.** No callers, unauthenticated, and it burned a
gpt-4o call per request.

**The nightly report runs again, off Supabase instead of Gmail.** It had been
dying on `ImportError` every night since the connector rewrite. Rather than port
it to the new Gmail API, it now reads yesterday's rows from `emails` — which
ingest has already fetched, stored and labelled. Consequences: no mailbox
credentials (the `service_account.json` step is gone from the workflow, and with
it the domain-wide-delegation dependency that was being revoked), no second
classification pass with its own divergent 4-label prompt, and the report can no
longer disagree with the dashboard about what an email is. It also warns when
emails in the window are still `pending`, so a short report explains itself.
Verified end to end against the live database.

**Rate-card parsing and price prediction removed entirely.** An agent's reply is
now linked to the RFQ it answers and shown to the operator as the email it is.
That deleted a cluster of defects rather than fixing them one by one:

- **Invented transit times.** `QuotationDetails.transit_time_days` was a
  required `int` and the prompt said "use reasonable defaults", so a reply that
  never stated a transit time got a fabricated one, stored and rendered as
  though quoted.
- **Currency-blind price verdicts.** `assess_quotation` compared a bare number
  against a USD range with `parsed.currency` unused beside it, so an INR quote
  always read "above expected".
- **Approving one agent emailed them a rejection too.** `quotations` held one
  row per rate line, and the approve loop iterated rows rather than agents.
  Approval now takes the job reference — one job is one agent — and sends only
  an acceptance.
- **Unbounded parse input.** The whole email body, quoted history included, went
  to the model with no truncation and no `_strip_quoted`.
- **The parking queue is moot.** `quotations_unmatched` existed to hold parsed
  rates that couldn't be attributed. With no parsing, an unattributed reply is
  just an email with `rfq_reference IS NULL` — still visible in the inbox, no
  second surface needed.

**A failed email is retried instead of lost.** The scan claimed a row before
doing the work, so anything that threw was retired permanently. The claim is now
handed back on failure, capped by `emails.processing_attempts` (3) with the
error recorded in `processing_error`.

**`/email-body/{id}` distinguishes missing from broken.** A message stored in
neither Supabase nor Gmail now returns 404 with a human-readable reason; an
upstream provider failure returns 502. Previously both were a generic 500.

**`/health` added.** 200 when the API can reach Supabase, 503 when it cannot, so
an uptime monitor can alert on the status code alone. The body reports the
scheduler flag, whether a scan is in flight, and whether `EMAIL_REDIRECT` safe
mode is on.

**`/automation/run-now` no longer blocks or lies.** It dispatches to a thread
and returns 202, or 409 when a scan is already running. `ScanStats.status`
distinguishes `completed` / `already_running` / `disabled` / `error`, and only
completed runs are persisted — previously all three returned identical zeros and
the UI rendered "0 new" as though the inbox were empty.

## Fixed on 2026-08-06

- **Rate cards filed against arbitrary jobs.** The `open_jobs[:1]` fallback and
  its status flip are gone; attribution is by RFQ reference only.
- **RFQ references were case-sensitive.** The pattern is now case-insensitive
  and `extract_rfq_reference` returns the canonical stored form.
- **A late reply could reopen an approved job.** Status only advances if the job
  was already open.
- **`backend/app/main.py` could not run.** It iterated a dict as a list and
  called `generate_rfq_drafts` with a signature that no longer existed.
- **Dead auto-send code deleted.** `_process_customer_email` — 104 unreachable
  lines behind a hard `return None`.
