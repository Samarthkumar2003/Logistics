"""
create_user.py
--------------
Create an API operator, or reset one's password.

    python -m backend.scripts.create_user --email you@yourdomain.com
    python -m backend.scripts.create_user --email you@yourdomain.com --reset

The password is prompted for twice and never echoed, never passed as an argument,
and never logged. A `--password` flag is deliberately absent: it would land the
credential in shell history, in `ps` output, and in any CI log that runs with
command tracing on.

Run it against the same SUPABASE_URL the API uses — it hashes with BCRYPT_ROUNDS
from the same .env, so the hash it writes is one the API can verify.
"""

import argparse
import getpass
import logging
import sys

from backend.core import security
from backend.core.config import ConfigError, settings
from backend.repositories import user_repo

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _prompt_password() -> str:
    """Ask twice, compare, and re-ask on a mismatch or a too-short value.

    Loops rather than exiting so a typo does not cost the whole invocation — this
    is usually being run over SSH against production while somebody waits.
    """
    while True:
        first = getpass.getpass("Password: ")
        second = getpass.getpass("Confirm password: ")
        if first != second:
            logger.error("Passwords do not match — try again.\n")
            continue
        try:
            # Validated by hashing it, not by re-implementing the rules here. The
            # length floor and bcrypt's 72-byte ceiling live in one place.
            security.hash_password(first)
        except ValueError as exc:
            logger.error("%s\n", exc)
            continue
        return first


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create an API operator or reset a password."
    )
    parser.add_argument("--email", required=True, help="Login address.")
    parser.add_argument("--name", default="", help="Full name, for the UI.")
    parser.add_argument("--role", default="operator",
                        help="Goes into the JWT. No behaviour attached yet.")
    parser.add_argument("--reset", action="store_true",
                        help="Set a new password on an existing account.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    email = args.email.strip().lower()

    try:
        settings.require_supabase()
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2

    if not settings.auth_enabled:
        # Not fatal: the row is still valid and AUTH_ENABLED may simply be 0 in the
        # local .env. Worth saying out loud, because "I made a user and the API
        # still lets everyone in" is otherwise a confusing afternoon.
        logger.warning("Note: AUTH_ENABLED=0 in this environment — the API is not "
                       "checking tokens. The user will still be created.\n")

    existing = user_repo.get_by_email(email)

    if existing and not args.reset:
        logger.error("%s already exists. Pass --reset to set a new password.", email)
        return 1
    if args.reset and not existing:
        logger.error("%s does not exist — run without --reset to create it.", email)
        return 1

    password = _prompt_password()
    password_hash = security.hash_password(password)

    if args.reset:
        user_repo.set_password(existing.id, password_hash)
        logger.info("\nPassword reset for %s.", email)
        logger.info("Any token issued before now stays valid until it expires "
                    "(%d minutes) — there is no revocation list.",
                    settings.jwt_expiry_minutes)
        return 0

    user = user_repo.create(email=email, password_hash=password_hash,
                            full_name=args.name, role=args.role)
    logger.info("\nCreated %s (id=%s, role=%s).", user.email, user.id, user.role)
    logger.info("Log in at the frontend's /login, or:\n"
                "  curl -X POST $API/auth/login -H 'Content-Type: application/json' "
                "-d '{\"email\":\"%s\",\"password\":\"...\"}'", user.email)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Ctrl-C at a getpass prompt is the normal way to abandon this, and a
        # traceback makes it look like the script broke.
        logger.info("\nCancelled — nothing was written.")
        sys.exit(130)
