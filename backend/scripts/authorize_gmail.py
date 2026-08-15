"""
One-time Gmail authorization — run once per mailbox, no admin needed.

Two steps, run as separate commands so the mailbox owner can take their time:

    python -m backend.scripts.authorize_gmail url
        Prints a Google consent URL and saves the pending PKCE state.

    python -m backend.scripts.authorize_gmail exchange "<pasted url>"
        Exchanges the returned code for a refresh token.

The owner opens the URL on THEIR machine, signs in, clicks Allow — their
password never leaves their computer. Google then redirects to
http://localhost:8080/?code=... which shows a browser error (nothing is
listening there); that is expected. They copy the URL from the address bar and
send it back for the exchange step.

Prereqs in .env:
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
    GMAIL_MAILBOX             (the account to authorize)

The printed refresh token is a live credential to that mailbox. Treat it like a
password — nothing is written to disk except the short-lived PKCE state.
"""
import json
import logging
import os
import sys

from dotenv import load_dotenv

# The registered redirect URI is http://localhost:8080/. oauthlib refuses plain
# HTTP by default; localhost is the one case where Google permits it.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

from google_auth_oauthlib.flow import Flow  # noqa: E402

from backend.connectors.google_oauth import SCOPES, client_config  # noqa: E402
from backend.core.logging_config import configure_logging  # noqa: E402
from backend.core.paths import PROJECT_ROOT  # noqa: E402

load_dotenv()

configure_logging()  # interactive, one-shot: console only
logger = logging.getLogger(__name__)

REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8080/")
# Holds the PKCE verifier between the two steps. Not a credential on its own —
# useless without the auth code — but short-lived and gitignored anyway.
PENDING_FILE = PROJECT_ROOT / ".oauth_pending.json"


def _build_flow(state: str | None = None) -> Flow:
    flow = Flow.from_client_config(client_config(), scopes=SCOPES, state=state)
    flow.redirect_uri = REDIRECT_URI
    return flow


def _check_client_config() -> bool:
    missing = [
        name for name in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET")
        if not (os.getenv(name) or "").strip()
    ]
    if missing:
        logger.error("Set %s in .env first.", " and ".join(missing))
        return False
    return True


def cmd_url() -> int:
    """Print the consent URL and persist the PKCE state for the exchange step."""
    if not _check_client_config():
        return 1
    mailbox = (os.getenv("GMAIL_MAILBOX") or "").strip()
    flow = _build_flow()
    # access_type=offline + prompt=consent force a refresh token even on re-auth;
    # without them Google returns a 1-hour access token and nothing renewable.
    auth_url, state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    PENDING_FILE.write_text(json.dumps({
        "state": state, "code_verifier": flow.code_verifier, "mailbox": mailbox,
    }), encoding="utf-8")

    logger.info("\n" + "=" * 72)
    logger.info("Send this link to %s. Ask them to:", mailbox or "the mailbox owner")
    logger.info("  1. Open it and sign in as %s", mailbox or "(GMAIL_MAILBOX unset)")
    logger.info("  2. Click Allow")
    logger.info("  3. The browser will show a 'can't reach this page' error at")
    logger.info("     %s — THIS IS EXPECTED, not a failure.", REDIRECT_URI)
    logger.info("  4. Copy the FULL URL from the address bar and send it back")
    logger.info("=" * 72 + "\n")
    logger.info("%s\n", auth_url)
    logger.info("The code in that URL is single-use and expires in ~10 minutes.")
    logger.info("Then run:\n  python -m backend.scripts.authorize_gmail exchange \"<url>\"\n")
    return 0


def cmd_exchange(pasted: str) -> int:
    """Swap the returned auth code for a refresh token."""
    if not _check_client_config():
        return 1
    if not PENDING_FILE.exists():
        logger.error("No pending authorization. Run the 'url' step first.")
        return 1

    pending = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    flow = _build_flow(state=pending.get("state"))
    flow.code_verifier = pending.get("code_verifier")

    try:
        flow.fetch_token(authorization_response=pasted.strip())
    except Exception as e:
        logger.error("Token exchange failed: %s", e)
        logger.error("Codes expire in ~10 min and work once. Re-run the 'url' step.")
        return 1

    refresh_token = getattr(flow.credentials, "refresh_token", None)
    if not refresh_token:
        logger.error("Google returned no refresh token. Revoke prior access at "
                     "https://myaccount.google.com/permissions and re-run.")
        return 1

    PENDING_FILE.unlink(missing_ok=True)
    logger.info("\nAuthorized. Put this in .env (keep secret, never commit):\n")
    logger.info("GMAIL_REFRESH_TOKEN=%s\n", refresh_token)
    logger.info("Set GMAIL_MAILBOX to the account you just signed in as, or the "
                "connector will refuse to start.")
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "url"
    if command == "url":
        return cmd_url()
    if command == "exchange":
        if len(argv) < 3:
            logger.error('Usage: authorize_gmail exchange "<pasted redirect url>"')
            return 1
        return cmd_exchange(argv[2])
    logger.error("Unknown command %r. Use 'url' or 'exchange'.", command)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
