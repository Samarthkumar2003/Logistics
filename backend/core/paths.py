"""Central filesystem paths for the logistics backend.

All on-disk artifacts (secrets, data files, generated state) live at the repo
root or in dedicated top-level folders. Modules import from here instead of
computing `__file__`-relative paths, which break when modules move between
sub-packages.
"""

from pathlib import Path

# backend/core/paths.py -> backend/core -> backend -> <repo root>
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

# Data + secrets (repo root / top-level folders)
DATA_DIR: Path = PROJECT_ROOT / "data"
SERVICE_ACCOUNT_FILE: Path = PROJECT_ROOT / "service_account.json"
AGENTS_CSV: Path = DATA_DIR / "agents_database.csv"
AUTOMATION_STATE_FILE: Path = PROJECT_ROOT / "automation_state.json"
WHATSAPP_EXPORTS_DIR: Path = PROJECT_ROOT / "whatsapp_exports"
