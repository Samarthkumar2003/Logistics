"""
User-delegated Google OAuth credentials for the Gmail connector.

The no-admin auth path: the mailbox owner grants access to their own mailbox
once, in their own browser, and we hold the resulting refresh token. Needs no
Domain-Wide Delegation, no Workspace admin, and no service_account.json.

Env vars (see .env.example):
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
    GMAIL_REFRESH_TOKEN       <- produced by backend/scripts/authorize_gmail.py
    GMAIL_MAILBOX             <- expected account, verified on connect

The refresh token maps to exactly one mailbox. Pointing at a different mailbox
means re-running the authorize script signed in as that user.
"""
import logging
import os

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials

load_dotenv()

logger = logging.getLogger(__name__)

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Must stay in sync with the scopes granted at consent time. Adding one here
# without re-running the authorize script yields 403s on the new capability.
SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

REAUTH_HINT = (
    "Gmail refresh token rejected — the mailbox owner must re-authorise. "
    "Run: python -m backend.scripts.authorize_gmail"
)


class GmailReauthRequired(RuntimeError):
    """Refresh token is dead (revoked, or invalidated by a password change).

    Distinct from a transient network/API failure: no amount of retrying fixes
    it, and a human has to click a consent link before ingestion resumes.
    """


def oauth_configured() -> bool:
    """True when all three user-OAuth env vars are present."""
    return all(
        (os.getenv(name) or "").strip()
        for name in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
                     "GMAIL_REFRESH_TOKEN")
    )


def client_config() -> dict:
    """In-memory equivalent of a downloaded Web-application client_secret.json."""
    return {
        "web": {
            "client_id": (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip(),
            "client_secret": (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip(),
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "redirect_uris": [os.getenv("GOOGLE_OAUTH_REDIRECT_URI",
                                        "http://localhost:8080/")],
        }
    }


def build_user_credentials() -> Credentials:
    """Build refreshable user credentials from the stored refresh token.

    Raises ValueError naming any missing env var.
    """
    client_id = (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
    refresh_token = (os.getenv("GMAIL_REFRESH_TOKEN") or "").strip()

    missing = [
        name for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
            ("GMAIL_REFRESH_TOKEN", refresh_token),
        ) if not value
    ]
    if missing:
        raise ValueError(
            "User OAuth is not fully configured — missing: " + ", ".join(missing)
            + ". Run: python -m backend.scripts.authorize_gmail"
        )

    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
