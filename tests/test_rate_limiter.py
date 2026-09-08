"""
Client-side pacing for LLM calls.

`classify_emails_batch` runs five threads, each sending a 5,881-character system
prompt plus up to 8 kB of body — ~3,540 tokens, so ~17,700 in flight the instant a
batch starts, against a Tier 1 ceiling of 30,000 per minute. The retry path finds
that limit by exceeding it and pays three times over: the tokens that were
rejected, the backoff, and a full re-send of the prompt. Waiting first costs a
sleep.

Two things here are easy to get wrong and both are load-bearing:

  * a request larger than the whole bucket must be CLAMPED, not queued — the
    bucket cannot hold more than its capacity, so an unclamped oversized request
    waits on a condition that can never become true;
  * the wait must happen OUTSIDE the lock, or five workers serialise on the mutex
    and the pool is one-wide.

Time is faked throughout. A test that proves a 20-second wait by waiting 20
seconds is a test people stop running.
"""

from dataclasses import replace

import pytest

from backend.classifier import rate_limiter
from backend.classifier.rate_limiter import (
    CHARS_PER_TOKEN, FRAMING_TOKENS, SAFETY_FRACTION, TokenBucket, estimate_tokens,
)


class _Clock:
    """A monotonic clock that only advances when something sleeps."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(rate_limiter.time, "monotonic", c.monotonic)
    monkeypatch.setattr(rate_limiter.time, "sleep", c.sleep)
    return c


# ---------------------------------------------------------------------------
# The bucket
# ---------------------------------------------------------------------------

def test_a_burst_that_fits_inside_one_minute_is_not_delayed(clock):
    """Capacity is a full minute's allowance, matching the window the API meters
    over, so load that a real minute would have accepted passes untouched."""
    bucket = TokenBucket(3_000)

    assert bucket.acquire(1_500) == 0.0
    assert bucket.acquire(1_500) == 0.0
    assert clock.slept == []


def test_sustained_load_waits_for_the_refill(clock):
    """60 per minute is 1 per second, so the 10 tokens past capacity cost 10s."""
    bucket = TokenBucket(60)
    bucket.acquire(60)

    assert bucket.acquire(10) == pytest.approx(10.0)


def test_the_refill_is_continuous_not_per_window(clock):
    """Tokens accrue with elapsed time rather than resetting on a minute boundary,
    so a caller that waited out its debt gets served immediately."""
    bucket = TokenBucket(60)
    bucket.acquire(60)
    clock.sleep(30)  # 30 tokens back

    assert bucket.acquire(30) == 0.0


def test_the_bucket_never_accrues_past_capacity(clock):
    """An idle hour does not buy an hour's worth of burst — otherwise a quiet
    period would be followed by a spike far above the limit being protected."""
    bucket = TokenBucket(60)
    clock.sleep(3_600)

    assert bucket.acquire(60) == 0.0
    assert bucket.acquire(1) == pytest.approx(1.0), "capped at one minute's worth"


def test_a_request_bigger_than_the_bucket_is_clamped_not_deadlocked(clock):
    """The bucket can never hold more than capacity, so an oversized request must
    be clamped to it. Unclamped, this waits forever on a condition that cannot
    occur — and it is reachable: MAX_BODY_CHARS of body against a small LLM_TPM."""
    bucket = TokenBucket(100)

    assert bucket.acquire(10_000) == 0.0
    # It did spend the capacity: refilling a full bucket takes one minute, by
    # construction, whatever the rate.
    assert bucket.acquire(100) == pytest.approx(60.0)


def test_a_zero_or_negative_request_costs_nothing(clock):
    bucket = TokenBucket(60)

    assert bucket.acquire(0) == 0.0
    assert bucket.acquire(-5) == 0.0
    assert bucket.acquire(60) == 0.0, "nothing was spent"


def test_a_zero_capacity_bucket_still_functions(clock):
    """`max(1, ...)` in the constructor. A bucket built with 0 would divide by zero
    computing its sleep — LLM_TPM=0 is meant to disable pacing (get_llm_bucket
    returns None), never to build a bucket that cannot serve anyone."""
    bucket = TokenBucket(0)

    assert bucket.acquire(1) == 0.0


# ---------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------

def test_the_estimate_counts_prompt_plus_reply_cap_plus_framing():
    system, user = "s" * 400, "u" * 400

    assert estimate_tokens(system, user, 60) == 800 // CHARS_PER_TOKEN + 60 + FRAMING_TOKENS


def test_the_estimate_never_undercounts_the_reply():
    """`max_tokens` is counted in full even though a classification returns ~10
    tokens against a 60-token cap. The asymmetry is deliberate: an underestimate
    spends exactly the headroom this module exists to protect, while an
    overestimate only slows the drain."""
    assert estimate_tokens("", "", 60) - estimate_tokens("", "", 0) == 60


def test_the_estimate_is_within_a_few_percent_of_a_real_bill():
    """Measured: a 5,881-char system prompt plus ~8 kB of body billed 3,543
    tokens. The 4-chars-per-token figure has to stay honest for the pacing to
    mean anything, so this pins it against the observation."""
    estimate = estimate_tokens("x" * 5_881, "y" * 8_000, 60)

    assert 3_400 <= estimate <= 3_700


# ---------------------------------------------------------------------------
# Resolving the process-wide bucket
# ---------------------------------------------------------------------------

@pytest.fixture
def unresolved(monkeypatch):
    """Undo the lazy one-time resolution so each test resolves it again."""
    monkeypatch.setattr(rate_limiter, "_bucket", None)
    monkeypatch.setattr(rate_limiter, "_resolved", False)


def _with_tpm(monkeypatch, tpm: int) -> None:
    monkeypatch.setattr(rate_limiter, "settings",
                        replace(rate_limiter.settings, llm_tpm=tpm))


def test_llm_tpm_zero_disables_pacing_entirely(unresolved, monkeypatch):
    _with_tpm(monkeypatch, 0)

    assert rate_limiter.get_llm_bucket() is None


def test_the_disabled_case_is_cached_too(unresolved, monkeypatch):
    """`_resolved` is a separate flag from `_bucket` on purpose: keyed on
    `_bucket is None` alone, every call with pacing off would re-enter the lock
    and re-log — once per classified email."""
    _with_tpm(monkeypatch, 0)
    rate_limiter.get_llm_bucket()

    assert rate_limiter._resolved is True


def test_pacing_spends_a_safety_fraction_of_the_advertised_limit(unresolved, monkeypatch):
    """The estimate is approximate and the org limit is shared with anything else
    on the key, so aiming at 100% guarantees the 429s this module exists to
    avoid."""
    _with_tpm(monkeypatch, 30_000)
    bucket = rate_limiter.get_llm_bucket()

    assert bucket is not None
    assert bucket._capacity == pytest.approx(30_000 * SAFETY_FRACTION)


def test_the_bucket_is_built_once_and_shared(unresolved, monkeypatch):
    """One process, one allowance. A per-call bucket would give every worker a
    full minute's budget, which is no limit at all."""
    _with_tpm(monkeypatch, 30_000)

    assert rate_limiter.get_llm_bucket() is rate_limiter.get_llm_bucket()


