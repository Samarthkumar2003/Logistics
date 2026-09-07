"""
railway_push_vars.py
--------------------
Push this project's environment variables to a Railway service.

Why a script rather than a row of `railway variable set` commands:

  1. Values go over the child process's STDIN (`railway variable set K --stdin`),
     so no secret is ever written into a command line, a shell history file, or a
     terminal transcript. This script prints key names, origins and lengths —
     never a value.
  2. `--skip-deploys` on every write. Without it Railway redeploys once per
     variable, and a service that boots midway through this list boots
     half-configured. The intended sequence is: set everything, THEN redeploy
     once, deliberately.
  3. The interlocks in `check()` run before the first write, so a deployment that
     would mail real freight agents fails here rather than at 02:30.

Usage
-----
    railway login                      # interactive; do this first
    railway link                       # pick the project + environment

    python scripts/railway_push_vars.py --service api --sync-alert-from-report \
        --set CORS_ORIGINS=https://frontend-production-xxxx.up.railway.app

`--set KEY=VALUE` is set verbatim and overrides the same key from .env. That is
how the one value .env cannot know gets in: the deployed frontend's origin, which
does not exist until the frontend service does.

`--sync-alert-from-report` fills the other gap. SYNC_ALERT_RECIPIENT has no
default anywhere — it used to be a hardcoded personal address, so every
deployment by anyone else mailed drift alerts to the original author — and without
it _alert_sync_drift logs a warning and sends nothing.

`--dry-run` runs every check and prints what would be written, touching nothing.

Then, once `railway variable list --service api` reads right:

    railway redeploy --service api
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger("railway_push_vars")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Values this file owns, because the default would be wrong ────────────────
#
# EMAIL_PROVIDER: config.py defaults this to "gmail", the legacy SMTP path, which
#   sends as EMAIL_ACCOUNT instead of GMAIL_MAILBOX. Replies then arrive in a
#   mailbox nothing ingests and every RFQ thread dead-ends — while each
#   individual send still reports success.
# RUN_SCHEDULER: exactly one process in the deployment may have this. The
#   scheduler starts in the FastAPI lifespan, so a second replica is a second
#   scheduler paying OpenAI twice for identical classifications.
# AUTH_ENABLED: the default is already True; pinned here so it is visible in
#   `railway variable list` rather than implied by absence.
# LLM_TPM: the pacer's ceiling (backend/classifier/rate_limiter.py), and it must
#   describe the KEY'S OWN tier rather than a budget: 30000 is Tier 1 gpt-4o. Set
#   too low it throttles work the account was entitled to run; too high it pages
#   through 429s, which is what the pacer exists to avoid. A literal rather than
#   .env because the local value paces a laptop that shares the key with nothing.
#   It is per process, so it is only true while RUN_SCHEDULER lives on one service.
# LOG_*: stdout is the log in a container. A rotating file lands on the overlay
#   filesystem and dies with the instance. JSON keeps the four correlation ids
#   (request_id / job_id / scan_id / email_id) queryable as fields.
LITERALS: dict[str, str] = {
    "EMAIL_PROVIDER": "gmail_workspace",
    "RUN_SCHEDULER": "1",
    "AUTH_ENABLED": "1",
    "LLM_TPM": "30000",
    "LOG_TO_FILE": "0",
    "LOG_JSON": "1",
    "LOG_LEVEL": "INFO",
}

# Copied from .env (or --set) as-is.
FROM_ENV: tuple[str, ...] = (
    "SUPABASE_URL", "SUPABASE_KEY",
    "OPENAI_API_KEY", "OPENAI_MODEL",
    "EMAIL_ACCOUNT", "EMAIL_PASSWORD", "IMAP_SERVER",
    "GMAIL_MAILBOX", "GMAIL_REFRESH_TOKEN",
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REDIRECT_URI",
    "EMAIL_REDIRECT", "REPORT_RECIPIENT", "SYNC_ALERT_RECIPIENT",
    "JWT_SECRET",
    "CORS_ORIGINS",
)

# Deliberately NOT pushed, each for its own reason:
#
#   LOG_DIR         — the local value is a Windows path (C:\ProgramData\...).
#                     Meaningless in a Linux container, and LOG_TO_FILE=0 makes
#                     the question moot.
#   MICROSOFT_*     — the Graph API path, read only when EMAIL_PROVIDER=outlook.
#   OUTLOOK_MAILBOX — same, and locally it is still the placeholder string.
#
# Everything else config.py reads is left unset on purpose, so the container takes
# the documented default: ATTACHMENT_BUCKET, LLM_PROVIDER, SMTP_SERVER, SMTP_PORT,
# JWT_ALGORITHM, JWT_EXPIRY_MINUTES, BCRYPT_ROUNDS, SCAN_BATCH,
# STALE_SENDING_MINUTES, AUTO_RETRY_STUCK_SENDS.
SKIPPED: tuple[str, ...] = ("LOG_DIR", "OUTLOOK_MAILBOX",
                            "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET",
                            "MICROSOFT_TENANT_ID")


class Refused(RuntimeError):
    """An interlock said no. Nothing has been written."""


def load(overrides: dict[str, str]) -> dict[str, str]:
    """.env, with --set values layered on top."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        raise Refused(f"no .env at {env_file}")
    values = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    values.update(overrides)
    return values


