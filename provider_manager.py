"""
provider_manager.py — единый Provider Manager NovaMind.

Объединяет все AI-провайдеры и их API-ключи в одном месте:

* классификация ошибок (401/403/404/429/5xx/timeout/quota/billing) —
  к каждой категории своя стратегия восстановления;
* ротация нескольких ключей с индивидуальным cooldown на ключ и модель;
* HTTP 429 с учётом Retry-After и rate-limit заголовков, без них —
  exponential backoff с jitter;
* конечное число попыток: без бесконечных повторов и циклических
  переключений, временно заблокированные ключи не используются повторно;
* ограниченная ротация ключей внутри выбранного провайдера;
* отдельный execute_with_failover helper существует для явных сценариев,
  но обычный чат не переключает выбранные провайдер или модель;
* структурированные логи (провайдер, модель, номер ключа без самого
  ключа, причина ошибки, время cooldown, результат переключения);
* ключи никогда не попадают в логи и ответы — только номер ключа.
"""
from __future__ import annotations

import enum
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger("novamind.providers")

MAX_COOLDOWN_SECONDS = 25 * 3600  # защитный верхний предел cooldown
DEFAULT_COOLDOWN_SECONDS = 60
AUTH_FAILURE_THRESHOLD = 3        # после 3 подряд 401 ключ отключается


class ErrorKind(str, enum.Enum):
    AUTH = "auth"                # 401 — ключ отклонён
    FORBIDDEN = "forbidden"      # 403 — доступ запрещён
    NOT_FOUND = "not_found"      # 404 — ресурс/модель не найдены
    RATE_LIMIT = "rate_limit"    # 429 — частотный лимит
    SERVER = "server"            # 5xx — ошибка сервера провайдера
    TIMEOUT = "timeout"
    QUOTA = "quota"              # исчерпана квота (например, дневная)
    BILLING = "billing"          # не хватает средств / платный тариф
    NETWORK = "network"          # соединение не установлено
    BAD_RESPONSE = "bad_response"
    UNKNOWN = "unknown"


# kind → (повторять ли на другом ключе, fatal для ключа)
_KIND_POLICY = {
    ErrorKind.AUTH: (True, True),         # пробуем следующий ключ; отключаем после порога 401
    ErrorKind.FORBIDDEN: (False, False),  # запрет не лечится сменой ключа
    ErrorKind.NOT_FOUND: (False, False),  # модель не та — ключ ни при чём
    ErrorKind.RATE_LIMIT: (True, False),  # пробуем другой ключ
    ErrorKind.QUOTA: (True, False),       # дневная квота — другой ключ поможет
    ErrorKind.SERVER: (True, False),      # 5xx — можно повторить
    ErrorKind.TIMEOUT: (True, False),
    ErrorKind.NETWORK: (True, False),
    ErrorKind.BILLING: (False, True),     # биллинг не лечится ротацией
    ErrorKind.BAD_RESPONSE: (False, False),
    ErrorKind.UNKNOWN: (False, False),
}


