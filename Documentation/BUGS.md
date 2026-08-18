# Known bugs

_Last reviewed: 2026-08-18. Scope: full backend + frontend._

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

### P1-2 · Tracebacks are never redacted
`SecretRedactingFilter.filter` rewrites `record.getMessage()` and nothing else.
`exc_text` does not exist yet at filter time — `Formatter.formatException`
produces it afterwards — so **every `logger.exception` call writes its traceback
verbatim**, past both the env-name and the shape-based matchers. Anything a frame
carries goes to disk: a Supabase URL with an embedded key, an SMTP password in a
`login()` argument, an API key in a provider client's repr. The same hole applies
to the JSON formatter's `extra` fields, which are also only message-redacted.

Silent by construction: the log looks redacted, because the *messages* are. The
recent conversion of ~31 handlers from `str(e)` to `logger.exception` was the
right call for debuggability and multiplied the volume of unredacted traceback
text by roughly the same factor.

**Consequence today:** `logs/` must be treated as containing secrets. Do not
attach a log file to a ticket or ship it to an aggregator without reading it.

**Fix:** redact in the `Formatter`, not the `Filter` — override `format()` /
`formatException()` and run the existing scrubber over the rendered result,
including `exc_text` and `stack_info`. A filter cannot do this; it runs too early.
Then extend the same pass over `extra`. Add a test asserting a raised
`RuntimeError("sk-live-…")` does not appear in the formatted output.

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

### P2-4 · No tests — *partly closed 2026-08-12, further 2026-08-16*
193 tests, run with `python -m pytest`, offline, in about a second.

Pure functions (2026-08-12): `extract_rfq_reference` / `inject_reference` /
`subject_token`, the classifier rule tiers and `_parse_llm_label`, `retry_utils`,
`domain/models` row parsing, the correlation-id contexts, secret redaction,
`ScanStats` persistence, and `_extract_country`.

Services against fake repositories, and the first HTTP-level tests (2026-08-16):
`tests/test_rfq_send.py` (14) covers the draft-send-record path including
per-agent outcome correlation; `tests/test_rfq_approve.py` (13) covers awarding
against a failed acceptance; `tests/test_inbox_routes.py` covers the six
outcomes of a body lookup through the route layer.

Still open: **`reply_service.link_reply` has no test** — the function that
decides which job a rate card attaches to, and the one place a wrong answer
silently attributes a vendor's rate to the wrong shipment. Most routes remain
uncovered. Also still no CI: nothing runs the suite on push.

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

### P2-7 · Four more unbounded reads, from the 2026-08-18 bounds audit
The audit that produced the four fixes below found these and left them open. They
are the same defect class — a limit that is absent, or attached to the wrong
quantity — ranked here by blast radius.

- **`GET /inbox?limit=` has no ceiling.** `limit: int = 20` goes straight into
  `.range(offset, offset + limit - 1)`, so `?limit=1000000` is a full-table read.
  Same at `/inbox/unlinked-rate-cards` (`limit: int = 50`), which also selects
  **bodies**. Sharpened by P1-1: there is no auth, so anyone who can reach the
  port can issue it. Fix is one line each — `Query(20, ge=1, le=200)`.
- **`list_replies_for()` has no limit and no pagination.** `.in_(...).order(...)`
  over `rfq_reference`, selecting bodies, bounded only by how many replies exist
  across the references passed. Grows monotonically with deployment age.
- **The attachment retry cap depends on a write allowed to fail silently.**
  `_download_pending_attachment` increments `attempts` and persists it inside
  `try/except: pass`. If that write fails the row stays `pending` with `attempts`
  unchanged, so the 2-minute worker retries it forever — `MAX_ATTACHMENT_ATTEMPTS`
  is only real if the bookkeeping succeeds.
- **`daily_report._fetch_emails(since, before)` drops its upper bound when
  `before` is `None`.** The `.lt("received_at", end)` filter is simply never
  applied — the optional-bound shape verbatim. Lowest severity of the four: it is
  still floored by `gte(since)`, and the only caller passes a day window.

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

## Fixed on 2026-08-18

