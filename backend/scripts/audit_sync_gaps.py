"""
audit_sync_gaps.py
------------------
Per-day reconciliation of Gmail INBOX vs the emails table. For each of the last
N days it counts what Gmail has and what we stored; any day where Gmail has more
is a gap (mail we failed to ingest — exactly the Jul 10-19 situation).

Usage:
    python -m backend.scripts.audit_sync_gaps            # last 14 days
    python -m backend.scripts.audit_sync_gaps --days 45  # custom window
"""
import logging
import sys

from backend.connectors.email_store import audit_sync_gaps
from backend.core.logging_config import configure_logging

configure_logging()  # read-only audit: console is enough
logger = logging.getLogger(__name__)


def main() -> None:
    days = next((int(sys.argv[sys.argv.index("--days") + 1])
                 for i, a in enumerate(sys.argv) if a == "--days"), 14)
    gaps = audit_sync_gaps(days=days)
    if not gaps:
        print(f"In sync — no gaps over the last {days} days.")
        return
    print(f"Found {len(gaps)} day(s) with missing mail:")
    print(f"{'day':<12} {'gmail':>7} {'db':>7} {'missing':>8}")
    for g in gaps:
        print(f"{g['day']:<12} {g['gmail']:>7} {g['db']:>7} {g['missing']:>8}")
    print("\nRun the ingest to heal: python -c \"from backend.connectors.email_store "
          "import ingest_new_emails; print(ingest_new_emails())\"")


if __name__ == "__main__":
    main()
