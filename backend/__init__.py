"""Logistics Copilot backend.

Importing anything under `backend.` loads `.env` first, via this import. That is
what makes the "one load_dotenv, from the repo root" rule hold even when a
script imports a leaf module directly — previously each module called
`load_dotenv()` itself, searching from the current working directory, so
behaviour depended on where you happened to run it from.
"""

from backend.core import config as _config  # noqa: F401  (import for side effect)