class ProviderError(Exception):
    """Классифицированная ошибка провайдера."""

    def __init__(self, kind: ErrorKind, message: str, *, status_code=None,
                 retry_after=None, provider=None, model=None, key_index=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retry_after = retry_after
        self.provider = provider
        self.model = model
        self.key_index = key_index

    @property
    def retry_on_other_key(self) -> bool:
        return _KIND_POLICY.get(self.kind, (False, False))[0]

    @property
    def fatal_for_key(self) -> bool:
        return _KIND_POLICY.get(self.kind, (False, False))[1]


def parse_retry_after(headers, default=None):
    """Retry-After (секунды или HTTP-date) и rate-limit reset заголовки.

    Возвращает секунды с безопасным верхним пределом либо ``default``.
    """
    headers = headers or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value:
        try:
            return max(1, min(int(float(value)), MAX_COOLDOWN_SECONDS))
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(str(value))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(1, min(int((when - datetime.now(timezone.utc)).total_seconds()),
                                  MAX_COOLDOWN_SECONDS))
            except (TypeError, ValueError, OverflowError):
                pass
    for name in ("X-RateLimit-Reset", "x-ratelimit-reset-requests",
                 "x-ratelimit-reset-tokens", "x-ratelimit-reset"):
        value = headers.get(name)
        if not value:
            continue
        try:
            raw = str(value).strip().lower()
            seconds = float(raw[:-1]) if raw.endswith("s") else float(raw)
            if not raw.endswith("s") and seconds > 1_000_000_000:
                seconds -= time.time()
            if seconds > 0:
                return max(1, min(int(seconds), MAX_COOLDOWN_SECONDS))
        except (TypeError, ValueError, OverflowError):
            continue
    return default


_DAILY_HINTS = ("per day", "per-day", "daily limit", "daily quota", "tokens per day",
                "requests per day", "day limit", "depleted")
_BILLING_HINTS = ("billing", "insufficient funds", "credit balance", "payment required",
                  "quota exceeded for paid tier")


def classify_response(status_code, headers=None, body_text=""):
    """HTTP-ответ → (ErrorKind, retry_after_seconds|None)."""
    headers = headers or {}
    text = (body_text or "").lower()
    retry_after = parse_retry_after(headers)
    status = int(status_code or 0)
    if status == 401:
        return ErrorKind.AUTH, retry_after
    if status == 403:
        return ErrorKind.FORBIDDEN, retry_after
    if status == 404:
        return ErrorKind.NOT_FOUND, None
    if status == 429:
        if any(h in text for h in _DAILY_HINTS) or "daily" in " ".join(
                str(v).lower() for k, v in headers.items() if "reset" in k.lower()):
            return ErrorKind.QUOTA, retry_after or MAX_COOLDOWN_SECONDS
        return ErrorKind.RATE_LIMIT, retry_after
    if status == 402 or any(h in text for h in _BILLING_HINTS):
        return ErrorKind.BILLING, None
    if any(h in text for h in _DAILY_HINTS):
        return ErrorKind.QUOTA, retry_after or MAX_COOLDOWN_SECONDS
    if 500 <= status < 600:
        return ErrorKind.SERVER, retry_after
    return ErrorKind.UNKNOWN, retry_after


def classify_exception(exc):
    """Исключение requests → ErrorKind."""
    import requests
    if isinstance(exc, requests.exceptions.Timeout):
        return ErrorKind.TIMEOUT
    if isinstance(exc, requests.exceptions.ConnectionError):
        return ErrorKind.NETWORK
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        if response is not None:
            kind, _ = classify_response(response.status_code,
                                        getattr(response, "headers", {}),
                                        getattr(response, "text", ""))
            return kind
        return ErrorKind.NETWORK
    if isinstance(exc, requests.exceptions.RequestException):
        return ErrorKind.NETWORK
    if isinstance(exc, (ValueError, TypeError)):
        return ErrorKind.BAD_RESPONSE
    return ErrorKind.UNKNOWN


def backoff_delay(attempt, retry_after=None, base=1.0, cap=30.0, jitter=0.5):
    """Exponential backoff с jitter. Retry-After имеет приоритет."""
    if retry_after is not None:
        return min(float(retry_after), MAX_COOLDOWN_SECONDS)
    delay = min(cap, base * (2 ** max(0, int(attempt))))
    spread = delay * jitter
    return max(0.0, delay - spread + random.uniform(0.0, spread))


def redact(text, secrets=()):
    """Убирает из текста значения секретов — ключи не должны попадать в логи."""
    text = str(text or "")
    for secret in secrets:
        if secret and len(str(secret)) >= 4:
            text = text.replace(str(secret), "[скрыто]")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[скрыто]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password|authorization)(\s*[:=]\s*)[^\s,;\"]+",
                  r"\1\2[скрыто]", text)
    return text[:900]


def log_event(level, event, **fields):
    """Структурированный лог: провайдер/модель/номер ключа/причина/cooldown/переключение.

    Значения ключей не передаются — только ``key_index``.
    """
    parts = [f"event={event}"]
    for name in ("provider", "model", "key_index", "kind", "cooldown_sec",
                 "retry_after_sec", "attempt", "switched_to", "result"):
        if name in fields and fields[name] is not None:
            parts.append(f"{name}={fields[name]}")
    if fields.get("reason") is not None:
        parts.append("reason=" + redact(fields["reason"]))
    extra = {k: v for k, v in fields.items()
             if k not in ("provider", "model", "key_index", "kind", "reason",
                          "cooldown_sec", "retry_after_sec", "attempt",
                          "switched_to", "result")}
    if extra:
        parts.append("extra=" + redact(str(extra)))
    getattr(logger, level, logger.info)(" ".join(parts))


