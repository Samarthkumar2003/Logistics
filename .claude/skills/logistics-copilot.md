# Logistics Copilot — Project Skill

## When to Use
When working on any part of the logistics-copilot project (api.py, email_classifier.py, frontend).

## Project Context
A logistics company copilot that reads IMAP inbox, classifies emails (customer RFQ vs agent quotations), matches shipment history via pgvector, and presents everything in a Next.js dashboard.

## Architecture
- **Backend**: FastAPI on port 8001 (port 8000 is occupied by Antigravity)
- **Frontend**: Next.js + TypeScript on port 3000, inline styles only
- **Database**: Supabase (PostgreSQL + pgvector)
- **Email**: IMAP via imaplib, paginated with limit/offset

## Email Types
- `customer_requirement` — a customer asking for a shipping quote (RFQ)
- `quotation_rate_card` — an agent/forwarder in another country sending back rates
- `general` — everything else

## Classifier Architecture (4 tiers)
1. **Rules** (instant, free) — known agent emails CSV + RFQ regex + keywords
2. **Fine-tuned GPT-4o-mini** — trained on labeled sample, env: `CLASSIFIER_MODEL_ID`
3. **KNN pgvector** — cosine similarity against `email_training_data` table
4. **Few-shot fallback** — always available

## Key API Patterns
```python
# Errors — always AppException, never HTTPException
raise AppException(status_code=400, detail="message")

# Supabase table column names
# rfq_jobs: uses `reference` column
# quotations: uses `rfq_reference` column

# agents_contacted is text[] of agent names, not JSON
```

## Key Frontend Patterns
```typescript
// All API calls via constant
const API_BASE = 'http://localhost:8001';

// Inbox state must update both list AND count together
setEmails([...]);
setTotalEmails(data.total);

// Override params instead of setTimeout for race-condition-free state
async function loadInbox(reset = true, overrideSearch?: string) {
  const search = overrideSearch !== undefined ? overrideSearch : searchQuery;
}
```

## Database Schema
- `rfq_jobs` — customer shipment requests
- `quotations` — agent rate cards linked to RFQs
- `email_training_data` — labeled emails with `vector(1536)` embeddings
- `classification_feedback` — human corrections for retraining

## Pending Setup
1. Run `setup_classifier.sql` in Supabase Dashboard
2. `python build_training_data.py --sample 300` → `python train_classifier.py`
3. Backend restart: kill python.exe, relaunch uvicorn on port 8001
