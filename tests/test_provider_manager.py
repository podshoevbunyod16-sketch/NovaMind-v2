"""Тесты единого Provider Manager: классификация ошибок, ротация ключей,
cooldown, backoff с jitter, failover между провайдерами, защита ключей."""
import time

import pytest
import requests

import provider_manager as pm


def make_manager(keys=("k1", "k2", "k3"), sleeps=None):
    manager = pm.ProviderManager(sleep=lambda s: (sleeps or []).append(s))
    manager.register_provider("groq", keys=list(keys), models=["m1"])
    manager.register_provider("cerebras", keys=["c1"], models=["m2"])
    return manager


# ---------- классификация ошибок ----------

@pytest.mark.parametrize("status,expected", [
    (401, pm.ErrorKind.AUTH),
    (403, pm.ErrorKind.FORBIDDEN),
    (404, pm.ErrorKind.NOT_FOUND),
    (429, pm.ErrorKind.RATE_LIMIT),
    (500, pm.ErrorKind.SERVER),
    (503, pm.ErrorKind.SERVER),
])
def test_classify_http_statuses(status, expected):
    kind, _ = pm.classify_response(status, {}, "")
    assert kind == expected


def test_classify_429_daily_quota():
    kind, retry = pm.classify_response(429, {"Retry-After": "120"},
                                       "tokens per day exceeded")
    assert kind == pm.ErrorKind.QUOTA
    assert retry == 120


def test_classify_billing_and_quota_text():
    assert pm.classify_response(400, {}, "insufficient funds")[0] == pm.ErrorKind.BILLING
    assert pm.classify_response(400, {}, "daily quota depleted")[0] == pm.ErrorKind.QUOTA


def test_classify_exceptions():
    assert pm.classify_exception(requests.exceptions.Timeout()) == pm.ErrorKind.TIMEOUT
    assert pm.classify_exception(requests.exceptions.ConnectionError()) == pm.ErrorKind.NETWORK
    assert pm.classify_exception(ValueError("bad json")) == pm.ErrorKind.BAD_RESPONSE


# ---------- Retry-After и backoff ----------

def test_parse_retry_after_seconds_and_date():
    assert pm.parse_retry_after({"Retry-After": "45"}) == 45
    assert pm.parse_retry_after({"X-RateLimit-Reset": "30s"}) == 30
    assert pm.parse_retry_after({}, default=7) == 7


def test_backoff_uses_retry_after_and_jitter():
    assert pm.backoff_delay(0, retry_after=99) == 99
    for attempt in range(5):
        delay = pm.backoff_delay(attempt, base=1.0, cap=30.0, jitter=0.5)
        base = min(30.0, 2 ** attempt)
        assert 0.5 * base <= delay <= base  # jitter не выходит за базовый диапазон


# ---------- ротация и cooldown ----------

def test_rotation_skips_cooldown_keys_and_recovers_after_cooldown():
    now = [1000.0]
    manager = pm.ProviderManager(sleep=lambda s: None, now=lambda: now[0])
    manager.register_provider("groq", keys=["a", "b"])
    err = pm.ProviderError(pm.ErrorKind.RATE_LIMIT, "limited", retry_after=60)
    manager.mark_key_result("groq", 0, err)
    assert manager.available_key("groq").key == "b"
    now[0] += 61  # cooldown истёк — ключ снова доступен
    assert manager.available_key("groq").key == "a"
    manager.mark_key_result("groq", 0, pm.ProviderError(pm.ErrorKind.RATE_LIMIT, "x", retry_after=60))
    assert manager.available_key("groq").key == "b"  # ключ в cooldown не используется повторно


def test_auth_failures_disable_key_permanently():
    manager = make_manager(keys=["a"])
    for _ in range(pm.AUTH_FAILURE_THRESHOLD):
        manager.mark_key_result("groq", 0, pm.ProviderError(pm.ErrorKind.AUTH, "401"))
    assert manager.all_keys_unavailable("groq")
    assert manager.available_key("groq") is None


def test_execute_bounded_and_no_key_reuse():
    manager = make_manager(keys=["a", "b"])
    seen = []

    def request_fn(key):
        seen.append(key.index)
        raise pm.ProviderError(pm.ErrorKind.RATE_LIMIT, "429", retry_after=0)

    result, error = manager.execute("groq", "m1", request_fn)
    assert result is None and error.kind == pm.ErrorKind.RATE_LIMIT
    assert len(seen) == 2  # каждый ключ попробован ровно один раз — без зацикливания
    assert sorted(seen) == [0, 1]


def test_execute_stops_immediately_on_non_retryable_error():
    manager = make_manager()
    calls = []

    def request_fn(key):
        calls.append(key.index)
        raise pm.ProviderError(pm.ErrorKind.NOT_FOUND, "model not found")

    _, error = manager.execute("groq", "m1", request_fn)
    assert error.kind == pm.ErrorKind.NOT_FOUND
    assert len(calls) == 1  # 404 не повторяется на других ключах