All four came out of one audit, prompted by a single question: where else does a
bound quietly become *no* bound? The watermark runaway had a shape worth naming —
a limit expressed as an optional value, where "absent" silently meant "unlimited"
— and it was not the only instance. Each of these fails differently, which is why
the audit had to look for the shape rather than grep for a symptom. Pinned by
`tests/test_ingest_guardrails.py`.

**A message count with optional bounds counted the entire mailbox.**
`count_inbox_messages(after_epoch_s=None, before_epoch_s=None)` treated `None` as
"omit that side of the filter", so calling it bare built no `q` at all and summed
page lengths across all ~195k messages — about 390 sequential Gmail requests — to
produce a number nobody needed to be exact. This is the runaway's signature with
the safety net removed: `fetch_messages_since` at least stops at
`MAX_INCREMENTAL_FETCH`, while a function that only sums page lengths has nothing
to stop it. Latent rather than live — both callers always passed a day window —
but one bare call reproduced the incident. Both bounds are now required and `None`
raises. Both callers already know their window, so an unbounded count was never
the question being asked.

**The ingest ceiling was measuring the wrong quantity.** `MAX_INGEST_BATCH` (5000)
bounds mail we do *not* have, and the sweep loop broke on nothing else. A window
full of mail we already have therefore never tripped it: `unknown_ids` stayed at 0
while the pager walked every page. A watermark that read back fine but sat far in
the past — seeded long ago, edited by hand, a timezone slip — paged the whole
mailbox every five minutes, which is the runaway's cost profile arriving through a
bound that was counting the wrong thing. Now capped on ids *seen* too
(`MAX_SWEEP_IDS`, 20k ≈ 40 requests). Deliberately **not** via the existing
`truncated` flag: that suppresses the `_max_received_at()` promotion, and with 0
unknown ids that promotion is the only way a lagging watermark advances at all.
Suppressing it would re-page the cap every tick forever — a permanent stall, worse
than the single expensive-but-self-healing run it replaced.

**Two heal guardrails multiplied, and neither measured what the heal spends.**
`AUTO_HEAL_MAX_DAYS` (14) and `AUTO_HEAL_MAX_MISSING_PER_DAY` (1000) were checked
independently, so the pass condition was "≤14 days **and** ≤1000 each" — 14,000
mails cleared both while no limit looked at the product. Every one costs a Gmail
full-fetch and a real LLM classification (these ids are new by definition, so the
classification cache cannot spare you), unattended, on a 24-hour timer. Not
hypothetical either: what produces a dozen-plus short days at once is this
codebase's own deliberate `MAX_INGEST_BATCH` truncation, and `audit_sync_gaps`
widens its window precisely to notice that. Added `AUTO_HEAL_MAX_TOTAL_MISSING`
(2000), enforced twice — up front against the estimate, then as a running budget
charged for mail actually pulled. Twice because the two numbers are not the same
measurement: the audit's `missing` is a count subtraction (Gmail's count for the
day minus DB rows dated that day) while the backfill is a set difference over
`provider_msg_id`. Rows the DB holds for that day which Gmail no longer lists in
INBOX — archived, deleted, moved — inflate the DB count and shrink `missing`
without touching the set difference, so a day reporting 50 missing can hand back
950 unknown ids and sail past a 1000 guardrail that never saw a number above it.
`backfill_window` now takes `max_new` and clips at the point the real quantity is
finally known, keeping the newest ids so progress stays monotonic. Days skipped
when the budget runs out come back as `deferred`, not silently dropped.

**A startup job with no circuit breaker billed for every doomed batch.**
`backfill_classifications` ran 500 rows in chunks of 20 and had no check for a
wholly-failed batch — so with the LLM provider down or out of quota it worked
through all 25 chunks anyway. Its sibling `retry_pending_classifications` had had
that check all along; this one simply never got it. It compounds: `cached_ids`
deliberately excludes `method="error"` rows so a transient failure gets another
chance, which means a permanently unclassifiable email is retried on every boot,
forever. And it runs on **startup**, so `--reload` charged for it on every file
save. Now stops on the first batch where every row comes back `method="error"`,
matching its sibling — a whole chunk failing is a provider-level condition, not
something the next 24 chunks improve on.

