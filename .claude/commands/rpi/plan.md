# RPI: Plan Phase

You are in the **Plan** phase of the RPI workflow.

## Task
Create a detailed implementation plan based on prior research.

**Feature:** $ARGUMENTS

## Instructions

1. Read `reports/RESEARCH.md` — if it doesn't exist, tell the user to run `/rpi:research` first.

2. Read all **Affected Files** listed in the research document to understand current implementation.

3. Enter Plan Mode (`EnterPlanMode`) and produce a structured plan.

4. Write the plan to `reports/PLAN.md` with these sections:
   ```
   # Plan: <feature name>

   ## Overview
   What we're building and why.

   ## Architecture Decisions
   Key design choices made and reasoning.

   ## Implementation Phases

   ### Phase 1: <name> (Backend / Foundation / etc.)
   - [ ] Step 1: description — file: path/to/file.py
   - [ ] Step 2: description — file: path/to/file.py

   ### Phase 2: <name>
   - [ ] Step 1: description — file: path/to/file.tsx

   ### Phase 3: Validation
   - [ ] Test endpoint X manually
   - [ ] Verify UI shows Y
   - [ ] Confirm no race conditions

   ## Files to Modify
   | File | Change Type | Notes |
   |------|-------------|-------|
   | api.py | Add endpoint | POST /new-endpoint |

   ## Files to Create
   | File | Purpose |
   |------|---------|
   | new_module.py | Handles X |

   ## Risks & Mitigations
   Known risks and how to handle them.

   ## Out of Scope
   What we're explicitly NOT doing in this iteration.
   ```

5. After writing `reports/PLAN.md`:
   - Present the phase breakdown to the user
   - Highlight any risks
   - Ask if they want to adjust scope before implementation
   - Tell them to run `/rpi:implement` to proceed
