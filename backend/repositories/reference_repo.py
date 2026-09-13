"""
reference_repo.py
-----------------
Allocation of RFQ reference numbers from the `rfq_reference_seq` sequence.

Separate from job_repo because this is the only call in the codebase that runs a
Postgres function rather than reading or writing a table, and because it is the
one repository call that must be allowed to fail without failing its caller.

`allocate` returns None when the sequence is not reachable, and None is a
supported answer rather than an error. The sequence arrives in a migration the
operator runs by hand (sql/add_rfq_reference_sequence.sql), so there is a window
where this code is deployed and the function does not exist yet. Raising in that
window would break every send until someone opened the SQL editor; returning
None lets rfq_service fall back to the old random reference, which is still
unique and still attributable, and switch over on its own the moment the
migration lands.
"""

import logging
from typing import Any, Optional

from backend.core.db import get_db

logger = logging.getLogger(__name__)

_RPC = "next_rfq_references"

# The unavailable-sequence warning is worth seeing once per process, not once per
# send. A fallback that logs on every RFQ buries the line that says why.
_warned = False


def _warn_once(detail: Any) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning(
        "RFQ reference sequence unavailable (%s) — falling back to random "
        "references. Run sql/add_rfq_reference_sequence.sql to enable "
        "sequential numbering. This is logged once per process.", detail,
    )


def _coerce(data: Any) -> Optional[list[int]]:
    """The numbers out of whatever PostgREST hands back, or None if unrecognised.

    Two shapes are accepted because the function's return type decides which one
    arrives, and this code has to survive someone editing the migration:
    `returns bigint[]` gives a bare list of ints, `returns setof bigint` gives a
    list of one-key dicts. Anything else is treated as unavailable rather than
    guessed at — a mis-parsed allocation would mint a reference nobody can read
    back, which is the one failure this module exists to avoid.
    """
    if not isinstance(data, list):
        return None
    numbers: list[int] = []
    for item in data:
        if isinstance(item, bool):          # bool is an int subclass; not a number here
            return None
        if isinstance(item, int):
            numbers.append(item)
        elif isinstance(item, dict) and len(item) == 1:
            value = next(iter(item.values()))
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            numbers.append(value)
        else:
            return None
    return numbers


def allocate(count: int) -> Optional[list[int]]:
    """`count` fresh reference numbers, or None if the sequence is unavailable.

    One round trip for the whole batch. The numbers are guaranteed distinct by
    nextval(); they are not guaranteed contiguous, and nothing should assume it.
    """
    if count <= 0:
        return []
    try:
        response = get_db().rpc(_RPC, {"n": count}).execute()
    except Exception as exc:
        _warn_once(exc)
        return None

    numbers = _coerce(getattr(response, "data", None))
    if numbers is None or len(numbers) != count:
        # A short answer is as unusable as no answer: handing three references to
        # four agents would either send an RFQ with no reference or reuse one.
        _warn_once(f"asked for {count}, got {getattr(response, 'data', None)!r}")
        return None
    return numbers
