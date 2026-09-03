# 6. Deploying

Target: **Railway Pro**, two services off one repo — the FastAPI API (which also
owns the background scheduler) and the Next 16 dashboard.

Read [04-running-locally.md](04-running-locally.md) first. This doc only covers
what changes when the thing leaves your laptop.

---

## Before you start: set the safe-mode valve

```
EMAIL_REDIRECT=your.name@yourdomain.com
```

Every outgoing RFQ then goes to you, subject-prefixed `[TEST → real@vendor.com]`,
and `GET /automation/status` reports `safe_mode: true`. **Set it on the very first
deploy.** The scheduler starts scanning within seconds of boot, and the send path
talks to real freight companies.

Unset it deliberately, once, after you have watched a full RFQ round-trip.

---

## Why Railway and not Render

Neither platform's free tier can host this. The app runs seven repeating
APScheduler jobs in-process (plus four one-shot threads at boot), and both
Render's free spin-down and Railway's opt-in Serverless mode
wake a sleeping service on **inbound traffic only** — a timer that should have
fired at 03:00 does not fire, and nothing wakes it up to notice. The inbox ingest
simply stops. So this is a paid always-on service either way.

Given that, Railway wins on three things that actually apply here:

| | Railway Pro | Render |
|---|---|---|
| Background jobs on the plan you're paying for | Yes — a normal always-on service | Background Workers need a paid instance on top of the web service |
| Billing | Per-second usage against a $20 credit | Fixed per-instance per month |
| Cron minimum | 5 min | 5 min, paid plans only |

No persistent disk is needed on either — Supabase holds all state, and
`AUTOMATION_STATE_FILE` / `WHATSAPP_EXPORTS_DIR` are dead code. That removes
Render's disk penalty (a disk blocks zero-downtime deploys) from the comparison,
and it means neither service needs a volume.

**Egress is metered on Railway at $0.05/GB.** Attachment downloads flow
Supabase → API → browser, so a heavy rate-card day is the only line item likely
to move. It is cents, not dollars, at this desk's volume.

---

## 1. Supabase: same region as the app

Pick the Railway region that matches where the Supabase project already lives,
before creating any service. Every request in the hot path is a Supabase round
trip — the classifier reads and writes per email, the scan claims rows one at a
time — so a cross-continent hop is 150–250 ms multiplied by the number of
queries per scan, not per request.

Supabase's region is fixed at project creation and cannot be changed, so the
Railway region is the one that has to give. Check it under **Project Settings →
General** in Supabase, then set the matching region on each Railway service under
**Settings → Deploy → Region**.

## 2. Run the SQL