@dataclass
class KeyState:
    """Состояние одного API-ключа (сам ключ хранится отдельно и не логируется)."""
    index: int
    key: str
    cooldown_until: float = 0.0
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    invalid: bool = False
    auth_failures: int = 0
    last_error: str = ""

    def available(self, now=None):
        now = time.time() if now is None else now
        return not self.invalid and now >= self.cooldown_until

    def cooldown_remaining(self, now=None):
        now = time.time() if now is None else now
        return max(0, int(self.cooldown_until - now))


@dataclass
class ProviderEntry:
    name: str
    keys: list = field(default_factory=list)     # list[KeyState]
    models: list = field(default_factory=list)
    max_attempts: int = 4


class ProviderManager:
    """Единый реестр провайдеров с ограниченной ротацией ключей."""

    def __init__(self, sleep=time.sleep, now=time.time):
        self._providers: dict[str, ProviderEntry] = {}
        self._lock = threading.RLock()
        self._sleep = sleep
        self._now = now

    # ---------- регистрация ----------
    def register_provider(self, name, keys=None, models=None, max_attempts=4):
        keys = [str(k).strip() for k in (keys or []) if str(k).strip()]
        with self._lock:
            entry = ProviderEntry(
                name=name,
                keys=[KeyState(index=i, key=k) for i, k in enumerate(keys)],
                models=list(models or []),
                max_attempts=max(1, int(max_attempts)),
            )
            self._providers[name] = entry
            log_event("info", "provider_registered", provider=name,
                      keys=len(entry.keys), models=len(entry.models))
            return entry

    def provider_names(self):
        with self._lock:
            return list(self._providers)

    # ---------- ключи ----------
    def available_key(self, provider):
        """Свободный ключ провайдера или None. Ключи в cooldown не возвращаются."""
        with self._lock:
            entry = self._providers.get(provider)
            if not entry:
                return None
            now = self._now()
            for key in entry.keys:
                if key.available(now):
                    return key
            return None

    def mark_key_result(self, provider, key_index, error: ProviderError | None,
                        cooldown_seconds=None):
        """Отмечает результат запроса: cooldown / permanent disable / сброс."""
        with self._lock:
            entry = self._providers.get(provider)
            if not entry:
                return
            key = next((k for k in entry.keys if k.index == key_index), None)
            if key is None:
                return
            now = self._now()
            if error is None:
                key.auth_failures = 0
                key.last_error = ""
                log_event("info", "key_success", provider=provider, key_index=key_index,
                          result="ok")
                return
            key.last_error = error.kind.value
            if error.kind == ErrorKind.AUTH:
                # 401 — ключ временно в cooldown; навсегда отключается только
                # после AUTH_FAILURE_THRESHOLD последовательных отказов.
                key.auth_failures += 1
                if key.auth_failures >= AUTH_FAILURE_THRESHOLD:
                    key.invalid = True
                    log_event("error", "key_disabled", provider=provider, key_index=key_index,
                              kind=error.kind.value, reason=redact(str(error), [key.key]),
                              result="disabled_permanently")
                    return
            elif error.kind == ErrorKind.BILLING:
                key.invalid = True
                log_event("error", "key_disabled", provider=provider, key_index=key_index,
                          kind=error.kind.value, reason=redact(str(error), [key.key]),
                          result="disabled_billing")
                return
            seconds = int(cooldown_seconds if cooldown_seconds is not None
                          else (error.retry_after or DEFAULT_COOLDOWN_SECONDS))
            seconds = max(1, min(seconds, MAX_COOLDOWN_SECONDS))
            key.cooldown_until = now + seconds
            key.cooldown_seconds = seconds
            log_event("warning", "key_cooldown", provider=provider, key_index=key_index,
                      kind=error.kind.value, reason=redact(str(error), [key.key]),
                      cooldown_sec=seconds, retry_after_sec=error.retry_after)

    def all_keys_unavailable(self, provider):
        with self._lock:
            entry = self._providers.get(provider)
            if not entry or not entry.keys:
                return True
            now = self._now()
            return not any(k.available(now) for k in entry.keys)

    # ---------- выполнение ----------
    def execute(self, provider, model, request_fn, max_attempts=None):
        """Выполняет запрос с ротацией ключей провайдера.

        ``request_fn(key_state)`` должен вернуть результат или бросить
        ProviderError / requests-исключение. Число попыток конечно; ключи в
        cooldown повторно не используются. Возвращает (result, None) либо
        (None, ProviderError).
        """
        with self._lock:
            entry = self._providers.get(provider)
            if not entry:
                return None, ProviderError(ErrorKind.UNKNOWN, f"Провайдер {provider} не зарегистрирован",
                                           provider=provider, model=model)
            attempts = max(1, min(int(max_attempts or entry.max_attempts), len(entry.keys) or 1))
        last_error = None
        tried_indexes = set()
        for attempt in range(attempts):
            key = self.available_key(provider)
            if key is None or key.index in tried_indexes:
                break  # все ключи в cooldown — не крутимся бесконечно
            tried_indexes.add(key.index)
            try:
                result = request_fn(key)
            except ProviderError as exc:
                exc.provider = exc.provider or provider
                exc.model = exc.model or model
                exc.key_index = key.index
                last_error = exc
                self.mark_key_result(provider, key.index, exc)
                log_event("warning", "request_failed", provider=provider, model=model,
                          key_index=key.index, kind=exc.kind.value,
                          reason=redact(str(exc), [key.key]), attempt=attempt + 1)
                if not exc.retry_on_other_key:
                    break
                self._sleep(backoff_delay(attempt, exc.retry_after))
                continue
            except Exception as exc:  # requests и прочие транспортные ошибки
                kind = classify_exception(exc)
                perr = ProviderError(kind, str(exc), provider=provider, model=model,
                                     key_index=key.index)
                last_error = perr
                self.mark_key_result(provider, key.index, perr)
                log_event("warning", "request_failed", provider=provider, model=model,
                          key_index=key.index, kind=kind.value,
                          reason=redact(str(exc), [key.key]), attempt=attempt + 1)
                if not perr.retry_on_other_key:
                    break
                self._sleep(backoff_delay(attempt))
                continue
            self.mark_key_result(provider, key.index, None)
            log_event("info", "request_ok", provider=provider, model=model,
                      key_index=key.index, attempt=attempt + 1, result="ok")
            return result, None
        if last_error is None:
            last_error = ProviderError(
                ErrorKind.UNKNOWN,
                "Все ключи провайдера временно недоступны" if self.all_keys_unavailable(provider)
                else "Запрос не выполнен",
                provider=provider, model=model)
        return None, last_error

    def execute_with_failover(self, order, model, request_fn, max_attempts=None):
        """Пробует провайдеров по порядку, пока все ключи одного недоступны.

        Переход к следующему провайдеру выполняется только когда текущий
        исчерпан (все ключи в cooldown/отключены) либо ошибка не лечится
        сменой ключа. Общее число попыток конечно.
        """
        failures = []
        for provider in order:
            if provider not in self._providers:
                continue
            result, error = self.execute(provider, model, request_fn,
                                         max_attempts=max_attempts)
            if result is not None or error is None:
                log_event("info", "failover_succeeded", provider=provider, model=model,
                          switched_to=provider, result="ok",
                          reason=f"after {len(failures)} failure(s)" if failures else None)
                return result, provider, failures
            failures.append({"provider": provider, "error": redact(str(error))})
            log_event("warning", "provider_exhausted", provider=provider, model=model,
                      kind=error.kind.value, reason=redact(str(error)),
                      result="switch_to_next")
        return None, None, failures

    # ---------- статус (без ключей) ----------
    def status(self):
        """Статус всех провайдеров и ключей: только номера, без значений ключей."""
        now = self._now()
        with self._lock:
            out = {}
            for name, entry in self._providers.items():
                out[name] = {
                    "models": list(entry.models),
                    "keys": [
                        {
                            "index": k.index,
                            "available": k.available(now),
                            "invalid": k.invalid,
                            "cooldown_remaining_sec": k.cooldown_remaining(now),
                            "auth_failures": k.auth_failures,
                            "last_error": k.last_error,
                        }
                        for k in entry.keys
                    ],
                }
            return out


