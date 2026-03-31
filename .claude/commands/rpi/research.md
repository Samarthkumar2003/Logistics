# RPI: Research Phase

You are in the **Research** phase of the RPI workflow.

## Task
Research the following feature request thoroughly before any implementation begins:

**Feature:** $ARGUMENTS

## Instructions

1. Launch 3 research sub-agents **in parallel** to cross-verify information from multiple sources:
   - Agent 1: Search official documentation and API references
   - Agent 2: Search for best practices, common patterns, and pitfalls
   - Agent 3: Search for examples, tutorials, and real-world implementations

2. Read all relevant files in this codebase that relate to the feature:
   - Identify affected files and their current state
   - Understand existing patterns used in the project
   - Note constraints (port 8001, inline styles, AppException, etc.)

3. Synthesize findings into a `reports/RESEARCH.md` file with these sections:
   ```
   # Research: <feature name>

   ## Summary
   One paragraph overview of findings.

   ## Technical Approach Options
   List 2-3 viable approaches with pros/cons.

   ## Recommended Approach
   Which option to use and why.

   ## Affected Files
   List of files that will need changes.

   ## Key Constraints
   Project-specific constraints to respect.

   ## Open Questions
   Anything that needs clarification before planning.

   ## Sources
   URLs or docs consulted.
   ```

4. After writing `reports/RESEARCH.md`, tell the user:
   - What you found
   - Which approach you recommend
   - Any blockers or open questions
   - To run `/rpi:plan` when ready to proceed