All the schema files in `sql/`, in the order given in
[04-running-locally.md § Database setup](04-running-locally.md#database-setup),
**plus the one this release adds**:

```
setup_app_users.sql                 app_users  ← required for login
```

It is idempotent and additive. Then seed the agents table:

```bash
python -m backend.scripts.seed_agents_table
```

## 3. Create the API service

New service → **Deploy from GitHub repo** → this repo, root directory left empty.
Railway finds the `Dockerfile` at the repo root and builds it. Nothing to
configure about the build.

Two settings that are not defaults:

- **Healthcheck path**: `/health`, timeout **120s**. The lifespan handler calls
  `check_connectivity()` synchronously — real network I/O against Supabase and
  the LLM provider — before the app accepts a single request, and then kicks off
  four one-shot startup threads that compete for CPU on a small container. The
  default 30s can fail a perfectly healthy cold boot.
- **Replicas: 1.** Not negotiable — see [§ Scaling past one replica](#scaling-past-one-replica).

`$PORT` needs nothing from you. Railway assigns a port per service and injects it;
the Dockerfile's `CMD` reads it (`--port ${PORT:-8001}`) and falls back to 8001
locally. The Docker `HEALTHCHECK` in the image is ignored by Railway, which polls
`healthcheckPath` over its own network instead — it is there for `docker run`.

## 4. Set the API's variables

Every variable `backend/core/config.py` reads. Grouped by how much thought each
one needs.

### Secrets — paste these in, never commit them

| Variable | Where it comes from |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → API |
| `SUPABASE_KEY` | the **service_role** key, not anon — the backend is the only client and it bypasses RLS |
| `OPENAI_API_KEY` | OpenAI dashboard |
| `EMAIL_ACCOUNT` | the mailbox that sends |
| `EMAIL_PASSWORD` | a Google **app password**, not the account password |
| `GOOGLE_OAUTH_CLIENT_ID` | GCP OAuth client (type: Web application) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | same client |
| `GMAIL_REFRESH_TOKEN` | `python -m backend.scripts.authorize_gmail`, run on a laptop |
| `JWT_SECRET` | generate per environment: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |

`JWT_SECRET` is checked at boot: `create_app()` raises `ConfigError` if it is
shorter than 32 characters, so a missing one fails the deploy rather than the
first login. Rotating it signs everyone out at once, which is the only way to
force that — there is no revocation list.

### The four that are easy to get wrong

| Variable | Value | Why it bites |
|---|---|---|
| `CORS_ORIGINS` | the frontend's exact public origin, e.g. `https://frontend-production-abcd.up.railway.app` | Scheme + host, no trailing slash, no wildcard. A browser matches `Origin` literally, so `http://` ≠ `https://` and a trailing `/` fails. Add `http://localhost:3000` only if you also point a local `npm run dev` at this API. |
| `GOOGLE_OAUTH_REDIRECT_URI` | **`http://localhost:8080/`** — leave it alone | It is **not** a runtime callback. The OAuth dance happens once, on a laptop, in `authorize_gmail.py`; the deployed API never redirects a browser to Google. The value only has to match the URI the refresh token was minted against. Point it at your deployed domain and every token refresh fails — all inbox ingest stops — with an `invalid_grant` / `redirect_uri_mismatch` that reads like anything but a config typo. |
| `RUN_SCHEDULER` | `1` on this service, and nowhere else, ever | The Dockerfile defaults it to `0` so you have to opt in. See [§ Scaling past one replica](#scaling-past-one-replica). |
| `EMAIL_REDIRECT` | your own address, until you mean it | Only honoured on the SMTP path. With `EMAIL_PROVIDER=outlook` it does nothing and real vendors get real RFQs while `/health` still reports safe mode as active. |

### The rest

| Variable | Set to | Notes |
|---|---|---|
| `ATTACHMENT_BUCKET` | `rate-card-attachments` | |
| `LLM_PROVIDER` | `openai` | or `gemini` |
| `OPENAI_MODEL` | `gpt-4o-mini` | |
| `EMAIL_PROVIDER` | `gmail_workspace` | |
| `SMTP_SERVER` | `smtp.gmail.com` | |
| `SMTP_PORT` | `587` | see [§ Outbound SMTP](#outbound-smtp-is-a-hard-requirement) |
| `IMAP_SERVER` | `imap.gmail.com` | unused on the `gmail_workspace` path, read at import |
| `GMAIL_MAILBOX` | the mailbox owner's address | the connector verifies the token actually opens this and fails loudly on a mismatch |
| `REPORT_RECIPIENT` | who gets the nightly Excel | |
| `SYNC_ALERT_RECIPIENT` | who gets the sync-drift alert | unset = the gap is logged and nobody is told |
| `AUTH_ENABLED` | `1` | |
| `JWT_ALGORITHM` | `HS256` | |
| `JWT_EXPIRY_MINUTES` | `720` | 12h — one login covers a shift |
| `BCRYPT_ROUNDS` | `12` | ~250 ms per verify on a small container |
| `SCAN_BATCH` | `50` | emails classified per scan pass |
| `STALE_SENDING_MINUTES` | `15` | |
| `AUTO_RETRY_STUCK_SENDS` | `1` | |
| `LOG_LEVEL` | `INFO` | |
| `LOG_JSON` | `1` | correlation ids become queryable fields |
| `LOG_TO_FILE` | `0` | a rotating file in a container dies with the container |
| `LOG_DIR` | leave unset | only read when `LOG_TO_FILE=1` |

`AUTH_ENABLED=0` turns the check off for the entire API. The offline test suite
sets it; a deployment must not. With it off, anything that can reach the port can
`POST /send-rfq` and mail real freight agents.

## 5. Create the frontend service

New service → same repo → **Root Directory: `frontend`**. Without the root
directory Railway builds the repo root, finds the API's Dockerfile, and deploys a
second copy of the API under the frontend's name.

- Build: `npm run build`
- Start: `npm run start`
- Node: **≥ 20.9.0**. Next 16 refuses to run on 18. `frontend/package.json`
  declares `engines.node`, so the builder should pick a correct version on its
  own; if it does not, set `NODE_VERSION=20`.

One variable:

```
NEXT_PUBLIC_API_BASE = https://<the api service's public domain>
```

No trailing slash.

### `NEXT_PUBLIC_API_BASE` is a build-time value

`next build` **inlines** `NEXT_PUBLIC_*` into the JavaScript bundle. It is not
read at runtime.

Two consequences, and both have bitten people:

1. It must be set **before the first build**. Set only at runtime, the literal
   fallback in `src/lib/api.ts` (`http://localhost:8001`) stays compiled in, and
   the deployed dashboard calls localhost from the operator's own browser.
2. Changing it requires a **redeploy**, not a restart. A restart reuses the same
   bundle with the same value baked in.

The symptom of getting this wrong is a dashboard where every request fails with a
CORS or connection error — which looks like a backend problem and is not.

Then go back and set the API's `CORS_ORIGINS` to this service's public URL. The
two services reference each other, so one of them has to be created first and
filled in second.

## 6. Bootstrap the first operator

There is no signup route, on purpose. Accounts are created from a terminal:

```bash
python -m backend.scripts.create_user --email you@yourdomain.com
```

It prompts for the password twice without echo, hashes with the same
`BCRYPT_ROUNDS` the API verifies with, and inserts the row. There is deliberately
no `--password` flag — a password passed as an argument lands in shell history,
in `ps` output, and in CI logs.

Run it against the **production** Supabase project: the same `SUPABASE_URL` /
`SUPABASE_KEY` the API uses, either from a local `.env` pointed at production or
via `railway run`:

```bash
railway run --service api python -m backend.scripts.create_user --email you@yourdomain.com
```

Confirm, without selecting the hash into a shared console:

```sql
SELECT id, email, role, is_active, created_at FROM app_users;
```

Then open `https://<frontend>/login`. Minimum password length is 12 characters,
enforced in `backend/core/security.py`; bcrypt silently truncates at 72 bytes, so
anything longer is rejected rather than quietly shortened.

## 7. Leave the GitHub Actions cron where it is

`.github/workflows/daily_report.yml` runs the nightly Excel report at 02:30 UTC
(08:00 IST) on GitHub's runners, with its own secrets. **Do not also add a
Railway cron for it.** Two schedules means two reports mailed to
`REPORT_RECIPIENT` every morning, and nothing in the report path is idempotent or
deduplicated — it reads the `emails` table and mails what it finds.

Add `REPORT_RECIPIENT`, `SUPABASE_URL`, `SUPABASE_KEY`, `OPENAI_API_KEY`,
`EMAIL_ACCOUNT` and `EMAIL_PASSWORD` to the repo's **Actions secrets** if they
are not there already. The workflow needs no mailbox OAuth — it reads the
database the ingest job already populated, so no service account and no
domain-wide delegation.

Railway cron would also be the wrong tool regardless: its minimum interval is
5 minutes and a cron service is billed as a service.

---

## Outbound SMTP is a hard requirement

`email_store._alert_sync_drift()` calls `smtplib` **directly**, with
`EMAIL_ACCOUNT` / `EMAIL_PASSWORD` on `SMTP_PORT`. It does not go through
`EMAIL_PROVIDER`, so the service running the scheduler needs outbound TCP 587
even on the `gmail_workspace` path where everything else uses the Gmail API.

Railway allows outbound 587. Worth knowing anyway, because the failure is quiet:
sync-drift alerts stop arriving, the gap is still logged, and nothing else
misbehaves.

## Scaling past one replica

The scheduler starts in the FastAPI **lifespan handler**, so every process that
boots the app gets its own copy of every background job — four one-shot threads
at boot, then seven repeating. That includes:

- `uvicorn --workers 3` → three schedulers
- three Railway replicas behind the proxy → three schedulers
- any separate worker or cron container that imports the app

Three schedulers scanning the same inbox on the same 5-minute cadence is *safe*:
the atomic claim in the scan means no email is processed twice. But the claim
happens **after** classification, so you pay OpenAI three times for identical
work — silently, indefinitely, and in proportion to how far you have scaled.

**Rule: `RUN_SCHEDULER=1` on exactly one process, `0` on every other.** Scale with
replicas rather than `--workers`, because `RUN_SCHEDULER` cannot tell one worker
of a process from another.

Forgetting `=0` costs money quietly. Forgetting `=1` is loud — nothing is
ingested, and the boot log says so:

```
INFO backend.app.lifespan RUN_SCHEDULER=0 — no background jobs in this process
```

which is why the Dockerfile defaults it to `0` and makes you opt in.

**So, to run more than one API replica:** add a second service off the same
Dockerfile with `RUN_SCHEDULER=1` and no public domain, set `RUN_SCHEDULER=0` on
the API service, and only then raise replicas. Splitting the scheduler into its
own process removes the footgun entirely; that is Phase 5 of
[05-hardening-plan.md](05-hardening-plan.md).

## Infrastructure as Code

`.railway/railway.ts` describes the topology above and is the reproducible way to
rebuild it.

```bash
npm install railway
railway link
railway config pull --force    # import what already exists
railway config plan            # read the whole diff
railway config apply
```

**`apply` deletes by omission** — one project definition, one apply, and anything
not named in the file is removed from the environment. If you built the project
through the dashboard, `pull --force` first and never skip `plan`. A plan that
proposes deleting a service or variable you did not touch means the file is stale,
not that the deletion is safe.

Note the file is TypeScript, not `railway.toml`. Config as Code is deprecated,
Railway stops reading `railway.toml` / `railway.json` on **2026-12-01**, and new
services cannot opt into it at all — a `railway.toml` added today is simply
ignored.

---

## What to check after the first deploy

```bash
curl https://<api>/health                    # 200, and 503 if Supabase is unreachable
curl https://<api>/fetch-inbox?limit=1       # 401 — auth is on
curl -X POST https://<api>/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"email":"you@yourdomain.com","password":"..."}'
```

A `401` from `/fetch-inbox` with no token is the check working. A `200` means
`AUTH_ENABLED` did not take.

Then in the browser, at `https://<frontend>/dashboard`:

- you should be bounced to `/login?next=%2Fdashboard`
- after signing in, land back on `/dashboard`
- the sidebar footer shows the signed-in address and a Sign out button

In the API logs, confirm the scheduler claimed its jobs:

```
INFO backend.app.api API ready — auth=True, scheduler=True, ...
```

and that the first scan ran — every line of one run carries `job=scan:<hex>`, so
`grep 'job=scan:'` isolates it. See
[04-running-locally.md § Logging](04-running-locally.md#logging-and-tracing-a-request).
