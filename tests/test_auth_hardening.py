"""Session primitives: rotation, logout everywhere, ownership checks, RBAC."""
import pytest

import auth


def _login(client, monkeypatch):
    import routes.admin as admin_routes

    monkeypatch.setitem(admin_routes.ADMIN_CREDENTIALS, "admin", "auth-test-code")
    response = client.post("/api/admin/login", json={"username": "admin", "code": "auth-test-code"})
    assert response.status_code == 200
    return response


@pytest.fixture(autouse=True)
def restore_epoch(monkeypatch):
    original = auth._admin_epoch
    yield
    auth._admin_epoch = original


def test_login_sets_admin_epoch_and_legacy_sessions_still_work(client, monkeypatch):
    _login(client, monkeypatch)
    assert client.get("/api/admin/check").get_json()["logged_in"] is True
    with client.session_transaction() as sess:
        assert sess["admin_epoch"] == auth._admin_epoch
    # Sessions created before this feature have no epoch: compatibility mode.
    with client.session_transaction() as sess:
        sess.pop("admin_epoch", None)
    assert client.get("/api/admin/check").get_json()["logged_in"] is True


def test_strict_mode_rejects_legacy_and_revoked_epochs(client, monkeypatch):
    monkeypatch.setenv("ADMIN_LEGACY_SESSIONS", "0")
    with client.session_transaction() as sess:
        sess["admin_logged_in"] = True
    assert auth.is_admin() is False
    with client.session_transaction() as sess:
        sess["admin_epoch"] = "outdated"
    assert client.get("/api/admin/check").get_json()["logged_in"] is False


def test_logout_everywhere_revokes_all_admin_browsers(client, monkeypatch):
    from app import app as flask_app

    _login(client, monkeypatch)
    second = flask_app.test_client()
    second.post("/api/admin/login", json={"username": "admin", "code": "auth-test-code"})
    assert second.get("/api/admin/check").get_json()["logged_in"] is True
    revoked = client.post("/api/admin/sessions/logout-all", json={})
    assert revoked.status_code == 200
    assert second.get("/api/admin/check").get_json()["logged_in"] is False
    assert client.get("/api/admin/check").get_json()["logged_in"] is False


def test_logout_everywhere_requires_admin(client):
    assert client.post("/api/admin/sessions/logout-all", json={}).status_code == 403


def test_rotation_keeps_chat_owner_but_changes_version(client):
    client.post("/api/chats", json={"title": "keep me"})
    before = client.get("/api/chats").get_json()["chats"]
    assert before
    response = client.post("/api/session/rotate", json={})
    assert response.status_code == 200
    with client.session_transaction() as sess:
        assert sess["auth_version"]
    assert client.get("/api/chats").get_json()["chats"] == before


def test_ownership_check():
    assert auth.can_access("anon:1", "anon:1") is True
    assert auth.can_access("anon:1", "anon:2") is False
    assert auth.can_access("anon:1", "admin:x", auth.Role.ADMIN) is True
    assert auth.can_access("", "", auth.Role.OWNER) is False


def test_rbac_model_is_available():
    assert {role.value for role in auth.Role} >= {"owner", "admin", "member", "viewer", "guest"}
    workspace = auth.Workspace(id="w1", owner_id="u1")
    membership = auth.Membership(user_id="u2", organization_id="o1", role=auth.Role.VIEWER)
    assert workspace.owner_id == "u1" and membership.role is auth.Role.VIEWER