def sync_alert_from_report(values: dict[str, str]) -> str:
    """The FIRST address in REPORT_RECIPIENT, for use as SYNC_ALERT_RECIPIENT.

    First address, not the whole string, and that is not a simplification.
    REPORT_RECIPIENT is comma-separated by design — daily_report.py splits it —
    but email_store._alert_sync_drift passes its recipient to smtplib as
    `sendmail(account, [recipient], ...)`, a one-element list. Hand that a
    comma-separated string and the whole thing is treated as a single mailbox, so
    the alert fails to send at exactly the moment it is needed.
    """
    report = values.get("REPORT_RECIPIENT", "")
    first = next((p.strip() for p in report.split(",") if p.strip()), "")
    if not first:
        raise Refused("--sync-alert-from-report given, but REPORT_RECIPIENT is empty.")
    return first


def check(values: dict[str, str]) -> None:
    """Every reason not to deploy, checked before the first write."""
    # The safe-mode valve. Empty means every RFQ goes to the real freight company
    # in the agents table, and the scheduler starts scanning seconds after boot.
    if not values.get("EMAIL_REDIRECT"):
        raise Refused(
            "EMAIL_REDIRECT is empty. Set it to your own address before the first "
            "deploy — the scheduler sends real RFQs to real freight agents. "
            "Pass --set EMAIL_REDIRECT=you@yourdomain.com to override."
        )

    # require_auth_secret() rejects anything shorter at import time, but failing
    # here names the reason instead of leaving a container crash-looping.
    secret = values.get("JWT_SECRET", "")
    if len(secret) < 32:
        raise Refused(f"JWT_SECRET is {len(secret)} chars; create_app() needs >= 32.")

    # Not a runtime callback — it only has to match the URI the refresh token was
    # minted against. Pointing it at the deployed domain breaks every token
    # refresh, which is all inbox ingest.
    redirect_uri = values.get("GOOGLE_OAUTH_REDIRECT_URI")
    if redirect_uri != "http://localhost:8080/":
        raise Refused(
            "GOOGLE_OAUTH_REDIRECT_URI must stay http://localhost:8080/ in "
            f"production, not {redirect_uri!r}."
        )

    # A browser compares the Origin header literally, so a wildcard or a trailing
    # slash here is a frontend that cannot call the API at all.
    origins = values.get("CORS_ORIGINS", "")
    if not origins or "*" in origins:
        raise Refused("CORS_ORIGINS must be the frontend's exact origin, no wildcard.")
    if any(o.strip().endswith("/") for o in origins.split(",")):
        raise Refused("CORS_ORIGINS must not have a trailing slash.")

    # AUTH_ENABLED=0 disables the bearer check on every route, and this API sends
    # real mail and serves the whole agent contact database.
    if values.get("AUTH_ENABLED", "1").lower() in {"0", "false", "no", "off"}:
        raise Refused("AUTH_ENABLED is falsey. Never deploy with auth off.")

    absent = [k for k in FROM_ENV if not values.get(k)]
    if absent:
        raise Refused("no value for: " + ", ".join(absent))


