"""
auth_service.py
---------------
Login, and turning a verified token back into a user.

No FastAPI here, like every other service: the route in app/routes/auth.py maps
`AuthError` onto an AppException. That split is what lets the login rules be
tested without an HTTP client.

The security decisions all live in this file rather than in the route:

  * one error message for every failure mode, so the API never confirms which
    addresses have accounts;
  * a real bcrypt verify even when the account does not exist, so response time
    does not confirm it either;
  * /auth/me re-reads the row instead of trusting the token, so deactivating an
    operator takes effect on their next request rather than at token expiry.
"""

import logging
from functools import lru_cache

from backend.core import security
from backend.domain.models import AppUser
from backend.repositories import user_repo

logger = logging.getLogger(__name__)

# Every rejected login says exactly this. "No such user" versus "wrong password"
# turns the login route into an account-existence oracle for the whole company
# domain, and this deployment's usernames are work email addresses.
INVALID_CREDENTIALS = "Invalid email or password"


class AuthError(Exception):
    """A login or token check failed. Carries a caller-safe message only."""


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A hash of a value nobody can supply, used to burn the same ~250ms a real
    verify costs when the email does not exist.

    Without it, an unknown address returns in ~2ms and a known one in ~250ms, and
    that gap enumerates the user table over the public internet regardless of how
    carefully the message is worded. Built lazily and cached: doing it at import
    would add a bcrypt round to every process start, including the CLI scripts.
    """
    return security.hash_password("not-a-real-password-just-for-timing")


def login(email: str, password: str) -> tuple[str, int, AppUser]:
    """Verify credentials and mint a token.

    Returns (token, expires_in_seconds, user). Raises AuthError on any failure.
    """
    email = (email or "").strip().lower()
    user = user_repo.get_by_email(email) if email else None

    if user is None:
        security.verify_password(password or "", _dummy_hash())
        logger.warning("Login failed for %s: no such account", email or "(blank)")
        raise AuthError(INVALID_CREDENTIALS)

    if not security.verify_password(password or "", user.password_hash):
        logger.warning("Login failed for %s: wrong password", email)
        raise AuthError(INVALID_CREDENTIALS)

    if not user.is_active:
        # Same message as a wrong password, on purpose. A distinct "account
        # disabled" tells whoever is trying that the address is real.
        logger.warning("Login refused for %s: account is deactivated", email)
        raise AuthError(INVALID_CREDENTIALS)

    token, expires_in = security.mint_token(user.id, user.email, user.role)

    try:
        user_repo.touch_last_login(user.id)
    except Exception as exc:
        # Bookkeeping. A failure here must not cost the operator their login.
        logger.warning("Could not record last_login_at for %s: %s", email, exc)

    logger.info("Login ok for %s (role=%s)", email, user.role)
    return token, expires_in, user


def resolve_user(token: str) -> AppUser:
    """Verify a token, then re-read the operator behind it.

    The extra read is the point. Claims are signed and therefore trustworthy
    about *what was true at login*, which is not the same as true now: a role
    change or a deactivation would otherwise sit unenforced for up to
    JWT_EXPIRY_MINUTES.

    Used by /auth/me and /auth/change-password, not by the middleware — see
    app/auth.py for why the per-request path stays signature-only.
    """
    claims = _claims(token)

    user = user_repo.get_by_id(claims.user_id)
    if user is None:
        # A validly signed token for a row that no longer exists. Deleted
        # operator, or a token minted against a different database.
        logger.warning("Token for unknown user id %s", claims.user_id)
        raise AuthError("Account no longer exists")
    if not user.is_active:
        logger.warning("Token for deactivated user %s", user.email)
        raise AuthError("Account is deactivated")
    return user


def change_password(token: str, current_password: str, new_password: str) -> None:
    """Rotate an operator's own password.

    The current password is required even though the caller already holds a valid
    token: a token pulled out of a browser must not be enough to lock its owner
    out of their own account.

    Existing tokens stay valid afterwards, including any the thief holds. There is
    no denylist to add them to — noted in Documentation/BUGS.md rather than
    papered over here.
    """
    user = resolve_user(token)

    stored = user_repo.get_by_email(user.email)
    if stored is None or not security.verify_password(current_password or "",
                                                      stored.password_hash):
        logger.warning("Password change refused for %s: current password wrong",
                       user.email)
        raise AuthError("Current password is incorrect")

    try:
        new_hash = security.hash_password(new_password or "")
    except ValueError as exc:
        # The only place a specific message is safe: the caller is authenticated
        # and the complaint is about input they just typed.
        raise AuthError(str(exc))

    user_repo.set_password(user.id, new_hash)
    logger.info("Password changed for %s", user.email)


def _claims(token: str) -> security.TokenClaims:
    try:
        return security.decode_token(token)
    except security.TokenError as exc:
        raise AuthError(str(exc))
