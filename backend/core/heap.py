"""
heap.py
-------
Hand freed heap pages back to the operating system.

Why this exists: the attachment worker pulls whole files into memory (largest on
file is a 17 MB PDF) on several threads at once. glibc answers large
multi-threaded allocations by opening per-thread arenas, and it does not return
the freed pages on its own, so resident memory ratchets up for the life of the
process. Observed in production: 41 MB on a fresh start climbing to 829 MB over
4.6 days against a 1024 MB limit, with a two-day PLATEAU while the queue was
drained. That plateau is the tell. A leak that retained references would grow
with every tick; fragmentation only grows while there are bytes to move.

Nothing in the download path holds a reference (the pool is context-managed, the
futures dict is local, the bytes are local, and get_db() returns one cached
client), which is why the fix belongs at the allocator rather than in the code
that allocates.

No-ops safely on anything that is not glibc, so callers need no platform check:
macOS development machines and musl-based images simply get False.
"""

import ctypes
import ctypes.util
import logging

logger = logging.getLogger(__name__)

_trim = None
_resolved = False


def trim_heap() -> bool:
    """Ask the allocator to release free pages. True when the call was made.

    Resolution is attempted once and cached, including the failure: a machine
    without malloc_trim must not pay for a lookup on every batch.
    """
    global _trim, _resolved
    if not _resolved:
        _resolved = True
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
            fn = libc.malloc_trim
            fn.argtypes = [ctypes.c_size_t]
            fn.restype = ctypes.c_int
            _trim = fn
        except (OSError, AttributeError) as e:
            logger.info("malloc_trim unavailable (%s); heap trimming disabled", e)
            _trim = None
    if _trim is None:
        return False
    _trim(0)
    return True


def reset_for_tests() -> None:
    """Forget the cached lookup so a test can exercise both branches."""
    global _trim, _resolved
    _trim = None
    _resolved = False