Found by the same audit and **not** fixed — recorded so they are not re-derived:
`GET /inbox?limit=` and `/inbox/unlinked-rate-cards?limit=` have defaults but no
ceiling, so `?limit=1000000` is a full-table read (and the latter selects bodies)
— sharpened by P1-1, no auth; `list_replies_for()` has no `limit` and no
pagination at all; the attachment retry cap depends on an `attempts` write made
inside `try/except: pass`, so a failed bookkeeping write means the row retries
every two minutes forever; and `daily_report._fetch_emails(since, before)` drops
its upper-bound filter when `before` is `None` — the same optional-bound shape,
but still floored by `gte(since)` and called only with a day window.

---

## Fixed on 2026-08-16

**An unhandled 500 was the one event that could not be traced.** The catch-all
`@app.exception_handler(Exception)` in `app/errors.py` logged the traceback, but
FastAPI registers that handler on Starlette's `ServerErrorMiddleware`, which
`build_middleware_stack` places **outermost** — outside the `correlate`
middleware. The exception therefore propagated out of `with request_context(...)`,
resetting the contextvar, *before* the handler ran. Measured: `ctx=''` on the
traceback, no access line (it is emitted after `call_next` returns, and it never
returned), and no `X-Request-ID` on the response. The single line most worth
correlating was the only line in the system that carried no id — and the runbook's
"read the id off the response, then grep it" procedure silently did not apply to
crashes. Now `correlate` catches, logs with the traceback while the id is still in
scope, and re-raises; the catch-all only builds the response. Deliberately not
both, or every 500 would write two tracebacks — and tracebacks are unredacted
(P1-2), so duplicating them doubles the exposure. Residual: the 500 *response*
still has no `X-Request-ID`, because Starlette builds it past the point the
middleware can touch it. Match by timestamp and path.