def push(key: str, value: str, service: str, dry_run: bool) -> bool:
    """Set one variable, value over stdin. Returns True on success."""
    if dry_run:
        return True
    result = subprocess.run(
        ["railway", "variable", "set", key,
         "--stdin", "--skip-deploys", "--service", service],
        input=value, text=True, capture_output=True,
    )
    if result.returncode != 0:
        # stderr can echo the key name but not the value, which went over stdin.
        logger.error("  %s: %s", key, (result.stderr or result.stdout).strip()[:200])
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, help="Railway service name")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="override or supply one variable; repeatable")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every check and print the plan, write nothing")
    parser.add_argument("--sync-alert-from-report", action="store_true",
                        help="set SYNC_ALERT_RECIPIENT to the first address in "
                             "REPORT_RECIPIENT (see sync_alert_from_report)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    overrides: dict[str, str] = {}
    for pair in args.set:
        if "=" not in pair:
            logger.error("not a KEY=VALUE pair: %s", pair)
            return 64
        key, _, value = pair.partition("=")
        overrides[key.strip()] = value

    derived: set[str] = set()
    try:
        values = load(overrides)
        # Before check(), which requires every FROM_ENV key to have a value.
        # After the --set layer, so an explicit --set still wins.
        if args.sync_alert_from_report and "SYNC_ALERT_RECIPIENT" not in overrides:
            values["SYNC_ALERT_RECIPIENT"] = sync_alert_from_report(values)
            derived.add("SYNC_ALERT_RECIPIENT")
        check(values)
    except Refused as exc:
        logger.error("REFUSING: %s", exc)
        return 65

    logger.info("service: %s%s", args.service, "  (dry run)" if args.dry_run else "")
    logger.info("")

    def origin(key: str) -> str:
        if key in overrides:
            return "argv"
        return "report" if key in derived else ".env"

    failed: list[str] = []
    plan = [(k, v, "literal") for k, v in sorted(LITERALS.items())]
    plan += [(k, values[k], origin(k)) for k in FROM_ENV]

    for key, value, src in plan:
        if push(key, value, args.service, args.dry_run):
            logger.info("  ok    %-28s %-8s %d chars", key, src, len(value))
        else:
            logger.info("  FAIL  %-28s %-8s", key, src)
            failed.append(key)

    present_but_skipped = [k for k in SKIPPED if k in values]
    if present_but_skipped:
        logger.info("")
        logger.info("  not pushed (see SKIPPED): %s", ", ".join(present_but_skipped))

    logger.info("")
    if failed:
        logger.error("failed: %s — nothing deployed, re-run after fixing.",
                     ", ".join(failed))
        return 1

    logger.info(NEXT_STEPS, args.service, args.service)
    return 0


NEXT_STEPS = """All variables set, no deploy triggered. Next:

  railway variable list --service %s   # confirm, then
  railway redeploy --service %s        # one deliberate deploy

Once it is up, the check that actually proves auth is on:

  curl -s -o /dev/null -w '%%{http_code}\\n' https://<api-domain>/fetch-inbox?limit=1

401 is correct. A 200 means AUTH_ENABLED did not take, and anything that can
reach that port can POST /send-rfq to real freight agents."""


if __name__ == "__main__":
    sys.exit(main())
