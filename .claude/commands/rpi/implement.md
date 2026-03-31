# RPI: Implement Phase

You are in the **Implement** phase of the RPI workflow.

## Task
Execute the implementation plan phase by phase, with validation at each step.

**Feature:** $ARGUMENTS

## Instructions

1. Read `reports/PLAN.md` — if it doesn't exist, tell the user to run `/rpi:plan` first.

2. Read all files listed under "Files to Modify" and "Files to Create" to understand current state.

3. **Implement phase by phase** — do not skip ahead:

   For each phase:
   a. Announce: "Starting Phase N: <name>"
   b. Make all changes for that phase
   c. Run validation (TypeScript check, syntax check, or manual test instructions)
   d. Report phase complete before starting next phase

4. **Project rules to follow during implementation:**
   - Backend (Python): type hints on all signatures, `logging` not `print`, AppException for errors, Pydantic models for request/response, keep functions under 50 lines
   - Frontend (TSX): all API calls via `API_BASE` constant, TypeScript interfaces for data shapes, inline styles only, no race conditions (use override params not setTimeout), update both list and totalEmails count for inbox state changes
   - API: port 8001, never raise HTTPException directly (use AppException), rfq_jobs uses `reference`, quotations uses `rfq_reference`

5. **After each file change**, check:
   - Does it follow project rules?
   - Does it break any existing functionality?
   - Is it under 50 lines per function?

6. **After all phases complete:**
   - Run `npx tsc --noEmit` in the frontend directory to check for TypeScript errors
   - List all files changed
   - Provide manual test steps for the user to verify
   - Update `reports/PLAN.md` checkboxes to mark completed steps
   - Note: restart backend with `kill python.exe && uvicorn api:app --host 0.0.0.0 --port 8001 --reload`
