# Logistics Copilot — Documentation

An AI assistant for a freight-forwarding desk at Bhatia Shipping. It reads the
shipping inbox, labels every email, helps an operator turn customer enquiries
into RFQs sent to freight agents, and files the rate cards that come back.

**Read in this order:**

| # | Doc | What it answers |
|---|-----|-----------------|
| 1 | [01-architecture.md](01-architecture.md) | What the system does and how the three flows fit together |
| 2 | [02-code-tour.md](02-code-tour.md) | Where everything lives, and what to read first |
| 3 | [03-data-model.md](03-data-model.md) | The Supabase tables, and the life of one email |
| 4 | [04-running-locally.md](04-running-locally.md) | Getting it running on your machine |
| 5 | [05-hardening-plan.md](05-hardening-plan.md) | Phased plan to restructure, fix logging, and productionise |
| — | [BUGS.md](BUGS.md) | Known defects, ranked. **Read before changing anything.** |
| — | [archive/](archive/) | Superseded reviews, kept for history |

---

## The 60-second version

There are **two things called "agents"** in this codebase and it trips
everyone up:

- **Freight agents** — real vendor companies (carriers, CHAs, forwarders) that
  quote you a price. They live in `data/agents_database.csv` and the Supabase
  `agents` table.
- **AI agents** — the modules in `backend/agents/`. Ordinary Python, one
  LLM call each. No framework, no tool-calling loop, no autonomy.

Nothing in this system sends an email to a vendor without a human pressing
Send. That is deliberate and load-bearing — see
[01-architecture.md § The human gate](01-architecture.md#the-human-gate).

## Current state, honestly

Working and in daily use:

- Gmail → Supabase ingest, incremental and idempotent
- Email classification (3 rules, then one LLM call), cached per email
- The dashboard: inbox, customer requests, rate cards, shipments
- The Send Request flow: extract → draft → **edit** → send, one RFQ reference per agent
- The 5-minute scan, which links rate-card replies to the RFQ they answer
- The request page: the customer's email, and every agent's reply in full

- The nightly Excel report, off the email store rather than Gmail

Known broken or unfinished:

- Dashboard label corrections silently 422 and show "✓ Corrected" anyway
- `frontend/src/app/page.tsx` (the "office view") is largely dead animation code
- Test coverage is partial: 146 tests cover the reference matcher, the classifier
  rules, logging, retry, and the body-lookup route — but no service or repository
  has any, and nothing runs them on push

All of it is written up with severities in [BUGS.md](BUGS.md).

## Things that were removed (so you don't go looking)

| Removed | Why |
|---|---|
| `quotation_agent.py` — LLM rate extraction | Invented transit times its schema forced it to produce; the operator reads the reply instead |
| `price_predictor.py` — estimates and verdicts | Graded every quote against a USD range regardless of currency, on a guess with no historical basis |
| `history_agent.py` + pgvector shipment search | Ranked agents by past shipments; agent choice is now fully manual |
| Fine-tuned / SVM / KNN / Qwen-MLP classifier tiers | The LLM alone was accurate enough; the corpus cost more than it returned |
| `train_classifier.py`, training-data scripts, `email_training_data` | Same |
| numpy, scikit-learn, sentence-transformers, torch | Nothing imports them any more |

The `quotations` table survives but is dead — see
[03-data-model.md](03-data-model.md#tables). Git history has all the code if you
need it back.