# ---------- Общий реестр по умолчанию (заполняется из окружения) ----------
def _env_keys(*names):
    """Collect base and numbered provider keys in stable, de-duplicated order."""
    import os
    values = []
    for name in names:
        candidates = [name, *(f"{name}_{index}" for index in range(1, 10))]
        for candidate in candidates:
            value = os.getenv(candidate, "").strip()
            if value and value not in values:
                values.append(value)
    return values


def default_manager():
    """ProviderManager со статусом ключей и cooldown для настроенных провайдеров.

    Выбор провайдера/модели остаётся за пользователем; этот реестр не выполняет
    автоматическую подмену провайдера или модели.
    """
    import os
    manager = ProviderManager()

    groq_keys = [os.getenv(f"GROQ_API_KEY_{i}", "").strip() for i in range(1, 10)]
    groq_keys = [k for k in groq_keys if k]
    if not groq_keys and os.getenv("GROQ_API_KEY", "").strip():
        groq_keys = [os.getenv("GROQ_API_KEY").strip()]
    if groq_keys:
        manager.register_provider("groq", keys=groq_keys,
                                  models=["openai/gpt-oss-120b", "openai/gpt-oss-20b",
                                          "qwen/qwen3.8-27b"])

    cerebras = _env_keys("CEREBRAS_API_KEY")
    if cerebras:
        manager.register_provider("cerebras", keys=cerebras,
                                  models=["qwen-3-235b-a22b-instruct-2507", "zai-glm-4.7"])

    openrouter = _env_keys("OPENROUTER_API_KEY")
    if openrouter:
        manager.register_provider("openrouter", keys=openrouter, models=[])

    gemini = _env_keys("GEMINI_API_KEY", "GOOGLE_AI_STUDIO_KEY")
    if gemini:
        manager.register_provider("google_ai_studio", keys=gemini,
                                  models=["gemini-3.8-flash"])

    pollinations = _env_keys("POLLINATIONS_API_KEY", "POLLINATIONS_KEY", "POLLINATIONS_TOKEN")
    if pollinations:
        manager.register_provider("pollinations", keys=pollinations, models=[])

    oai = _env_keys("OPENAI_COMPATIBLE_KEY", "OPENAI_API_KEY")
    if oai:
        manager.register_provider("openai_compatible", keys=oai,
                                  models=[os.getenv("LOCAL_MODEL_ID", "local-llama")])

    return manager


