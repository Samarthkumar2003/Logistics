---
glob: "api.py"
---

# API Rules

- Port is always 8001 (port 8000 is occupied by other processes)
- All endpoints return JSON — use AppException for errors, never raise HTTPException directly
- Supabase table columns: rfq_jobs uses `reference`, quotations uses `rfq_reference`
- agents_contacted in rfq_jobs is text[] of agent names, not JSON objects
- Always restart the backend after changes: kill python.exe and relaunch uvicorn
