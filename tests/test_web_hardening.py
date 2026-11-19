"""Security-hardening checks: headers, cookies, CSRF, request IDs, redaction."""
import json
import re
from pathlib import Path

import pytest

import observability
import web_hardening


def test_app_source_never_prints_admin_code():
    source = Path("app.py").read_text(encoding="utf-8")
    assert "ADMIN_CODE)" not in source
    assert "код администратора:" not in source


def test_security_headers_present_on_html_and_json(client):
    for path in ("/chat", "/api/agent/status"):
        response = client.get(path)
        for header in ("Content-Security-Policy", "X-Content-Type-Options",
                       "Referrer-Policy", "Permissions-Policy", "X-Frame-Options"):
            assert header in response.headers, (path, header)
        csp = response.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp
        assert "default-src 'self'" in csp
    assert client.get("/chat").headers["X-Content-Type-Options"] == "nosniff"


def test_request_id_is_echoed_and_client_cannot_inject_arbitrary_value(client):
    unique = "corr-12345678"
    assert client.get("/healthz", headers={"X-Request-ID": unique}).headers["X-Request-ID"] == unique
    forged = client.get("/healthz", headers={"X-Request-ID": "bad value!"}).headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", forged)


def test_health_readiness_and_metrics_endpoints(client):
    health = client.get("/healthz")
    assert health.status_code == 200 and health.get_json()["status"] == "ok"
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.get_json()["status"] == "ready"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200 and "novamind_http_requests_total" in metrics.get_data(as_text=True)


def test_json_error_has_request_id_without_traceback(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    body = response.get_json()
    assert body["code"] == "not_found"
    assert re.fullmatch(r"[0-9a-f]{32}", body["request_id"])
    assert "Traceback" not in response.get_data(as_text=True)


def test_csrf_rejects_cross_site_origin_with_cookie(client):
    client.get("/chat")  # establishes the session cookie
    response = client.post("/api/session/login", json={"nick": "test"},
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.get_json()["code"] == "csrf_failed"


def test_csrf_allows_same_origin_and_requests_without_cookies(client):
    assert client.post("/api/session/login", json={"nick": "test"}).status_code == 200
    client.get("/chat")
    response = client.post("/api/session/login", json={"nick": "test"},
                           headers={"Origin": "http://localhost"})
    assert response.status_code == 200


def test_csrf_double_submit_strict_mode(client, monkeypatch):
    monkeypatch.setenv("CSRF_REQUIRE_HEADER", "1")
    client.get("/chat")
    token_response = client.get("/api/csrf-token")
    token = token_response.get_json()["token"]
    assert token
    denied = client.post("/api/session/login", json={"nick": "test"})
    assert denied.status_code == 403
    allowed = client.post("/api/session/login", json={"nick": "test"},
                          headers={web_hardening.CSRF_HEADER: token})
    assert allowed.status_code == 200


def test_cookie_flags_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    assert web_hardening.is_production() is True
    assert web_hardening.cookie_secure() is True
    problems = web_hardening.validate_production_config()
    assert any("ADMIN_CODE" in item for item in problems)
    assert any("SESSION_SECRET" in item for item in problems)


@pytest.mark.parametrize("secret", [
    "sk-abcdefghijklmnopqrstuvwxyz",
    "Bearer abcdef1234567890",
    'api_key: "supersecretvalue"',
    "password=hunter2secret",
])
def test_redaction_removes_secret_values(secret):
    safe = observability.redact("request failed: " + secret)
    assert "supersecretvalue" not in safe
    assert "hunter2secret" not in safe
    assert "abcdefghijklmnopqrstuvwxyz" not in safe
    assert "abcdef1234567890" not in safe
    assert "[REDACTED" in safe


def test_metrics_counters_are_thread_safe_and_bounded():
    counter = observability.Metrics()
    for _ in range(5):
        counter.inc("requests_total", 1, route="chat")
    assert counter.snapshot()['requests_total{route="chat"}'] == 5
    assert "novamind_requests_total" in counter.prometheus()
