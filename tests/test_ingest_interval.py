"""How often the scheduler sweeps Gmail: the lag between mail landing and the
desk seeing it. Was a hard-coded 5 minutes; now INGEST_INTERVAL_SECONDS, default
60, floored so a typo cannot hammer the Gmail quota."""

import asyncio
import dataclasses

import pytest

from backend.app import lifespan as lifespan_mod
from backend.core import config


@pytest.fixture
def fresh_settings():
    config.get_settings.cache_clear()
    yield config.get_settings
    config.get_settings.cache_clear()


@pytest.mark.parametrize("raw, expected", [
    (None, 60),
    ("", 60),
    ("120", 120),
    ("30", 30),
    ("5", config.MIN_INGEST_INTERVAL_SECONDS),
    ("0", config.MIN_INGEST_INTERVAL_SECONDS),
])
def test_interval_defaults_to_a_minute_and_is_floored(monkeypatch, fresh_settings, raw, expected):
    if raw is None:
        monkeypatch.delenv("INGEST_INTERVAL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("INGEST_INTERVAL_SECONDS", raw)
    assert fresh_settings().ingest_interval_seconds == expected


def test_a_non_number_is_a_config_error(monkeypatch, fresh_settings):
    monkeypatch.setenv("INGEST_INTERVAL_SECONDS", "1m")
    with pytest.raises(config.ConfigError):
        fresh_settings()


class _FakeScheduler:
    def __init__(self, **_kw):
        self.jobs: dict[str, dict] = {}

    def add_job(self, fn, trigger, **kw):
        self.jobs[kw["id"]] = {"trigger": trigger, **kw}

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


def test_the_scheduler_sweeps_gmail_at_the_configured_interval(monkeypatch):
    made: list[_FakeScheduler] = []

    def factory(**kw):
        s = _FakeScheduler(**kw)
        made.append(s)
        return s

    monkeypatch.setattr(lifespan_mod, "BackgroundScheduler", factory)
    monkeypatch.setattr(lifespan_mod, "check_connectivity", lambda: None)
    # The startup one-shots would reach Gmail and the LLM; not under test here.
    monkeypatch.setattr(lifespan_mod.threading, "Thread",
                        lambda **_kw: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(lifespan_mod, "settings",
                        dataclasses.replace(lifespan_mod.settings, run_scheduler=True,
                                            ingest_interval_seconds=60))

    async def run():
        async with lifespan_mod.lifespan(None):
            pass

    asyncio.run(run())
    (sched,) = made
    ingest = sched.jobs["email_ingest"]
    assert ingest["trigger"] == "interval" and ingest["seconds"] == 60
    # A sweep that overruns is followed by one run, not a burst of missed ones.
    assert ingest["coalesce"] is True and ingest["max_instances"] == 1
    # The scan is unchanged.
    assert sched.jobs["inbox_scan"]["minutes"] == 5