def classify_error_text(text):
    """Классификация по тексту ошибки транспорта (для обратной совместимости
    с функциями, возвращающими (data, error_string))."""
    lowered = str(text or "").lower()
    if "401" in lowered or "auth" in lowered and "ключ" in lowered:
        return ErrorKind.AUTH
    if "403" in lowered or "forbidden" in lowered or "запрещён" in lowered:
        return ErrorKind.FORBIDDEN
    if "404" in lowered or "не найден" in lowered or "not found" in lowered:
        return ErrorKind.NOT_FOUND
    if "429" in lowered or "rate limit" in lowered or "лимит запросов" in lowered:
        return ErrorKind.RATE_LIMIT
    if "billing" in lowered or "insufficient funds" in lowered or "оплат" in lowered:
        return ErrorKind.BILLING
    if "quota" in lowered or "квота" in lowered:
        return ErrorKind.QUOTA
    if "таймаут" in lowered or "timeout" in lowered:
        return ErrorKind.TIMEOUT
    if "недоступен" in lowered or "connection" in lowered or "network" in lowered:
        return ErrorKind.NETWORK
    if "5" in lowered and any(f"http 5" in lowered for _ in (0,)):
        return ErrorKind.SERVER
    if "некорректный json" in lowered or "неожиданный формат" in lowered:
        return ErrorKind.BAD_RESPONSE
    return ErrorKind.UNKNOWN
