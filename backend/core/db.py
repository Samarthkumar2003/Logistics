"""
db.py
-----
The Supabase client, created lazily and shared.

Previously five modules each did `create_client(...)` at import time. With an
empty or wrong `SUPABASE_URL` that raises *during import*, which killed the
whole API — including endpoints that never touch the database. It also meant a
lot of defensive plumbing: imports buried inside function bodies purely so a
module wouldn't be imported until it was unavoidable.

One lazy accessor fixes both. A credential problem now fails the request that
needs the database, as a clean 503, and every other endpoint keeps working.

    from backend.core.db import get_db
    rows = get_db().table("emails").select("id").execute().data
"""

import logging
import socket
import threading
from typing import Optional

from supabase import Client, create_client

from backend.core.config import ConfigError, settings

logger = logging.getLogger(__name__)

_client: Optional[Client] = None
_lock = threading.Lock()


def get_db() -> Client:
    """Return the shared Supabase client, building it on first use.

    Raises ConfigError when credentials are missing — callers at the HTTP
    boundary translate that into a 503 rather than a 500, because it is a
    deployment problem, not a bug.
    """
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            settings.require_supabase()
            _client = create_client(settings.supabase_url, settings.supabase_key)
            logger.info("Supabase client initialised")
    return _client


def reset_db() -> None:
    """Drop the cached client. For tests — lets a fake be installed between cases."""
    global _client
    with _lock:
        _client = None


def check_connectivity() -> None:
    """Log a clear diagnosis for the two failures that look identical from the
    inside: unset credentials, and a project URL that no longer resolves.

    Non-fatal by design. Call it at startup for the operator's benefit; it must
    never stop the process, because a DNS blip is not a reason to refuse to boot.
    """
    try:
        settings.require_supabase()
    except ConfigError as e:
        logger.error("%s", e)
        return

    host = settings.supabase_url.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror:
        logger.error(
            "Supabase host %r does not resolve (DNS NXDOMAIN). The project may be "
            "deleted, or SUPABASE_URL is wrong — copy Project URL + service_role "
            "key from Supabase Dashboard → Settings → API.",
            host,
        )
