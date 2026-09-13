"""
agent_repo.py
-------------
Freight agents, from the Supabase `agents` table with the CSV as a fallback.

Two sources of truth exist and drift: the table is seeded from
`data/agents_database.csv` by `scripts/seed_agents_table.py`, and editing one
without re-running that leaves them inconsistent.

Both are one row **per office**, so a company with several branches appears
several times with different mailboxes — DP World 4x, ATC 3x, MSC 3x. Anything
keyed on `agent_name` alone cannot tell them apart, which is why
`email_for_name` refuses to guess rather than picking the first match.
"""

import csv
import logging
from dataclasses import dataclass, field

from backend.core.db import get_db
from backend.core.paths import AGENTS_CSV
from backend.domain.models import AgentContact

logger = logging.getLogger(__name__)


def list_all() -> list[AgentContact]:
    rows = get_db().table("agents").select("*").order("agent_name").execute().data or []
    return [AgentContact.from_row(r) for r in rows]


def list_all_raw() -> list[dict]:
    """Raw rows for the Send Request dropdowns, which need the row `id` the
    frontend uses as a selection key."""
    return get_db().table("agents").select("*").order("agent_name").execute().data or []


def load_csv() -> list[AgentContact]:
    """The CSV fallback. Used where the table may not be seeded yet."""
    agents: list[AgentContact] = []
    try:
        with open(AGENTS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                agents.append(AgentContact.from_row(row))
    except FileNotFoundError:
        logger.error("Agents CSV not found at %s", AGENTS_CSV)
    except Exception as e:
        logger.exception("Failed to load agents CSV: %s", e)
    return agents


def ensure_agents(recipients: list[dict], category: str = "MANUAL") -> int:
    """Insert any recipient whose email is not already in the agents table.

    Recipients are {agent_name, email}. Existing emails — DB agents the user
    picked, or a manual address entered before — are skipped, so this is how a
    hand-entered address gets remembered for next time. Returns the count added.
    Non-fatal: on any DB error it logs and returns 0 rather than blocking a send.
    """
    emails = [(r.get("email") or "").strip() for r in recipients]
    emails = [e for e in emails if e]
    if not emails:
        return 0
    try:
        rows = get_db().table("agents").select("email").in_("email", emails).execute().data or []
        existing = {(r.get("email") or "").strip().lower() for r in rows}
    except Exception as e:
        logger.warning("ensure_agents: existing-email lookup failed: %s", e)
        return 0

    to_insert, seen = [], set()
    for r in recipients:
        email = (r.get("email") or "").strip()
        lower = email.lower()
        if not email or lower in existing or lower in seen:
            continue
        seen.add(lower)
        to_insert.append({
            "agent_name": (r.get("agent_name") or email.split("@")[0]).strip(),
            "email": email,
            "category": category,
        })
    if not to_insert:
        return 0
    try:
        get_db().table("agents").insert(to_insert).execute()
        logger.info("ensure_agents: added %d new agent(s) from manual entry", len(to_insert))
        return len(to_insert)
    except Exception as e:
        logger.warning("ensure_agents: insert failed: %s", e)
        return 0


def email_for_name(agent_name: str) -> str:
    """Resolve a name to one address, or '' when that cannot be done safely.

    Returns empty for both "unknown" and "ambiguous". The caller turns that into
    a clear error — which is the right outcome, because the alternative is
    emailing the wrong branch of a multi-office agent.
    """
    if not agent_name:
        return ""

    try:
        rows = (
            get_db().table("agents").select("email")
            .eq("agent_name", agent_name).execute().data or []
        )
        if len(rows) == 1:
            return (rows[0].get("email") or "").strip()
        if len(rows) > 1:
            logger.warning("Agent %r has %d addresses on file — refusing to guess",
                           agent_name, len(rows))
            return ""
    except Exception as e:
        logger.warning("Agent lookup failed for %r: %s", agent_name, e)

    matches = [a for a in load_csv() if a.agent_name == agent_name]
    return matches[0].email if len(matches) == 1 else ""


@dataclass(frozen=True)
class CategoryIndex:
    """What kind of agent each mailbox belongs to: CHA, freight forwarder, carrier.

    A read model for display only. The category lives on `agents`, never on
    `rfq_jobs`, so an RFQ's type has to be resolved back through the roster at
    read time. Two consequences worth knowing before trusting the label:

    * It reflects the roster **now**, not at send time. Re-categorise an agent and
      every past RFQ of theirs re-labels with it. There is no per-job snapshot to
      compare against, so this is reported, not fixed.
    * A mailbox that has since been removed from the roster resolves to `""`.

    Email is the only exact key. `agents` is one row per *office*, so the same
    company appears several times under one name, and a name could in principle
    span two categories. `by_name` therefore holds only names that resolve to
    exactly one category; the ambiguous ones are dropped rather than guessed at,
    the same refusal `email_for_name` makes.
    """

    by_email: dict[str, str] = field(default_factory=dict)
    by_name: dict[str, str] = field(default_factory=dict)

    def category_for(self, agent_name: str, email: str = "") -> str:
        """This agent's category, or `""` when it cannot be known.

        Empty is a real answer and the caller must render it as unknown rather
        than as a default category. Six of the twenty-one RFQs on file resolve to
        empty today; all six went to QA addresses that were never roster agents.
        """
        addr = (email or "").strip().lower()
        if addr and addr in self.by_email:
            return self.by_email[addr]
        return self.by_name.get((agent_name or "").strip(), "")


def category_index() -> CategoryIndex:
    """Build the name/email to category lookup in one query.

    One read for a whole page of shipments rather than one per RFQ. Failure is
    non-fatal: an empty index labels every agent unknown, which degrades the
    display and breaks nothing.
    """
    try:
        rows = get_db().table("agents").select("agent_name, email, category").execute().data or []
    except Exception as e:
        logger.warning("Agent category lookup failed: %s", e)
        return CategoryIndex()

    by_email: dict[str, str] = {}
    names: dict[str, set[str]] = {}
    for r in rows:
        category = (r.get("category") or "").strip()
        if not category:
            continue
        email = (r.get("email") or "").strip().lower()
        if email:
            by_email[email] = category
        name = (r.get("agent_name") or "").strip()
        if name:
            names.setdefault(name, set()).add(category)

    return CategoryIndex(
        by_email=by_email,
        by_name={n: next(iter(c)) for n, c in names.items() if len(c) == 1},
    )