**A deliberate failure returned its reason to the caller and logged nothing.**
`AppException` — the type every route raises — was turned into JSON without a log
line, as was `RequestValidationError`. The access line recorded the status, never
the reason, so a route raising `AppException(502, "postgrest upstream failed")`
left a `-> 502` in the log with no trace of what failed; the detail reached the
operator's browser and nowhere else, and a 422 was indistinguishable from a
request that never arrived. Both now log, split by level per the contract: 5xx at
ERROR (ours to fix), 4xx at WARNING (the caller's). A routine 404 does not deserve
ERROR — that level means a human must act.

**An inbound `X-Request-ID` could forge log structure.** It is the only
correlation id not minted here, and it was rendered verbatim into `ctx` on every
line of its request. Measured over real HTTP:
`X-Request-ID: x] [job=scan:FORGED email=victim@example.com` produced

    INFO [backend.app.api] [req=x] [job=scan:FORGED email=victim@example.com] GET /jobs/NOPE -> 404

— a job and an email field that nothing set, on a line otherwise
indistinguishable from a real one, corrupting exactly the audit trail the ids
exist to provide. ANSI escapes and tabs also passed through (colour in `tail` from
attacker input), and an 8192-char id was accepted and multiplied across every line
of its request, which against a 10 MB × 50 rotation is a cheap way to evict
history. h11 rejects a raw newline, so full forged lines are blocked by
uvicorn — but not necessarily by another proxy or ASGI server in front. New
`clean_incoming_id` accepts `[A-Za-z0-9._:-]{1,64}` and mints a fresh id for
anything else: rejecting rather than stripping keeps the guarantee the format
relies on, that a rendered `ctx` field was set by us. Legitimate ids still pass
through, which is the point of honouring the header at all.

All three found by cross-verification of the logging flow, and pinned by
`tests/test_error_logging.py` (20 tests). Negative control: reverting the three
fixes fails 7 of them, including
`assert 'FORGED' not in ' [req=x] [job=scan:FORGED email=victim@example.com] '`.

**A drafted RFQ was recorded as sent, whether or not the mail left.**
`send_rfqs` inserted the `rfq_jobs` row with `status="rfqs_sent"` hardcoded,
guarded only by "a draft exists and drafting did not raise" — a condition about
the *model*, not the *sender*. `send_rfq_emails_batch` was called before the
insert and its return value was used only for a count; per-agent outcomes were
discarded.

So a dead SMTP login, a rejected recipient, or `EMAIL_PROVIDER` misconfigured
produced a dashboard full of jobs apparently awaiting replies from agents who
were never contacted. Nothing later corrected it: replies attach by RFQ
reference, and no agent holds a reference that was never delivered, so no reply
could ever arrive to contradict the row. The failure mode presents as an
unresponsive vendor — the operator chases the agent, or quietly drops them from
future selections, over mail that never left this building.

Three parts to the fix:

- `_attach_send_status` records each draft's outcome on its own entry, **by
  position** rather than by `vendor_name`. That name comes from the model
  (`DraftEmail.vendor_name`) so it need not match the agent selected, and
  multi-office agents share one name (P2-1) — a name-keyed dict collapses two
  agents' outcomes into one. When the result count does not match the draft
  count, positions are untrustworthy, so every entry becomes `unconfirmed`
  rather than being guessed at.
- `_record_job` stores `rfqs_sent` only when the sender reported `sent`, and
  `send_failed` otherwise. `SENT_STATUS` is a single named constant, since
  "failed", "skipped", `batch_error: …`, `unconfirmed` and a missing `status` key
  all have to be treated identically.
- The row is still written on failure, deliberately. A send failure can be
  ambiguous — a timeout may have delivered — and dropping the row would strand
  any reply that does arrive. `send_failed` is therefore in
  `OPEN_JOB_STATUSES`, so such a job still accepts a reply and still appears in
  the operator's queue instead of vanishing.

`total_sent` in the response now counts confirmed sends rather than drafts handed
to the sender.

`tests/test_rfq_send.py` covers 14 outcomes. Restoring the hardcoded status alone
fails 8 of them with `assert ['rfqs_sent'] == ['send_failed']`, including the
mixed batch, the two same-named offices, the miscounted result list, and the
batch call raising.

**Approving an RFQ marked it awarded even when the acceptance email failed.**
`rfq_service.approve` called `job_repo.set_status(reference, "approved")`
unconditionally, outside the `try` wrapping the send, and hardcoded
`"status": "approved"` in the response. `send_rfq_email` returns
`{"status": "failed"}` rather than raising, so the ordinary failure — a dead SMTP
login — never even reached the handler. The job went to `approved`, the operator
was told it was approved, and the agent who won it was never informed.

Nothing later contradicted the record: replies attach by RFQ reference, and
`approved` is not in `OPEN_JOB_STATUSES`, so `link_reply` would not advance the
job even if the agent wrote in. The winning vendor waits for a booking
instruction that is not coming, and the desk believes the award is complete.

This is the same defect as the send-path one below, one function later, on the
step that commits the company to a vendor.

A failure now raises `RfqError` (422) and leaves the status untouched, rather
than recording a distinct status. Job statuses are enumerated in five places —
`OPEN_JOB_STATUSES`, `metrics_service.JOB_STATUSES`, the `metrics_snapshots`
columns, and two frontend status maps — so a sixth value would be uncounted in
metrics and render as unknown in the UI until all five were updated. Leaving the
job in its existing open status is also the truthful record: nobody has been told
anything and a reply can still land, so `approve` is safe to call again once the
sender is fixed. That is the operator's recovery path, and the error message says
so.

`tests/test_rfq_approve.py` covers 13 outcomes. Reverting the guard alone fails
10 of them with `Failed: DID NOT RAISE RfqError` — `failed`, `skipped`,
`unknown`, an empty status, a sender that raises, and a reply missing the
`status` key all previously wrote `approved`. `dashboard/page.tsx:435`'s
"Approved, but the acceptance email …" branch is now unreachable; the 422 renders
through line 438.

**Correlation ids were lost at every thread-pool boundary.** A contextvar is
per-thread, so anything handed to a `ThreadPoolExecutor` started with an *empty*
context and logged with no ids at all, however carefully the caller set them.
asyncio tasks and anyio's threadpool (the one running FastAPI's sync endpoints)
copy the context; a pool you create yourself does not.

