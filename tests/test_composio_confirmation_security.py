"""Composio external actions require a server-side, session-bound confirmation."""

import pytest


@pytest.fixture
def composio_key(monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-composio-key")


def _login_admin(client):
    with client.session_transaction() as flask_session:
        flask_session["admin_logged_in"] = True
        flask_session["admin_username"] = "admin"


def test_execute_only_creates_challenge_and_never_calls_provider(
    admin_client, composio_key, monkeypatch
):
    from routes import composio

    provider_calls = []
    monkeypatch.setattr(composio.requests, "post", lambda *args, **kwargs: provider_calls.append(args))

    response = admin_client.post(
        "/api/composio/execute",
        json={"action": "GMAIL_SEND_EMAIL", "params": {"to": "user@example.test"}},
    )

    assert response.status_code == 409
    body = response.get_json()
    assert body["confirmation_required"] is True
    assert body["challenge_id"]
    assert provider_calls == []


def test_confirmation_requires_matching_pending_session_operation(
    admin_client, composio_key, monkeypatch
):
    from app import app as flask_app
    from routes import composio

    provider_calls = []
    monkeypatch.setattr(
        composio.requests,
        "post",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )
    challenge = admin_client.post(
        "/api/composio/execute",
        json={"action": "GMAIL_SEND_EMAIL", "params": {"to": "user@example.test"}},
    ).get_json()["challenge_id"]

    # A boolean and challenge from another authenticated session cannot approve it.
    other_client = flask_app.test_client()
    _login_admin(other_client)
    foreign = other_client.post(
        "/api/composio/confirm",
        json={"challenge_id": challenge, "confirmed": True},
    )
    assert foreign.status_code == 409
    assert provider_calls == []

    # The owning session still requires the exact, explicit confirmation shape.
    missing_boolean = admin_client.post(
        "/api/composio/confirm",
        json={"challenge_id": challenge, "confirmed": "true"},
    )
    assert missing_boolean.status_code == 400
    assert provider_calls == []


def test_confirm_executes_once_then_consumes_challenge(
    admin_client, composio_key, monkeypatch
):
    from app import app as flask_app
    from routes import composio
    from tests.conftest import FakeResponse

    provider_calls = []

    def fake_post(*args, **kwargs):
        provider_calls.append((args, kwargs))
        return FakeResponse(json_data={"id": "provider-result"})

    monkeypatch.setattr(composio.requests, "post", fake_post)
    challenge = admin_client.post(
        "/api/composio/execute",
        json={
            "action": "GMAIL_SEND_EMAIL",
            "params": {"to": "user@example.test", "body": "approved"},
            "connected_account_id": "acct-1",
        },
    ).get_json()["challenge_id"]
    old_session_cookie = admin_client.get_cookie("session")
    replay_client = flask_app.test_client()
    replay_client.set_cookie("session", old_session_cookie.value)

    confirmed = admin_client.post(
        "/api/composio/confirm",
        json={"challenge_id": challenge, "confirmed": True},
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json() == {"result": {"id": "provider-result"}}
    assert len(provider_calls) == 1
    assert provider_calls[0][1]["json"] == {
        "input": {"to": "user@example.test", "body": "approved"},
        "allow_tracing": True,
        "connected_account_id": "acct-1",
    }

    # Replaying the original signed client-side cookie must not restore the
    # server-side one-shot challenge after it has already been consumed.
    replay = replay_client.post(
        "/api/composio/confirm",
        json={"challenge_id": challenge, "confirmed": True},
    )
    assert replay.status_code == 409
    assert len(provider_calls) == 1