def test_execute_success_resets_auth_failures():
    now = [1000.0]
    manager = pm.ProviderManager(sleep=lambda s: None, now=lambda: now[0])
    manager.register_provider("groq", keys=["a"])
    manager.mark_key_result("groq", 0, pm.ProviderError(pm.ErrorKind.AUTH, "401"))
    now[0] += 61  # cooldown после 401 истёк
    result, error = manager.execute("groq", "m1", lambda key: "ok")
    assert result == "ok" and error is None
    assert manager.status()["groq"]["keys"][0]["auth_failures"] == 0


# ---------- failover между провайдерами ----------

def test_failover_to_next_provider_when_all_keys_down():
    manager = make_manager(keys=["a"])
    manager.mark_key_result("groq", 0, pm.ProviderError(pm.ErrorKind.QUOTA, "daily"))
    calls = []

    def request_fn(key):
        calls.append(key.key)
        if key.key == "a":
            raise pm.ProviderError(pm.ErrorKind.RATE_LIMIT, "429")
        return "cerebras answer"

    result, provider, failures = manager.execute_with_failover(
        ["groq", "cerebras"], "m", request_fn)
    assert result == "cerebras answer"
    assert provider == "cerebras"
    assert failures and failures[0]["provider"] == "groq"
    assert calls == ["c1"]  # заблокированный ключ groq повторно не использовался


def test_failover_reports_all_providers_down():
    manager = make_manager(keys=["a"])

    def request_fn(key):
        raise pm.ProviderError(pm.ErrorKind.SERVER, "503")

    result, provider, failures = manager.execute_with_failover(
        ["groq", "cerebras"], "m", request_fn)
    assert result is None and provider is None
    assert {f["provider"] for f in failures} == {"groq", "cerebras"}


# ---------- безопасность ключей ----------

def test_status_and_logs_never_expose_keys(caplog):
    manager = make_manager(keys=["super-secret-key-123"])
    manager.mark_key_result("groq", 0, pm.ProviderError(pm.ErrorKind.AUTH, "Bearer super-secret-key-123 rejected"))
    with caplog.at_level("INFO", logger="novamind.providers"):
        pm.log_event("warning", "key_cooldown", provider="groq", key_index=0,
                     reason="Authorization: Bearer super-secret-key-123 bad", cooldown_sec=60)
        manager.status()
    assert "super-secret-key-123" not in caplog.text
    status = manager.status()
    assert "super-secret-key-123" not in str(status)
    assert status["groq"]["keys"][0]["index"] == 0


def test_redact_strips_bearer_and_key_values():
    assert "abc123token" not in pm.redact("Authorization: Bearer abc123token", ["abc123token"])
    assert "api_key=xyz" not in pm.redact("api_key=xyz")


# ---------- интеграция с ai_providers ----------

def test_default_manager_registers_all_env_providers(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY_1", "g1")
    monkeypatch.setenv("GROQ_API_KEY_2", "g2")
    monkeypatch.setenv("CEREBRAS_API_KEY", "c1")
    monkeypatch.setenv("GEMINI_API_KEY", "gm1")
    manager = pm.default_manager()
    names = manager.provider_names()
    assert {"groq", "cerebras", "google_ai_studio"} <= set(names)
    assert len(manager.status()["groq"]["keys"]) == 2
    assert "g1" not in str(manager.status())


def test_ai_providers_failover_skips_provider_with_all_keys_in_cooldown(monkeypatch):
    import ai_providers
    monkeypatch.setattr(ai_providers, "resolve_target", lambda *a, **k: (
        "local_demo", "nova-local-1", {}))
    import config
    monkeypatch.setattr(config, "has_credentials", lambda provider: True)
    manager = pm.ProviderManager(sleep=lambda s: None)
    manager.register_provider("groq", keys=["k1"])
    manager.mark_key_result("groq", 0, pm.ProviderError(pm.ErrorKind.QUOTA, "daily quota"))
    monkeypatch.setattr(ai_providers, "_PM", manager)
    targets = ai_providers.free_failover_targets("groq", "openai/gpt-oss-120b")
    assert all(p != "groq" for p, _m in targets)


def test_ai_providers_marks_pm_failure_on_transport_error(monkeypatch):
    import ai_providers
    manager = pm.ProviderManager(sleep=lambda s: None)
    manager.register_provider("cerebras", keys=["c1"])
    monkeypatch.setattr(ai_providers, "_PM", manager)
    monkeypatch.setattr(ai_providers, "resolve_target", lambda *a, **k: (
        "cerebras", "m", {"url": "https://api.cerebras.ai/v1/chat/completions", "max_tokens": 8}))
    monkeypatch.setattr(ai_providers, "openai_compatible_request",
                        lambda *a, **k: (None, "Сервер вернул HTTP 429: rate limit"))
    monkeypatch.setattr(ai_providers, "provider_headers", lambda p: {})
    text, meta = ai_providers._chat_completion_once([{"role": "user", "content": "hi"}])
    assert text is None
    key = manager.status()["cerebras"]["keys"][0]
    assert key["invalid"] is False               # 429 → cooldown, а не permanent disable
    assert key["cooldown_remaining_sec"] > 0      # ключ временно заблокирован
    assert key["last_error"] == "rate_limit"



def test_default_manager_tracks_current_gemini_model_id(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    manager = pm.default_manager()
    assert manager.status()["google_ai_studio"]["models"] == ["gemini-3.8-flash"]