def test_a_tiny_limit_still_yields_a_usable_bucket(unresolved, monkeypatch):
    """`LLM_TPM=1` × 0.85 truncates to 0, which would be a bucket that can never
    serve a request. Floored at 1 instead."""
    _with_tpm(monkeypatch, 1)
    bucket = rate_limiter.get_llm_bucket()

    assert bucket is not None and bucket._capacity >= 1.0


# ---------------------------------------------------------------------------
# Wiring: the classifier actually asks the bucket
#
# A pacer nothing calls is worse than no pacer — it reads as solved. These pin
# the call site, not the arithmetic.
# ---------------------------------------------------------------------------

class _SpyBucket:
    def __init__(self):
        self.charges: list[int] = []

    def acquire(self, tokens: int) -> float:
        self.charges.append(tokens)
        return 0.0


class _FakeProvider:
    """A provider that answers, and optionally 429s the first N attempts."""

    name = "fake"

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls: list[tuple[str, str, int]] = []

    def complete(self, system, user, temperature=0.0, max_tokens=0):
        self.calls.append((system, user, max_tokens))
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("Error code: 429 - rate_limit_exceeded")
        return '{"label": "general", "confidence": 0.7}'


# An email that matches no rule: external sender, no job reference, no rate-card
# subject. The only way into the LLM path, which is the path being paced.
_FALLTHROUGH = ("Container availability next week",
                "Please advise on space for next Tuesday.",
                "agent@example.com")


@pytest.fixture
def paced(monkeypatch):
    """Wire a spy bucket and a stub provider into the classifier."""
    from backend.classifier import email_classifier

    bucket, provider = _SpyBucket(), _FakeProvider()
    monkeypatch.setattr(email_classifier, "get_llm_bucket", lambda: bucket)
    monkeypatch.setattr(email_classifier, "get_provider", lambda: provider)
    monkeypatch.setattr(email_classifier.time, "sleep", lambda _s: None)
    return email_classifier, bucket, provider


def test_the_classifier_pays_the_bucket_before_calling_the_provider(paced):
    email_classifier, bucket, provider = paced

    result = email_classifier.classify_email(*_FALLTHROUGH)

    assert result.method == "llm:fake", "it did take the LLM path"
    assert len(bucket.charges) == 1
    assert len(provider.calls) == 1


def test_the_charge_matches_the_request_that_was_actually_sent(paced):
    """The estimate has to be built from the same prompt and the same
    `max_tokens` the call uses, or the pacing is spending against a fiction."""
    email_classifier, bucket, provider = paced

    email_classifier.classify_email(*_FALLTHROUGH)

    system, user, max_tokens = provider.calls[0]
    assert bucket.charges[0] == estimate_tokens(system, user, max_tokens)


def test_every_retry_is_charged_again(paced, monkeypatch):
    """A 429 retry re-sends the entire prompt, so attempt two costs the provider
    exactly what attempt one did. Charging per email instead of per attempt would
    under-count precisely when the limit is already tight."""
    email_classifier, bucket, provider = paced
    provider.fail_times = 2

    result = email_classifier.classify_email(*_FALLTHROUGH)

    assert result.method == "llm:fake", "third attempt succeeded"
    assert len(provider.calls) == 3
    assert bucket.charges == [bucket.charges[0]] * 3


def test_pacing_off_still_classifies(monkeypatch):
    """`LLM_TPM=0` returns None, and the call site must treat that as a no-op
    rather than an error — it is what the whole offline suite runs with."""
    from backend.classifier import email_classifier

    provider = _FakeProvider()
    monkeypatch.setattr(email_classifier, "get_llm_bucket", lambda: None)
    monkeypatch.setattr(email_classifier, "get_provider", lambda: provider)

    assert email_classifier.classify_email(*_FALLTHROUGH).method == "llm:fake"


def test_a_rule_hit_never_touches_the_bucket(paced):
    """Rules short-circuit before the provider, and ~80% of the inbox is decided
    by one. Charging for them would throttle calls that never happen."""
    email_classifier, bucket, provider = paced

    email_classifier.classify_email("anything", "body", "ops@bhatiashipping.com")

    assert bucket.charges == [] and provider.calls == []
