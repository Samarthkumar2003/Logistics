"""
logging_config.py
-----------------
One place that configures logging. Call `configure_logging()` from an entrypoint
and nowhere else.

Why this exists:

  * `logging.basicConfig()` was being called from two entrypoints, so whichever
    imported first won and the other's format was silently ignored.
  * httpx logs every Supabase REST call at INFO. A single scan produced hundreds
    of lines of URL noise that buried the application's own messages.
  * Nothing rotated. backend.log / uvicorn.log / watchdog.log were shell
    redirects that grew without bound.

Log-level contract:

    ERROR    a human must act            job persist failed after a send succeeded
    WARNING  degraded but self-healing   LLM rate-limited; reply left unlinked
    INFO     state changes worth auditing RFQ sent, reply linked, job approved
    DEBUG    per-item detail             cache hit/miss, cache lookups

One deliberate exception: `email_classifier` logs the winning rule per email at
INFO, not DEBUG. It reads like per-item detail, but it is the only record of
*why* an email got its label — and a misclassification is the failure mode that
sends a rate card to the wrong desk. With `email_id` now stamped on the line, it
is a usable audit trail. Keep it.
"""

import logging
import logging.handlers
import os
import re
from pathlib import Path
from typing import Iterable, Optional

from backend.core.config import settings
from backend.core.logging_context import CorrelationFilter
from backend.core.paths import PROJECT_ROOT

# Libraries that narrate every network call. Silencing these is the single
# highest-value line in this module: it is the difference between logs you read
# and logs you scroll past.
_NOISY = (
    "httpx",
    "httpcore",
    "hpack",
    "urllib3",
    "openai._base_client",
    "apscheduler.executors.default",
    "apscheduler.scheduler",
)

# %(ctx)s is supplied by CorrelationFilter and is empty when no ids are in
# scope, so ordinary startup lines stay clean.
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s]%(ctx)s %(message)s"
_JSON_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s %(message)s "
    "%(request_id)s %(scan_id)s %(email_id)s"
)
# Rotation budget. 10 MB × 50 files ≈ 510 MB worst case on disk, which at the
# current volume (a few MB a day) is roughly a year of history — long enough to
# answer "what did we send that agent last quarter?" without a log store.
# Deliberately generous: rotation deletes the OLDEST file, so a small backup
# count silently destroys exactly the history an audit would ask for.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 50

# Environment variables whose VALUES must never reach a log line. Matched by
# name, so a credential added later is covered without editing this file.
_SECRET_NAME = re.compile(r"(SECRET|PASSWORD|TOKEN|_KEY|APIKEY|CREDENTIAL)", re.I)

# Recognisable credential shapes, for secrets that arrive in a message without
# coming from this process's environment — an error string echoing a request
# header, say. Deliberately narrow: a loose pattern that redacts an RFQ
# reference would make the logs useless.
_SECRET_SHAPE = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9_\-]{20,}"          # OpenAI
    r"|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"  # JWT (Supabase)
    r"|ya29\.[A-Za-z0-9_\-]{20,}"      # Google access token
    r"|gh[pousr]_[A-Za-z0-9]{30,}"     # GitHub
    r")"
)
_MASK = "***REDACTED***"

_configured = False


def configure_logging(
    level: Optional[str] = None,
    log_file: Optional[Path] = None,
    json_output: Optional[bool] = None,
) -> None:
    """Set up root logging. Idempotent — a second call is a no-op, so importing
    two entrypoints in one process cannot clobber the first one's setup."""
    global _configured
    if _configured:
        return

    level = (level or settings.log_level).upper()
    json_output = settings.log_json if json_output is None else json_output

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):  # drop anything basicConfig left behind
        root.removeHandler(handler)

    formatter: logging.Formatter
    if json_output:
        formatter = _json_formatter()
    else:
        formatter = logging.Formatter(_CONSOLE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    # The filters go on the HANDLERS, not the root logger: a filter attached to
    # a logger is not applied to records that propagate up from child loggers,
    # so records from backend.services.* would arrive without the ids — and,
    # worse for the redactor, unredacted.
    filters = (CorrelationFilter(), SecretRedactingFilter())

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    for f in filters:
        console.addFilter(f)
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        for f in filters:
            rotating.addFilter(f)
        root.addHandler(rotating)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
    logging.getLogger(__name__).debug("Logging configured at %s", level)


def collect_secrets(environ: Optional[dict] = None) -> list[str]:
    """Values from the environment that must never appear in a log line.

    Name-based rather than an explicit list, so a credential added to .env next
    month is covered without anyone remembering to update this module.

    Short and purely numeric values are skipped: `SMTP_PORT=587` matches the
    name pattern, and redacting every "587" in the logs would be worse than the
    problem being solved.
    """
    env = os.environ if environ is None else environ
    found = {
        value for name, value in env.items()
        if _SECRET_NAME.search(name) and value and len(value) >= 8 and not value.isdigit()
    }
    # Longest first, so a secret that contains another is masked whole.
    return sorted(found, key=len, reverse=True)


class SecretRedactingFilter(logging.Filter):
    """Removes credentials from log lines.

    This exists because `daily_report` logged the first 8 characters of
    OPENAI_API_KEY on every run, straight into GitHub Actions output. It was
    removed, but nothing stopped it coming back.

    Catches two cases: a known environment value appearing anywhere in a
    message, and an unknown-but-recognisable credential shape. It cannot catch a
    deliberately truncated secret (`key[:8]` is not a substring match against
    anything) — the CI grep in `.github/workflows/ci.yml` is the layer for that.
    """

    def __init__(self, secrets: Optional[Iterable[str]] = None):
        super().__init__()
        self._secrets = list(secrets) if secrets is not None else collect_secrets()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True  # a broken format string is the formatter's problem

        original = message
        for secret in self._secrets:
            if secret in message:
                message = message.replace(secret, _MASK)
        message = _SECRET_SHAPE.sub(_MASK, message)

        if message != original:
            # Collapse to the redacted string: leaving args in place would let
            # the formatter re-expand the secret.
            record.msg = message
            record.args = ()
        return True


def default_log_file() -> Optional[Path]:
    """Where a long-running process should write its rotating log.

    Returns None when LOG_TO_FILE=0 — the container case, where stdout is the
    log and a file on an ephemeral disk is just wasted IO.

    LOG_DIR overrides the location. Point it outside any synced folder: a
    rotating log is rewritten constantly and OneDrive will upload every
    revision of all 50 files.
    """
    if not settings.log_to_file:
        return None
    base = Path(settings.log_dir) if settings.log_dir else PROJECT_ROOT / "logs"
    return base / "backend.log"


def _json_formatter() -> logging.Formatter:
    """Structured output for production — one JSON object per line, with the
    correlation ids as first-class fields so a log store can filter on them.

    Falls back to the text format if python-json-logger isn't installed rather
    than refusing to start: a logging dependency should never be the reason a
    service won't boot.
    """
    try:
        from pythonjsonlogger import jsonlogger
    except ImportError:
        logging.getLogger(__name__).warning(
            "LOG_JSON=1 but python-json-logger is not installed — using text format"
        )
        return logging.Formatter(_CONSOLE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    return jsonlogger.JsonFormatter(
        _JSON_FORMAT,
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    )
