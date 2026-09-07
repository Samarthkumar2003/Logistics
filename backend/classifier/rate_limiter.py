"""
rate_limiter.py
---------------
Client-side token-bucket pacing for LLM calls.

Why this exists
---------------
OpenAI meters tokens per minute per *organization*, and `classify_emails_batch`
runs five threads at once. At this project's measured request size — a
5,881-character system prompt plus up to `MAX_BODY_CHARS` of body, ~3,540 tokens
— five concurrent calls put ~17,700 tokens in flight the instant a batch starts.
On a Tier 1 key (30,000 TPM for gpt-4o) draining a backlog hits the ceiling in
seconds:

    Rate limit reached for gpt-4o ... on tokens per min (TPM):
    Limit 30000, Used 30000, Requested 3543

`classify_email` already retries a 429 with 5s/10s/20s backoff, but that is
reactive in the expensive way: the limit is found by exceeding it, all five
workers find it in the same moment, and each retry re-sends the entire prompt.
Pacing a request before it leaves costs a `sleep`; being rejected costs the
tokens plus the wait plus a re-send.

Scope — and what this deliberately does NOT do
----------------------------------------------
The bucket is per process. It is not a distributed limiter and does not pretend
to be: two processes sharing one API key each get a full allowance and together
exceed the org limit regardless of what this file does. That is a deployment
invariant rather than something a client-side limiter can enforce —
`RUN_SCHEDULER=1` on exactly one process, see
Documentation/06-deploying.md § Scaling past one replica.

Set `LLM_TPM=0` to disable pacing entirely (the offline test suite has no
provider to pace).
"""

from __future__ import annotations

import logging
import threading
import time

from backend.core.config import settings

logger = logging.getLogger(__name__)

# Characters per token. The standard rough figure for English prose, and it
# matched what this project actually gets billed: a 5,881-char system prompt
# plus ~8 kB of body estimated at ~3,470 tokens against an observed 3,543.
CHARS_PER_TOKEN = 4

# Per-call framing the API counts but the prompt strings do not contain — role
# keys, message separators, the chat template's own scaffolding.
FRAMING_TOKENS = 16

# Fraction of the advertised limit the bucket will actually spend. The estimate
# below is approximate, and the org limit is shared with anything else using the
# same key, so aiming at 100% guarantees periodic 429s — which is the thing this
# module exists to avoid.
SAFETY_FRACTION = 0.85


class TokenBucket:
    """A blocking token bucket, refilled continuously. Thread-safe.

    Capacity is one minute's allowance, matching the window the API meters over.
    A burst that would have fitted inside a real minute therefore passes with no
    artificial delay, and only genuinely sustained load gets paced.
    """

    def __init__(self, tokens_per_min: int, name: str = "llm") -> None:
        self._capacity = float(max(1, tokens_per_min))
        self._rate = self._capacity / 60.0  # tokens per second
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()
        self._name = name

    def acquire(self, tokens: int) -> float:
        """Block until `tokens` are available, spend them, return seconds waited.

        A request larger than the whole capacity is clamped rather than allowed
        to wait: the bucket can never hold more than `capacity`, so an unclamped
        oversized request would block on a condition that cannot ever occur.
        """
        need = min(float(max(0, tokens)), self._capacity)
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._updated) * self._rate,
                )
                self._updated = now
                if self._tokens >= need:
                    self._tokens -= need
                    if waited >= 1.0:
                        logger.info("%s throttle: waited %.1fs for ~%d tokens",
                                    self._name, waited, tokens)
                    return waited
                sleep_for = (need - self._tokens) / self._rate
            # Sleep OUTSIDE the lock. Holding it would serialise every worker on
            # the mutex instead of letting them queue on the refill, which turns
            # a five-wide pool into a one-wide one.
            time.sleep(sleep_for)
            waited += sleep_for


def estimate_tokens(system: str, user: str, max_tokens: int) -> int:
    """Rough upper bound on what one completion will bill.

    Counts `max_tokens` in full even though most replies are far shorter — a
    classification returns a label, ~10 tokens against a 60-token cap. The
    asymmetry is deliberate: an underestimate spends precisely the headroom this
    module exists to protect, while an overestimate only slows the drain.
    """
    prompt = (len(system) + len(user)) // CHARS_PER_TOKEN
    return prompt + max_tokens + FRAMING_TOKENS


_bucket: TokenBucket | None = None
_resolved = False
_resolve_lock = threading.Lock()


def get_llm_bucket() -> TokenBucket | None:
    """The process-wide bucket, or None when pacing is off (`LLM_TPM=0`).

    Built lazily and once. `_resolved` is separate from `_bucket` so that the
    disabled case is also cached — keyed on `_bucket is None` alone, every call
    would re-enter the lock and re-log.
    """
    global _bucket, _resolved
    if _resolved:
        return _bucket
    with _resolve_lock:
        if not _resolved:
            limit = settings.llm_tpm
            if limit > 0:
                budget = max(1, int(limit * SAFETY_FRACTION))
                _bucket = TokenBucket(budget, name=settings.llm_provider)
                logger.info(
                    "LLM pacing on: %d TPM limit, spending %d (%.0f%%). "
                    "Per process — one scheduler only.",
                    limit, budget, SAFETY_FRACTION * 100,
                )
            else:
                logger.info("LLM_TPM=0 — client-side pacing disabled")
            _resolved = True
    return _bucket
