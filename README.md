# Logistics Copilot

AI assistant for a freight-forwarding desk: reads the shipping inbox,
classifies emails, generates RFQs to agents, parses returned rate cards,
predicts pricing, and serves it all through a web dashboard.

**New here? Read [PROJECT_GUIDE.md](PROJECT_GUIDE.md)** — architecture, folder
layout, and how to run everything.

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in secrets

./run_backend.sh                # API on http://localhost:8001
cd frontend && npm install && npm run dev   # UI on http://localhost:3000
```
