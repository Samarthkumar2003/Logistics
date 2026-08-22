"""
config.py
---------
Every environment variable this backend reads, in one place.

Before this existed, `os.environ.get(...)` was scattered across ten modules —
several of them declaring different defaults for the same key — and
`load_dotenv()` was called in eight, some with an explicit path and some
without, so behaviour depended on your working directory.

Now: `.env` is loaded once, here, from the repo root. Settings are read once and
frozen. A missing required value fails immediately with a message naming the
key, rather than surfacing at 3am inside a scheduler thread.

    from backend.core.config import settings
    if settings.email_redirect:
        ...
"""

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

from backend.core.paths import PROJECT_ROOT

# The single load. Importing this module is what makes .env available; nothing
# else should call load_dotenv().
load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """A required setting is missing or unusable."""


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _flag(key: str, default: bool) -> bool:
    raw = _env(key)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    raw = _env(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    # --- Datastore -----------------------------------------------------------
    supabase_url: str
    supabase_key: str
    attachment_bucket: str

    # --- LLM -----------------------------------------------------------------
    openai_api_key: str
    llm_provider: str
    openai_model: str

    # --- Mail ----------------------------------------------------------------
    email_provider: str
    email_account: str
    email_password: str
    smtp_server: str
    smtp_port: int
    imap_server: str
    # When set, ALL outgoing mail goes here instead of the real vendor. The one
    # safety valve that makes this codebase testable against a live inbox.
    email_redirect: str
    gmail_mailbox: str
    report_recipient: str

    # --- Runtime -------------------------------------------------------------
    run_scheduler: bool
    scan_batch: int
    # A job sits at `sending` only between reserving its row and recording the
    # send outcome — seconds, normally. Past this many minutes it is treated as
    # abandoned by a crashed process and swept: reconciled against the Sent
    # folder, then advanced or (provably-unsent) retried once. Read by both the
    # metrics gauge and the retry sweep, so they never disagree on "stuck".
    stale_sending_minutes: int
    # When true, the sweep may re-send an RFQ it has PROVEN never left (absent
    # from the Sent folder). Set false to make the sweep reconcile-only —
    # advancing delivered rows and flagging the rest send_failed for a human,
    # never sending anything unattended.
    auto_retry_stuck_sends: bool
    log_level: str
    log_json: bool
    # Where the rotating log file goes. Empty = PROJECT_ROOT/logs. Set it to a
    # path OUTSIDE any synced folder (OneDrive, Dropbox) — a rotating log is a
    # file rewritten constantly, and a sync client will upload every version of
    # it. In a container, leave this empty and set LOG_TO_FILE=0 instead: the
    # platform collects stdout.
    log_dir: str
    log_to_file: bool
    cors_origins: tuple[str, ...]

    @property
    def safe_mode(self) -> bool:
        """True when outgoing mail is being redirected away from real vendors."""
        return bool(self.email_redirect)

    def require_supabase(self) -> None:
        if not self.supabase_url or not self.supabase_key:
            raise ConfigError(
                "SUPABASE_URL and SUPABASE_KEY must be set — see .env.example"
            )

    def require_openai(self) -> None:
        if not self.openai_api_key:
            raise ConfigError("OPENAI_API_KEY must be set — see .env.example")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read the environment once. Cached, so later calls are free."""
    origins = _env("CORS_ORIGINS", "http://localhost:3000,http://localhost:3001")
    return Settings(
        supabase_url=_env("SUPABASE_URL"),
        supabase_key=_env("SUPABASE_KEY"),
        attachment_bucket=_env("ATTACHMENT_BUCKET", "rate-card-attachments"),
        openai_api_key=_env("OPENAI_API_KEY"),
        llm_provider=_env("LLM_PROVIDER", "openai").lower(),
        openai_model=_env("OPENAI_MODEL", "gpt-4o-mini"),
        email_provider=_env("EMAIL_PROVIDER", "gmail").lower(),
        email_account=_env("EMAIL_ACCOUNT"),
        email_password=_env("EMAIL_PASSWORD"),
        smtp_server=_env("SMTP_SERVER", "smtp.gmail.com"),
        smtp_port=_int("SMTP_PORT", 587),
        imap_server=_env("IMAP_SERVER", "imap.gmail.com"),
        email_redirect=_env("EMAIL_REDIRECT"),
        gmail_mailbox=_env("GMAIL_MAILBOX"),
        report_recipient=_env("REPORT_RECIPIENT"),
        run_scheduler=_flag("RUN_SCHEDULER", True),
        scan_batch=_int("SCAN_BATCH", 50),
        stale_sending_minutes=_int("STALE_SENDING_MINUTES", 15),
        auto_retry_stuck_sends=_flag("AUTO_RETRY_STUCK_SENDS", True),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        log_json=_flag("LOG_JSON", False),
        log_dir=_env("LOG_DIR"),
        log_to_file=_flag("LOG_TO_FILE", True),
        cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
    )


# Module-level convenience. Reading an attribute never touches the environment
# again, so this is safe to import anywhere.
settings = get_settings()
