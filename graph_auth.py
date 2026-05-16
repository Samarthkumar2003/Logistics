"""
graph_auth.py
-------------
Acquires and caches Microsoft Graph API OAuth2 tokens using MSAL.
Used by outlook_connector.py and outlook_sender.py.

Token caching is handled automatically by MSAL's in-memory cache —
tokens are reused until ~1 minute before expiry, then refreshed.
"""

import logging
import os

import msal
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "")
_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "")

# Module-level singleton — one app per process lifetime.
# Lazily initialised on first call to get_graph_token() so that missing
# env vars only raise at call time, not at import time.
_msal_app: msal.ConfidentialClientApplication | None = None


def _get_msal_app() -> msal.ConfidentialClientApplication:
    global _msal_app
    if _msal_app is None:
        if not _TENANT_ID or not _CLIENT_ID or not _CLIENT_SECRET:
            raise ValueError(
                "MICROSOFT_TENANT_ID, MICROSOFT_CLIENT_ID, and "
                "MICROSOFT_CLIENT_SECRET must all be set when EMAIL_PROVIDER=outlook"
            )
        _msal_app = msal.ConfidentialClientApplication(
            client_id=_CLIENT_ID,
            client_credential=_CLIENT_SECRET,
            authority=f"https://login.microsoftonline.com/{_TENANT_ID}",
        )
        logger.debug("MSAL ConfidentialClientApplication initialised for tenant %s", _TENANT_ID)
    return _msal_app


def get_graph_token() -> str:
    """Return a valid Bearer token for Microsoft Graph API.

    MSAL's acquire_token_for_client() returns a cached token when still
    valid, and only calls the Azure token endpoint when the cached token
    is expired or absent.

    Raises
    ------
    ValueError
        If any required env var is missing.
    RuntimeError
        If MSAL fails to acquire a token (bad credentials, no consent, etc.).
    """
    app = _get_msal_app()
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

    if "access_token" not in result:
        error = result.get("error", "unknown_error")
        description = result.get("error_description", "No details available")
        raise RuntimeError(
            f"Microsoft Graph token acquisition failed [{error}]: {description}"
        )

    logger.debug("Graph token acquired/refreshed successfully")
    return result["access_token"]
