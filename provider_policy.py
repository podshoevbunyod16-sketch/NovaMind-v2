"""provider_policy.py — model capabilities, accounting and circuit breaker.

The existing provider manager owns key rotation and retries.  This module adds
the missing policy layer without changing ``chat_completion``'s signature:

* a declarative capability/context/cost registry for models;
* conservative token and cost estimation;
* per-owner budgets with a thread-safe in-memory ledger (Redis-ready interface);
* a small circuit breaker for unstable providers.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from config import PROVIDERS


@dataclass(frozen=True)
class ModelCapabilities:
    provider: str
    model_id: str
    context_window: int = 8192
    max_output_tokens: int = 4096
    supports_streaming: bool = True
    supports_tools: bool = False
    supports_vision: bool = False
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0
    free: bool = False


def build_registry() -> dict[tuple[str, str], ModelCapabilities]:
    registry: dict[tuple[str, str], ModelCapabilities] = {}
    for provider, cfg in PROVIDERS.items():
        for model in cfg.get("models", []):
            model_id = str(model.get("id") or "")
            if not model_id:
                continue
            registry[(provider, model_id)] = ModelCapabilities(
                provider=provider,
                model_id=model_id,
                context_window=int(model.get("context_length") or cfg.get("max_tokens") or 8192),
                max_output_tokens=int(cfg.get("max_tokens") or 4096),
                supports_tools=provider not in {"local_demo"},
                supports_vision=("vision" in model_id.lower() or "gemini" in model_id.lower()),
                free=bool(model.get("free", False)),
            )
    return registry


CAPABILITIES = build_registry()


def capabilities(provider: str, model: str) -> ModelCapabilities:
    """Return known capabilities or conservative defaults for unknown models."""
    found = CAPABILITIES.get((provider, model)) or CAPABILITIES.get((provider, ""))
    if found:
        return found
    return ModelCapabilities(provider=provider, model_id=model, context_window=4096,
                             max_output_tokens=2048, free=False)


def estimate_tokens(text: str) -> int:
    """Rough but deterministic token estimate (4 characters per token)."""
    return max(1, (len(str(text or "")) + 3) // 4)


def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    caps = capabilities(provider, model)
    return round((input_tokens * caps.input_cost_per_1k
                  + output_tokens * caps.output_cost_per_1k) / 1000.0, 6)


class TokenLedger:
    """Per-owner rolling usage. Swap this class for a Redis implementation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usage: dict[str, dict[str, float]] = {}

    def record(self, owner: str, provider: str, model: str,
               input_tokens: int, output_tokens: int) -> dict[str, float]:
        cost = estimate_cost(provider, model, input_tokens, output_tokens)
        key = str(owner or "anonymous")[:200]
        with self._lock:
            entry = self._usage.setdefault(key, {"tokens": 0.0, "cost": 0.0, "calls": 0})
            entry["tokens"] += input_tokens + output_tokens
            entry["cost"] += cost
            entry["calls"] += 1
            return dict(entry)

    def allowed(self, owner: str, input_tokens: int) -> tuple[bool, str]:
        budget = int(os.getenv("MODEL_TOKEN_BUDGET", "0"))
        if budget <= 0:
            return True, ""
        entry = self._usage.get(str(owner or "anonymous")[:200], {})
        if entry.get("tokens", 0) + input_tokens > budget:
            return False, "Превышен лимит токенов"
        return True, ""

    def snapshot(self, owner: str) -> dict[str, float]:
        with self._lock:
            return dict(self._usage.get(str(owner or "anonymous")[:200],
                                        {"tokens": 0.0, "cost": 0.0, "calls": 0}))


ledger = TokenLedger()


class CircuitBreaker:
    """Per-provider breaker: closed → open after N failures → half-open probe.

    No background threads: probes happen on the first call after cooldown.
    """

    def __init__(self, threshold: int = 5, cooldown: float = 60.0,
                 clock: Any = None) -> None:
        self.threshold = max(1, threshold)
        self.cooldown = max(0.0, cooldown)
        self._clock = clock or time.monotonic
        self._state = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if self._state == "open" and self._clock() - self._opened_at >= self.cooldown:
            self._state = "half-open"

    def allow(self) -> bool:
        with self._lock:
            self._maybe_half_open()
            return self._state != "open"

    def record_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == "half-open" or self._failures >= self.threshold:
                self._state = "open"
                self._opened_at = self._clock()


_BREAKERS: dict[str, CircuitBreaker] = {}
_BREAKER_LOCK = threading.Lock()


def breaker(provider: str) -> CircuitBreaker:
    with _BREAKER_LOCK:
        if provider not in _BREAKERS:
            _BREAKERS[provider] = CircuitBreaker(
                threshold=int(os.getenv("PROVIDER_BREAKER_THRESHOLD", "5")),
                cooldown=float(os.getenv("PROVIDER_BREAKER_COOLDOWN", "60")))
        return _BREAKERS[provider]


def public_catalog() -> list[dict[str, Any]]:
    """Safe catalogue for ``/api/ai/capabilities`` (no credentials)."""
    return [
        {
            "provider": caps.provider,
            "model": caps.model_id,
            "context_window": caps.context_window,
            "max_output_tokens": caps.max_output_tokens,
            "streaming": caps.supports_streaming,
            "tools": caps.supports_tools,
            "vision": caps.supports_vision,
            "free": caps.free,
            "configured": __import__("config").has_credentials(caps.provider),
        }
        for caps in CAPABILITIES.values()
    ]
