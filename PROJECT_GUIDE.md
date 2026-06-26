# Logistics Copilot — Project Guide

End-to-end AI assistant for a freight-forwarding desk. It reads the shipping
inbox, classifies each email, turns customer requirements into RFQs sent to
freight agents, parses the rate cards that come back, predicts a fair price, and
surfaces everything in a web dashboard. A nightly job emails a summary report.

---

## 1. The big picture

```
                         ┌──────────────────────────────┐
   Gmail / Outlook  ───► │  connectors/  (fetch + send)  │
                         └───────────────┬───────────────┘
                                         │ raw emails
                                         ▼
                         ┌──────────────────────────────┐
                         │  classifier/  (label email)   │  customer_requirement?
                         └───────────────┬───────────────┘  quotation_rate_card?
                                         │
                ┌────────────────────────┴────────────────────────┐
                ▼                                                  ▼
   customer_requirement                                 quotation_rate_card
                │                                                  │
                ▼                                                  ▼
   agents/intake_agent  (extract shipment)        agents/quotation_agent (parse rates)
   agents/history_agent (find similar past)       classifier/price_predictor (assess)
   agents/agents_lookup (pick freight agents)               │
   agents/rfq_agent     (draft RFQ emails)                  ▼
                │                                  stored as quotations,
                ▼                                  job status updated
   connectors/email_sender (send RFQs)
                │
                ▼
        rfq_jobs row in Supabase
```

