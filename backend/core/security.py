"""
security.py
-----------
Password hashing and token minting. Pure functions over strings — no FastAPI, no
Supabase — so the crypto is testable without a request or a database.

Two primitives, deliberately separate:

    bcrypt  stores a password. Slow on purpose; the work factor is baked into
            each hash, so raising BCRYPT_ROUNDS never invalidates old ones.
    JWT     carries an already-proven identity. Fast, stateless, and signed with
            JWT_SECRET so this API can trust it without a database round trip on
            every request.

The asymmetry matters: bcrypt runs once per login, the JWT check runs on every
one of the ~30 calls the dashboard makes on load.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from backend.core.config import settings

logger = logging.getLogger(__name__)

# bcrypt hashes the first 72 bytes of input and ignores the rest, silently. Left
# alone that is a real vulnerability rather than a quirk: two distinct long
# passwords sharing a 72-byte prefix both verify, so a password manager's 128-char
# output is effectively truncated without anyone being told. Rejecting is the only
# honest option — pre-hashing with SHA-256 to dodge the limit is the other common
# answer, but it changes the stored format and would break every existing hash.
BCRYPT_MAX_BYTES = 72

# Minimum on new passwords only. Enforced here rather than in the route so the
# CLI in backend/scripts/create_user.py cannot bypass it.
MIN_PASSWORD_LENGTH = 12


class TokenError(Exception):
    """A token was absent, malformed, expired, or signed with the wrong key.

    One type for all four on purpose. The distinction is useful in a log line and
    dangerous in a response body — "expired" versus "bad signature" tells someone
    probing the API which half of the token to keep working on.
    """


@dataclass(frozen=True)
class TokenClaims:
    """What a verified token asserts. Nothing here is read from the database."""

    user_id: str
    email: str
    role: str
    issued_at: datetime
    expires_at: datetime


def hash_password(plain: str) -> str:
    """Hash a new password. Raises ValueError on input bcrypt cannot store."""
    encoded = plain.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_BYTES:
        raise ValueError(
            f"Password is {len(encoded)} bytes; bcrypt silently ignores anything "
            f"past {BCRYPT_MAX_BYTES}, so it is rejected rather than truncated."
        )
    if len(plain) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time compare of a candidate password against a stored hash.

    Never raises. A malformed or empty stored hash — a row seeded by hand, a
    column left NULL — makes bcrypt raise ValueError, and letting that escape
    would turn a bad row into a 500 that reads as "the API is down" instead of
    "that login does not work". Returns False and logs instead.

    Truncation is NOT rejected here, unlike hash_password: refusing to verify a
    long password would lock out anyone whose hash predates that check.
    """
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        logger.error("Stored password hash is unusable: %s", exc)
        return False


def mint_token(user_id: str, email: str, role: str) -> tuple[str, int]:
    """Sign a token for an identity that has ALREADY been proven.

    Returns (token, seconds_until_expiry). The second value is what the frontend
    stores to decide when to bounce to /login without waiting for a 401.

    `jti` is a fresh uuid per token. Nothing revokes tokens today — there is no
    denylist — but without a jti two logins by the same user in the same second
    produce byte-identical tokens, and a revocation list added later would have
    no key to revoke on.
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.jwt_expiry_minutes)
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, int((expires_at - now).total_seconds())


def decode_token(token: str) -> TokenClaims:
    """Verify a token and return its claims. Raises TokenError on anything wrong.

    `algorithms=[...]` is a fixed list rather than read from the token's own
    header. Honouring the header is the classic JWT break: a caller sets
    `alg: none`, or swaps HS256 for a public-key algorithm, and the library
    verifies against attacker-chosen material.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise TokenError("Token expired")
    except jwt.InvalidTokenError as exc:
        # Logged with the reason, returned without it.
        logger.warning("Rejected token: %s", exc)
        raise TokenError("Invalid token")

    return TokenClaims(
        user_id=str(payload["sub"]),
        email=str(payload.get("email", "")),
        role=str(payload.get("role", "operator")),
        issued_at=datetime.fromtimestamp(payload.get("iat", 0), tz=timezone.utc),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )


def bearer_token(header_value: str | None) -> str:
    """Pull the token out of an `Authorization: Bearer <token>` header.

    Raises TokenError rather than returning None so every failure downstream of
    here is one exception type. The scheme compare is case-insensitive because
    RFC 7235 says it is, and some HTTP clients send `bearer`.
    """
    if not header_value:
        raise TokenError("Missing Authorization header")
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise TokenError("Authorization header must be 'Bearer <token>'")
    token = parts[1].strip()
    if not token:
        raise TokenError("Bearer token is empty")
    return token