This voided the audit trail `logging_config.py` documents. Its carve-out keeps
`email_classifier`'s rule lines at INFO because *"it is the only record of why an
email got its label … with `email_id` now stamped on the line, it is a usable
audit trail"* — but `classify_emails_batch` is a pool, so on the ingest path,
where nearly all classification happens, those lines carried no id. Five workers
interleaved `Classified by openai: general (85%)` with nothing to say which of
five emails each line meant.

`logging_context.carry_context` now wraps the worker at six pool sites
(`email_classifier`, `rfq_service` drafting, `email_store` attachments,
`gmail_connector` ×2, `backfill_3months`). It copies the four id *values* rather
than using `copy_context()` as the wrapper: a single `Context` may only be
entered once at a time, so N workers sharing one copy raise
`RuntimeError: cannot enter context`.

**Six of the seven scheduled jobs established no correlation id, and every one
of them logged `str(e)` with no traceback.** Only the scan set an id, and only
inside `run_scan`, so the handler above it did not get one either — ingest's
lines, the attachment worker's and a concurrent scan's all arrived with an empty
`ctx`, indistinguishable in a file they share with HTTP traffic. At two- and
five-minute cadences that uncorrelated output is the bulk of the log.

A single `_job()` wrapper in `lifespan.py` now supplies both, so a job added
later cannot forget either. A new `job_id` contextvar carries the job *name*
(`job=ingest:3f9a1c22`) because "which job is this?" is the first question asked
of such a line and a bare hex id does not answer it. Job error messages changed
text as a result: `"Scheduled scan error"` is now `"scan job failed"`.

**`POST /automation/run-now` severed the request→work chain.** The 202 carried an
`X-Request-ID` that appeared in no log line, because the scan ran in a new thread
with an empty context — leaving wall-clock time as the only way to match an
operator's complaint to the scan their click started. The thread target is now
wrapped in `carry_context`.

**Almost nothing logged a stack trace** — one `logger.exception` in the entire
backend, in `errors.py`. 31 handlers now use `logger.exception`; `str(e)` alone is
a bare key name for a `KeyError` and the empty string for a bare `RuntimeError`,
which is not enough to act on a job that failed unattended at 3am. Nine sites
were deliberately left as `logger.error`: each catches an already-diagnosed
condition (missing file, bad config, DNS NXDOMAIN, expired auth code, rejected
refresh token) or re-raises for someone else to log, and a traceback there is
noise.

**RFQ references drew from 65,536 values per day.** `new_reference` used
`uuid4().hex[:4]`, and the suffix is scoped to a single date, so at ~150 RFQs a
day there was a ~16% chance per day of minting a reference that already existed.
`rfq_jobs.reference` is `unique`, so a collision never misattributed anything —
it failed the insert *after* `send_rfq_emails_batch` had already delivered the
mail, leaving an RFQ with an agent and no job row for its reply to attach to,
which presents exactly like an agent who never answered. Now 8 hex characters.

`_CORE` in `rfq_reference.py` was pinned to `[a-f0-9]{4}`, so widening the
generator alone would have made every new reference unreadable. It now accepts
`{4,8}` — the 4-char form has to keep resolving because ~150 legacy references
are still out awaiting replies — with a trailing `(?![a-f0-9])` lookahead. The
lookahead is load-bearing: without it a 4-char core could be shaved off the
front of a longer hex run and resolve to a real-but-wrong job. Anything longer
than 8 now matches nothing, on the principle that failing to attribute is
recoverable and attributing to the wrong shipment is not.

**Every Gmail failure on `/email-body/{id}` reported the message as deleted.**
The 404 condition read `... == 404 or stored is None`, and control only reaches
that handler when `stored` was already falsy — `email_repo.get_body` returns
`None` for any message not yet persisted, which is the ordinary case for this
fallback. So the second clause was always true, the 502 branch was unreachable,
and an expired refresh token, a timeout, or a Gmail 500 all told the operator
the mail "may have been deleted or moved" — advice to stop looking, for the one
failure here a human can actually fix. Only Gmail's own 404 is a 404 now, and
`GmailReauthRequired` gets its own message naming re-consent as the remedy.

**No route had any test coverage.** `tests/test_inbox_routes.py` covers the six
outcomes of a body lookup. The four provider-failure cases and the reauth case
fail against the previous code with `assert 404 == 502`.

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
