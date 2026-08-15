# Logistics Copilot

AI assistant for a freight-forwarding desk: reads the shipping inbox, labels
every email, helps an operator turn customer enquiries into RFQs to freight
agents, and files the rate cards that come back — all through a web dashboard.

**New here? Start with [Documentation/](Documentation/README.md).**

| Doc | |
|---|---|
| [Architecture](Documentation/01-architecture.md) | What it does and how the three flows fit together |
| [Code tour](Documentation/02-code-tour.md) | Where everything lives, what to read first |
| [Data model](Documentation/03-data-model.md) | Supabase tables and the life of one email |
| [Running locally](Documentation/04-running-locally.md) | Setup and troubleshooting |
| [Known bugs](Documentation/BUGS.md) | Ranked defect register — read before changing anything |

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in secrets
```

Set `EMAIL_REDIRECT` in `.env` before running anything that sends — this system
emails real freight vendors.

```bash
./run_backend.sh                             # API on http://localhost:8001
cd frontend && npm install && npm run dev    # UI  on http://localhost:3000/dashboard
```
