"""Characterization tests: public contracts fixed BEFORE hardening changes.

These tests intentionally assert only the behaviour that already exists on the
baseline commit (868df79).  If a hardening patch changes any of these
contracts, the test must be updated explicitly and the change documented in
the migration notes.
"""
import json

import pytest

import config


def _ndjson(response):
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


# ---------- health / status ----------

def test_characterization_health_contract(client):
    data = client.get("/api/system/health").get_json()
    assert {"ai", "search", "linux", "agent"} <= set(data)
    assert {"provider", "model", "configured", "offline"} <= set(data["ai"])
    assert {"enabled", "tools"} <= set(data["agent"])
    assert client.get("/api/search/health").status_code == 200


def test_characterization_agent_status_contract(client):
    data = client.get("/api/agent/status").get_json()
    assert data["engine"] == "react"
    assert isinstance(data["enabled"], bool)
    assert isinstance(data["guest"], bool)
    assert {"image", "audio", "video"} <= set(data["media"])


# ---------- chats / ownership / session isolation ----------

def test_characterization_chat_crud_contract(client):
    assert client.get("/api/chats").get_json()["chats"] == []
    created = client.post("/api/chats", json={"title": "characterization"}).get_json()
    chat_id = created["chat_id"]
    assert chat_id
    assert client.get(f"/api/chats/{chat_id}").status_code == 200
    assert client.delete(f"/api/chats/{chat_id}").status_code in (200, 204)
    assert client.get(f"/api/chats/{chat_id}").status_code == 404


def test_characterization_owner_cannot_read_another_owners_chat(client):
    """Anon identity is a signed browser cookie; a second client is a second owner."""
    from app import app as flask_app

    chat_id = client.post("/api/chats", json={"title": "private"}).get_json()["chat_id"]
    with flask_app.test_client() as other:
        assert other.get(f"/api/chats/{chat_id}").status_code == 404
        assert other.post("/send", json={"message": "hi", "chat_id": chat_id}).status_code == 404


def test_characterization_session_check_and_logout(client):
    data = client.get("/api/session/check").get_json()
    assert "admin" in data
    assert client.post("/api/session/logout", json={}).status_code == 200


# ---------- chat completion / streaming ----------

def test_characterization_send_stream_ndjson_contract(client, monkeypatch):
    """Offline demo provider must keep emitting NDJSON with a terminal event."""
    monkeypatch.setattr(config, "current_provider", "local_demo")
    monkeypatch.setattr(config, "current_model", "nova-local-1")
    response = client.post("/send_stream", json={"message": "привет", "reasoning": False})
    assert response.status_code == 200
    events = _ndjson(response)
    assert events, "stream must contain at least one event"
    assert any("done" in event for event in events)


# ---------- upload ----------

def test_characterization_attachment_endpoint_contract(client, monkeypatch):
    import io

    import routes.media as media_routes

    monkeypatch.setattr(
        media_routes, "_call_selected_text_ai",
        lambda prompt: ("characterized analysis", None),
    )
    response = client.post(
        "/api/attachments/analyze",
        data={"file": (io.BytesIO(b"hello from test"), "notes.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert response.get_json()["filename"] == "notes.txt"


# ---------- auth / admin ----------

def test_characterization_admin_login_contract(client, monkeypatch):
    import routes.admin as admin_routes

    monkeypatch.setitem(admin_routes.ADMIN_CREDENTIALS, "admin", "characterization-code")
    bad = client.post("/api/admin/login", json={"username": "admin", "code": "wrong"})
    assert bad.status_code == 200 and bad.get_json()["success"] is False
    ok = client.post("/api/admin/login", json={"username": "admin", "code": "characterization-code"})
    assert ok.status_code == 200 and ok.get_json()["success"] is True
    assert client.get("/api/admin/check").get_json()["logged_in"] is True


# ---------- agent tool policy / confirmation ----------

def test_characterization_confirmation_validation(client):
    missing = client.post("/api/agent/confirm", json={})
    assert missing.status_code == 400 and missing.get_json()["ok"] is False
    unknown = client.post("/api/agent/confirm", json={"id": "does-not-exist", "approve": True})
    assert unknown.status_code == 409
    permissions = client.get("/api/agent/permissions").get_json()
    assert permissions["ok"] is True and "mode" in permissions


@pytest.mark.parametrize("path", ["/api/agent/runs", "/api/usage"])
def test_characterization_read_only_api_contract(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert isinstance(response.get_json(), dict)


# ---------- provider selection (no network) ----------

def test_characterization_provider_registry_contract():
    for name in ("groq", "google_ai_studio", "openai_compatible", "local_demo"):
        assert name in config.PROVIDERS
        assert set(config.PROVIDERS[name]) >= {"url", "max_tokens", "models"}
    assert isinstance(config.has_credentials("groq"), bool)
    assert config.has_credentials("local_demo") is True
