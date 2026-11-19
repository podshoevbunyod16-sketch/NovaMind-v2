"""CSRF protection for all cookie-authenticated state-changing requests."""
import pytest

MUTATING_ENDPOINTS = (
    ("post", "/api/admin/settings", {"json": {}}),
    ("post", "/api/composio/execute", {"json": {}}),
    ("post", "/api/attachments/analyze", {"data": {}}),
    ("delete", "/api/history/clear", {}),
    ("post", "/api/terminal/run", {"json": {}}),
)


@pytest.mark.parametrize(("method", "path", "payload"), MUTATING_ENDPOINTS)
def test_cookie_authenticated_mutations_reject_missing_or_invalid_csrf(
    client, method, path, payload
):
    for token in ("", "wrong-session-token"):
        response = getattr(client, method)(
            path, headers={"X-CSRF-Token": token}, **payload
        )
        assert response.status_code == 403, (method, path)
        assert response.get_json() == {"error": "CSRF validation failed"}


def test_csrf_token_is_bound_to_session_and_available_without_cache(client):
    response = client.get("/api/csrf-token")
    token = response.get_json()["csrf_token"]
    cookie = client.get_cookie("nova_csrf")
    assert token and cookie and cookie.value == token
    assert response.headers["Cache-Control"] == "no-store"
    assert cookie.http_only is False
    assert cookie.same_site == "Strict"

    other = client.application.test_client()
    other.get("/api/csrf-token")
    cross_session = other.post(
        "/api/settings/lang",
        json={"language": "en"},
        headers={"X-CSRF-Token": token},
    )
    assert cross_session.status_code == 403


def test_valid_same_origin_csrf_passes_and_foreign_origin_is_rejected(admin_client):
    token = admin_client.get_cookie("nova_csrf").value
    accepted = admin_client.post(
        "/api/admin/settings",
        json={},
        headers={"X-CSRF-Token": token, "Origin": "http://localhost"},
    )
    assert accepted.status_code == 200

    rejected = admin_client.post(
        "/api/admin/settings",
        json={},
        headers={"X-CSRF-Token": token, "Origin": "https://attacker.example"},
    )
    assert rejected.status_code == 403

    wrong_scheme = admin_client.post(
        "/api/admin/settings",
        json={},
        headers={"X-CSRF-Token": token, "Origin": "https://localhost"},
    )
    assert wrong_scheme.status_code == 403

    invalid_origin = admin_client.post(
        "/api/admin/settings",
        json={},
        headers={"X-CSRF-Token": token, "Origin": "http://localhost/path"},
    )
    assert invalid_origin.status_code == 403


def test_fetch_metadata_cross_site_is_rejected_even_with_a_token(client):
    token = client.get_cookie("nova_csrf").value
    response = client.post(
        "/api/settings/lang",
        json={"language": "en"},
        headers={"X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
