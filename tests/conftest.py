"""
Shared test setup.

The whole suite is offline by design. Nothing here opens a socket, so `pytest`
is safe to run on a plane, in CI without secrets, and against a production .env
without touching production.
"""

import os
import socket
import sys
from pathlib import Path

import pytest

# Running `python -m pytest` from the project root already puts it on sys.path;
# this makes `pytest` alone work too, from any directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# --- Auth defaults for the suite --------------------------------------------
#
# Set BEFORE anything imports backend.core.config, which reads the environment
# once at import and freezes it. conftest is the only file pytest guarantees runs
# first, so this is the only place it can go.
#
# A hard assignment, not setdefault: `load_dotenv()` does not override values
# already in os.environ, so this wins over a developer's real .env — which is the
# point. Without it, a .env with AUTH_ENABLED=1 would 401 every request in the
# twenty test files that predate auth and know nothing about tokens.
#
# tests/test_auth.py does not rely on this; it builds its app with
# `create_app(auth_enabled=True)` and asks for the check explicitly.
os.environ["AUTH_ENABLED"] = "0"
# Long enough to satisfy require_auth_secret(), and obviously not a real secret.
# Needed because test_auth.py mints and verifies real tokens with it.
os.environ.setdefault(
    "JWT_SECRET", "test-only-signing-key-not-a-secret-abcdefghijklmnop"
)
# The suite must never spend 250ms per bcrypt call. 4 is the library minimum and
# tests only care that a hash round-trips, not that it is expensive to crack.
os.environ.setdefault("BCRYPT_ROUNDS", "4")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly on any outbound connection.

    Not paranoia: the first run of this suite made a real OpenAI call, because a
    classifier test fell through the rules into the LLM path. It passed nothing
    and cost a fraction of a cent, but a test that silently spends money and
    needs a network is not a unit test. Now that path raises instead.
    """
    def blocked(*_args, **_kwargs):
        raise AssertionError(
            "This test tried to open a network connection. Unit tests run "
            "offline — stub the boundary, or move it to an integration suite."
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def no_sleep(monkeypatch):
    """Make retry backoff instant. A test that actually waits 1+2 seconds to
    prove a retry happened is a test people stop running."""
    slept: list[float] = []
    monkeypatch.setattr("backend.core.retry_utils.time.sleep", slept.append)
    return slept
