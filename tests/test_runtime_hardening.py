"""Regression tests for bootstrap authentication and browser security defaults."""

import logging


def test_run_is_fail_closed_when_agent_token_is_missing(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "AGENT_TOKEN", "")
    response = client.post("/run", json={"input": {"text": "should not run"}})

    assert response.status_code == 503
    assert response.get_json() == {"error": "agent endpoint is not configured"}


def test_run_rejects_empty_or_wrong_token_without_provider_call(client, monkeypatch):
    import ai_providers
    import app as app_module

    monkeypatch.setattr(app_module, "AGENT_TOKEN", "runtime-test-token")
    provider_called = False

    def unexpected_provider_call(*args, **kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("provider must not be called for rejected authentication")

    monkeypatch.setattr(ai_providers, "chat_completion", unexpected_provider_call)
    assert client.post("/run", json={"text": "x"}).status_code == 403
    assert client.post(
        "/run", headers={"X-Agent-Token": "wrong"}, json={"text": "x"}
    ).status_code == 403
    assert not provider_called


def test_run_accepts_exact_configured_token_with_mocked_provider(client, monkeypatch):
    import ai_providers
    import app as app_module

    monkeypatch.setattr(app_module, "AGENT_TOKEN", "runtime-test-token")
    monkeypatch.setattr(
        ai_providers, "chat_completion", lambda *args, **kwargs: ("ok", {})
    )

    response = client.post(
        "/run", headers={"X-Agent-Token": "runtime-test-token"}, json={"text": "x"}
    )
    assert response.status_code == 200
    assert response.get_json()["output"]["answer"] == "ok"


def test_security_headers_and_cookie_flags_are_centralized(client):
    import app as app_module

    response = client.get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert "'unsafe-eval'" not in response.headers["Content-Security-Policy"]
    assert app_module.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app_module.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app_module.app.config["SESSION_COOKIE_SECURE"] is False
    assert "Strict-Transport-Security" not in response.headers


def test_admin_code_status_log_does_not_include_generated_value(caplog):
    import app as app_module
    import config

    with caplog.at_level(logging.INFO, logger=app_module.__name__):
        message = "ADMIN_CODE не задан" if app_module.ADMIN_CODE_IS_DEFAULT else "ADMIN_CODE задан"
        logging.getLogger(app_module.__name__).info(message)

    assert config.ADMIN_CODE not in caplog.text
    assert "ADMIN_CODE задан" in caplog.text or "ADMIN_CODE не задан" in caplog.text
