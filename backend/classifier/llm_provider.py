"""
llm_provider.py
---------------
Swappable LLM backend (Open/Closed Principle).

A new model is added by writing a provider class and registering it — no
existing code changes. The active provider is chosen at runtime by the
``LLM_PROVIDER`` env var; callers depend only on the ``LLMProvider`` interface,
never on a concrete vendor SDK.

    LLM_PROVIDER=openai   (default)  -> OpenAIProvider   (gpt-4o-mini)
    LLM_PROVIDER=gemini              -> GeminiProvider   (gemini-2.5-flash-lite)

Add a provider:
    @register_provider("anthropic")
    class AnthropicProvider(LLMProvider):
        def complete(self, system, user, *, temperature=0.0, max_tokens=512): ...
"""

import os
import logging
import threading
from abc import ABC, abstractmethod
from typing import Callable, Optional

from backend.core.config import settings
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy interface
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """One text-in / text-out chat completion. Vendor-agnostic by design —
    providers know nothing about classification, so they stay reusable."""

    name: str = "base"

    @abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> str:
        """Return the model's reply text for a system + user prompt pair."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Registry + factory
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[LLMProvider]] = {}
_INSTANCE: Optional[LLMProvider] = None
_LOCK = threading.Lock()


def register_provider(key: str) -> Callable[[type[LLMProvider]], type[LLMProvider]]:
    """Class decorator — register a provider under an ``LLM_PROVIDER`` key."""
    def _wrap(cls: type[LLMProvider]) -> type[LLMProvider]:
        cls.name = key
        _REGISTRY[key.lower()] = cls
        return cls
    return _wrap


def available_providers() -> list[str]:
    """Keys that can be selected via ``LLM_PROVIDER``."""
    return sorted(_REGISTRY.keys())


def get_provider() -> LLMProvider:
    """Return the active provider (cached). Selected by ``LLM_PROVIDER`` env,
    default ``openai``. Swapping models is an env change, not a code change."""
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _LOCK:
        if _INSTANCE is not None:
            return _INSTANCE
        key = settings.llm_provider
        cls = _REGISTRY.get(key)
        if cls is None:
            raise ValueError(
                f"Unknown LLM_PROVIDER '{key}'. Available: {available_providers()}"
            )
        _INSTANCE = cls()
        logger.info("LLM provider active: %s", key)
        return _INSTANCE


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

@register_provider("openai")
class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions. Model from ``OPENAI_MODEL`` (default gpt-4o-mini)."""

    def __init__(self) -> None:
        from openai import OpenAI
        self._client = OpenAI()
        self._model = settings.openai_model

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------

@register_provider("gemini")
class GeminiProvider(LLMProvider):
    """Google Gemini via google-genai. Model from ``GEMINI_MODEL``
    (default gemini-2.5-flash-lite). Needs ``GEMINI_API_KEY`` and
    ``pip install google-genai``."""

    def __init__(self) -> None:
        from google import genai  # lazy: only required when this provider is selected
        self._genai = genai
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")
        self._client = genai.Client(api_key=api_key)
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> str:
        from google.genai import types
        resp = self._client.models.generate_content(
            model=self._model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        return (resp.text or "").strip()
