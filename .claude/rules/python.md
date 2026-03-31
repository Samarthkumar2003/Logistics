---
glob: "**/*.py"
---

# Python Rules

- Use type hints on all function signatures
- Use `logging` not `print` for debug output
- All API endpoints must have error handling via AppException
- Pydantic models for all request/response shapes
- Never hardcode credentials — use os.environ / dotenv
- Use dataclasses for internal data structures, Pydantic for API boundaries
- Keep functions under 50 lines; extract helpers if longer