The **automation/** loop runs this whole pipeline every 5 minutes. The
**app/api.py** FastAPI service exposes the same steps as HTTP endpoints for the
**frontend/** dashboard to drive manually.

---

## 2. Folder layout

```
logistics/
├── backend/                  Python package — all server code
│   ├── app/                  Entry points (run these)
│   │   ├── api.py            FastAPI service — the dashboard backend (port 8001)
│   │   ├── main.py           CLI one-shot pipeline over the inbox (dev/debug)
│   │   └── daily_report.py   Nightly Excel report emailer (GitHub Action)
│   ├── connectors/           Email I/O
│   │   ├── email_connector.py    IMAP inbox fetch (generic)
│   │   ├── gmail_connector.py    Gmail API via service-account delegation
│   │   ├── outlook_connector.py  Microsoft Graph inbox fetch
│   │   ├── graph_auth.py         MS Graph OAuth token
│   │   ├── email_sender.py       SMTP send
│   │   └── outlook_sender.py     Graph send
│   ├── agents/               Business logic ("agents")
│   │   ├── intake_agent.py       Email → structured ShipmentDetails
│   │   ├── history_agent.py      Semantic search over past shipments
│   │   ├── agents_lookup.py      Match shipment → freight agents (CSV + history)
│   │   ├── rfq_agent.py          Draft RFQ emails to agents
│   │   └── quotation_agent.py    Parse agent rate-card replies
│   ├── classifier/           Email classification + pricing
│   │   ├── email_classifier.py   Multi-tier email labeler (rules → SVM → KNN → LLM)
│   │   ├── classification_cache.py  Supabase-backed label cache
│   │   ├── llm_provider.py       LLM provider abstraction (OpenAI, etc.)
│   │   ├── price_predictor.py    Price prediction + quotation assessment
│   │   └── train_classifier.py   Train the fine-tuned classifier tier
│   ├── automation/
│   │   └── automation.py         5-minute end-to-end pipeline loop (APScheduler)
│   ├── core/                 Shared utilities
│   │   ├── paths.py              Central filesystem paths (secrets, data, state)
│   │   ├── port_normalizer.py    Normalize port/city names → canonical form
│   │   └── retry_utils.py        with_retry() helper for flaky API calls
│   ├── scripts/              One-off / maintenance scripts (data prep, seeding)
│   └── evals/                Classifier evaluation experiments
├── frontend/                 Next.js dashboard (TypeScript, inline styles)
│   └── src/app/
│       ├── page.tsx              Main inbox + job workflow UI
│       └── dashboard/page.tsx    Analytics dashboard
├── sql/                      Supabase schema + setup scripts
├── data/                     Static data (agents_database.csv)
├── docs/                     Design notes (qwen-mlp-classifier.md)
├── run_backend.sh            Launch the API on port 8001
├── service_account.json      Gmail service-account creds (gitignored)
├── automation_state.json     Processed-email IDs (gitignored, runtime state)
├── .env                      Secrets (gitignored) — see .env.example
└── requirements.txt
```

**Why this shape:** code that talks to the outside world (`connectors`), code
that makes decisions (`agents`, `classifier`), the thing that orchestrates them
(`automation`), and the things you actually launch (`app`) are each separated.
`scripts` and `evals` are not part of the running service — they are tools you
run by hand.

---

## 3. How to run it

All commands run **from the repo root** so `backend` resolves as a package.

```bash
# 0. one-time setup
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in secrets

# 1. backend API (port 8001 — 8000 is occupied)
./run_backend.sh
#   equivalently: uvicorn backend.app.api:app --port 8001 --reload

# 2. frontend (separate terminal)
cd frontend && npm install && npm run dev   # http://localhost:3000

# 3. one-shot CLI pipeline (debug, no server)
python -m backend.app.main

# 4. nightly report (also runs via .github/workflows/daily_report.yml)
python -m backend.app.daily_report
```

Data/maintenance scripts run the same way, e.g.:

```bash
python -m backend.scripts.ingest_training_data
python -m backend.scripts.fetch_gmail_history
python -m backend.evals.eval_compare_all
```

---

## 4. The request lifecycle (follow the code)

**Manual flow via the dashboard** (`frontend/src/app/page.tsx` → `api.py`):

1. `GET /fetch-inbox` → `connectors` pulls recent emails.
2. `POST /classify-inbox` → `classifier/email_classifier` labels them.
3. `POST /process-email` on a `customer_requirement`:
   - `agents/intake_agent` extracts the shipment,
   - `agents/history_agent` finds similar past jobs,
   - `agents/agents_lookup` picks freight agents,
   - `agents/rfq_agent` drafts RFQs,
   - `connectors/email_sender` sends them, and a `rfq_jobs` row is stored.
4. `POST /jobs/{reference}/check-quotations` → `agents/quotation_agent` parses
   replied rate cards; `classifier/price_predictor` scores them.
5. `POST /jobs/{reference}/approve` → finalizes the chosen quotation.

**Automatic flow** (`automation/automation.py`): the same steps, on a 5-minute
APScheduler loop, skipping email IDs already recorded in
`automation_state.json`. Toggle it via `POST /automation/toggle`.

---

## 5. Email classifier tiers

`classifier/email_classifier.py` labels each email as `customer_requirement`,
`quotation_rate_card`, `general`, or `skip` using a cascade:

| Tier | What | Always on? |
|------|------|-----------|
| 1 | Rule-based keyword/domain heuristics | yes |
| 2 | Fine-tuned SVM over embeddings | needs training data |
| 3 | KNN over `email_training_data` in Supabase | needs `sql/setup_classifier.sql` |
| 4 | LLM few-shot via `llm_provider` | yes (fallback) |

Check live status: `curl http://localhost:8001/classifier-status`. See
`docs/qwen-mlp-classifier.md` for the embedding experiments.

---

## 6. Conventions (enforced — see CLAUDE.md & .claude/rules/)

- **API**: port **8001**; every endpoint returns JSON; raise `AppException`,
  never `HTTPException`. Supabase columns: `rfq_jobs.reference`,
  `quotations.rfq_reference`. `agents_contacted` is `text[]` of agent names.
- **Python**: type hints on every signature; `logging` not `print`; Pydantic at
  API boundaries, dataclasses internally; functions under 50 lines; never
  hardcode credentials — use `os.environ` / dotenv. Paths come from
  `backend/core/paths.py`, not `__file__`-relative computation.
- **Frontend**: all calls go through the `API_BASE` constant; TypeScript
  interfaces for every data shape; inline styles only; inbox/email state updates
  must touch both the list and `totalEmails`.

---

## 7. External dependencies

- **Supabase** — Postgres + pgvector store for jobs, quotations, training data,
  embeddings. Schema lives in `sql/`.
- **OpenAI** (+ pluggable providers via `llm_provider`) — extraction,
  classification, pricing.
- **Gmail / Outlook** — inbox source and send channel. Gmail uses a
  service-account with domain-wide delegation (`service_account.json`).
- **GitHub Actions** — `.github/workflows/daily_report.yml` runs the nightly
  report at 02:30 UTC (08:00 IST).
