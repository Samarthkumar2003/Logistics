"""
user_repo.py
------------
The `app_users` table — the only place operator credentials are read or written.

Schema in sql/setup_app_users.sql. Reached with the Supabase **service role** key
like every other repo here, which bypasses row-level security; the RLS policy on
the table exists to stop an anon key reading password hashes, not to constrain
this process.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from backend.core.db import get_db
from backend.domain.models import AppUser

logger = logging.getLogger(__name__)

# Every column except password_hash. Used by list/read paths so a hash cannot
# leak into a response by way of a `select("*")` someone added later.
_SAFE_COLUMNS = "id, email, full_name, role, is_active, created_at, last_login_at"


def get_by_email(email: str) -> Optional[AppUser]:
    """The login lookup. Includes password_hash — the one query that needs it.

    Email is lowercased here and stored lowercase (see the unique index on
    `lower(email)` in the migration), so `Sam@Corp.com` and `sam@corp.com` are one
    account rather than two, only one of which has a working password.
    """
    rows = (
        get_db().table("app_users")
        .select("*")
        .eq("email", email.strip().lower())
        .limit(1)
        .execute()
        .data
        or []
    )
    return AppUser.from_row(rows[0]) if rows else None


def get_by_id(user_id: str) -> Optional[AppUser]:
    """Re-read a user from a token's `sub`. No password_hash: nothing downstream
    of a verified token needs it, and /auth/me is called on every page load."""
    rows = (
        get_db().table("app_users")
        .select(_SAFE_COLUMNS)
        .eq("id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    return AppUser.from_row(rows[0]) if rows else None


def create(email: str, password_hash: str, full_name: str = "",
           role: str = "operator") -> AppUser:
    """Insert one operator. Raises on a duplicate email — the unique index is the
    guard, not a prior SELECT, so two concurrent creates cannot both succeed."""
    row = {
        "email": email.strip().lower(),
        "password_hash": password_hash,
        "full_name": full_name.strip(),
        "role": role,
    }
    inserted = get_db().table("app_users").insert(row).execute().data or []
    if not inserted:
        raise RuntimeError(f"Insert of user {email} returned no row")
    return AppUser.from_row(inserted[0])


def set_password(user_id: str, password_hash: str) -> None:
    get_db().table("app_users").update({"password_hash": password_hash}).eq(
        "id", user_id
    ).execute()


def touch_last_login(user_id: str) -> None:
    """Record a successful login. Deliberately best-effort: a failed write here
    must not turn a valid login into a 500, so the caller logs and continues."""
    get_db().table("app_users").update(
        {"last_login_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", user_id).execute()


def list_all() -> list[AppUser]:
    """Operators, without hashes. For an admin view; no route exposes it yet."""
    rows = (
        get_db().table("app_users")
        .select(_SAFE_COLUMNS)
        .order("email")
        .execute()
        .data
        or []
    )
    return [AppUser.from_row(r) for r in rows]
