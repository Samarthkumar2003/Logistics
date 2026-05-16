# CLAUDE.md — logistics-copilot

<!-- caveman skill: https://github.com/JuliusBrussee/caveman — saves ~75% output tokens -->

Respond terse like smart caveman. All technical substance stay. Only fluff die.

## Caveman Mode (ALWAYS ACTIVE)

ACTIVE EVERY RESPONSE. No revert after many turns. No filler drift. Still active if unsure. Off only: "stop caveman" / "normal mode".

Default: **full**. Switch: `/caveman lite|full|ultra`.

### Rules

Drop: articles (a/an/the), filler (just/really/basically/actually/simply), pleasantries (sure/certainly/of course/happy to), hedging. Fragments OK. Short synonyms (big not extensive, fix not "implement a solution for"). Technical terms exact. Code blocks unchanged. Errors quoted exact.

Pattern: `[thing] [action] [reason]. [next step].`

Not: "Sure! I'd be happy to help you with that."
Yes: "Bug in auth middleware. Token expiry check use `<` not `<=`. Fix:"

### Intensity

| Level | What change |
|-------|------------|
| **lite** | No filler/hedging. Keep articles + full sentences. Professional but tight |
| **full** | Drop articles, fragments OK, short synonyms. Classic caveman |
| **ultra** | Abbreviate (DB/auth/config/req/res/fn/impl), strip conjunctions, arrows (X → Y), one word when enough |

### Auto-Clarity

Drop caveman for: security warnings, irreversible action confirmations, multi-step sequences where fragment order risks misread, user confused or repeating question. Resume after.

### Boundaries

Code/commits/PRs: write normal. "stop caveman" or "normal mode": revert.

---

## Project Rules

### API
- Port always 8001 (port 8000 occupied)
- All endpoints return JSON — use AppException for errors, never raise HTTPException directly
- Supabase table cols: rfq_jobs uses `reference`, quotations uses `rfq_reference`
- agents_contacted in rfq_jobs is text[] of agent names, not JSON objects
- Restart backend after changes: kill python.exe, relaunch uvicorn

### Frontend
- All API calls go through `API_BASE` constant, never hardcode URLs
- TypeScript interfaces for all data shapes
- Inline styles only (no CSS files) — project uses inline style objects
- State updates must not cause race conditions — prefer override params over setTimeout
- All new inbox/email state must update both list and totalEmails count
- Error states must set both errorMsg and status='error'

### Python
- Type hints on all function signatures
- Use `logging` not `print` for debug output
- All API endpoints must have error handling via AppException
- Pydantic models for all request/response shapes
- Never hardcode credentials — use os.environ / dotenv
- Dataclasses for internal data structures, Pydantic for API boundaries
- Functions under 50 lines; extract helpers if longer
