# Claude Code Cheatsheet

## Daily Workflow
| When | Command |
|------|---------|
| Starting a new feature | `/plan` or `/rpi:research` → `/rpi:plan` → `/rpi:implement` |
| Writing new code | `/tdd` |
| Before finishing | `/code-review` or `/python-review` |
| End of session | `/learn` (saves patterns for next time) |
| Build is broken | `/build-fix` |
| Security concern | `/harness-audit` |

## Agents (Claude delegates automatically, or ask explicitly)
| Agent | Use for |
|-------|---------|
| `planner` | Breaking down features into steps |
| `architect` | System design decisions |
| `python-reviewer` | Backend code review (api.py, email_classifier.py) |
| `typescript-reviewer` | Frontend code review (page.tsx) |
| `security-reviewer` | Auth, SQL injection, secrets |
| `tdd-guide` | Writing tests first |
| `build-error-resolver` | Fixing pip/npm errors |
| `refactor-cleaner` | Cleaning up messy code |
| `code-reviewer` | General quality review |

## RPI Workflow (for larger features)
```
/rpi:research <feature>   → 3 agents research, writes reports/RESEARCH.md
/rpi:plan <feature>       → reads research, writes reports/PLAN.md
/rpi:implement <feature>  → implements phase by phase
```

## This Project's Rules
- Backend port: **8001** (8000 is occupied)
- Errors: use `AppException`, never `HTTPException` directly
- Frontend: all API calls via `API_BASE`, inline styles only
- Python: type hints on all functions, `logging` not `print`

## Email Classifier
Three rules short-circuit (internal domain / job-ref subject / rate-card cover
note); everything else is one LLM call via `llm_provider`. No training corpus,
no local models. Swap the model with `LLM_PROVIDER=openai|gemini` in `.env`.

```bash
# labels for the current inbox page
curl "http://localhost:8001/fetch-inbox?limit=20" | python -m json.tool
```
A label of `pending` means the LLM call failed and the 15-minute retry job will
re-run it — it is not a verdict.

## Useful Paths
- Commands: `~/.claude/commands/`
- Agents: `~/.claude/agents/`
- Skills: `~/.claude/skills/`
- Rules: `~/.claude/rules/`
