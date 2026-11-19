"""Capability registry, token/cost accounting and circuit breaker."""
import pytest

import config
import provider_policy as pp


def test_registry_covers_configured_providers():
    catalog = pp.public_catalog()
    providers = {item["provider"] for item in catalog}
    assert {"groq", "google_ai_studio", "openai_compatible", "local_demo"} <= providers
    assert all("configured" in item and "context_window" in item for item in catalog)


def test_capabilities_fallback_is_conservative():
    caps = pp.capabilities("unknown", "unknown-model")
    assert caps.context_window == 4096
    assert caps.free is False
    assert caps.max_output_tokens == 2048


def test_token_and_cost_accounting():
    assert pp.estimate_tokens("abcd") == 1
    assert pp.estimate_tokens("") == 1
    ledger = pp.TokenLedger()
    entry = ledger.record("anon:1", "groq", "test-model", 1000, 500)
    assert entry["calls"] == 1 and entry["tokens"] == 1500
    assert entry["cost"] == pytest.approx(pp.estimate_cost("groq", "test-model", 1000, 500))


def test_token_budget(monkeypatch):
    monkeypatch.setenv("MODEL_TOKEN_BUDGET", "100")
    ledger = pp.TokenLedger()
    assert ledger.allowed("anon:1", 50) == (True, "")
    ledger.record("anon:1", "groq", "m", 80, 20)
    allowed, message = ledger.allowed("anon:1", 50)
    assert allowed is False and message


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_circuit_breaker_states():
    clock = FakeClock()
    breaker = pp.CircuitBreaker(threshold=2, cooldown=10, clock=clock)
    assert breaker.state == "closed" and breaker.allow() is True
    breaker.record_failure()
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.allow() is False and breaker.state == "open"
    clock.now = 11
    assert breaker.state == "half-open" and breaker.allow() is True
    breaker.record_success()
    assert breaker.state == "closed"


def test_breaker_registry_is_per_provider():
    first = pp.breaker("unit-test-provider-a")
    second = pp.breaker("unit-test-provider-b")
    assert first is not second
    assert pp.breaker("unit-test-provider-a") is first


def test_capabilities_endpoint_has_no_key_material(client):
    response = client.get("/api/ai/capabilities")
    assert response.status_code == 200
    assert isinstance(response.get_json()["models"], list)
    assert "api_key" not in response.get_data(as_text=True).lower()
