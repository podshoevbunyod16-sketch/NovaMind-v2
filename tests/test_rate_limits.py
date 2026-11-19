"""Per-endpoint abuse limits (in-process fallback, Redis-ready interface)."""
import io

import security


def test_endpoint_rules():
    assert security.endpoint_limiter.rule_for("/api/attachments/analyze")[1] == "UPLOAD"
    assert security.endpoint_limiter.rule_for("/api/terminal/run")[1] == "TERMINAL"
    assert security.endpoint_limiter.rule_for("/api/media/image")[1] == "MEDIA"
    assert security.endpoint_limiter.rule_for("/static/app.js")[1] == ""


def test_upload_endpoint_is_rate_limited(client, monkeypatch):
    import routes.media as media_routes

    monkeypatch.setenv("RATE_UPLOAD", "2")
    monkeypatch.setattr(media_routes, "_call_selected_text_ai", lambda prompt: ("ok", None))
    payload = {"file": (io.BytesIO(b"hello"), "note.txt")}
    for _ in range(2):
        fresh = {"file": (io.BytesIO(b"hello"), "note.txt")}
        assert client.post("/api/attachments/analyze", data=fresh,
                           content_type="multipart/form-data").status_code in (200, 400, 415)
    blocked = client.post("/api/attachments/analyze", data=payload,
                          content_type="multipart/form-data")
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) > 0


def test_login_uses_dedicated_limiter_not_endpoint_limiter(client):
    # Five failed admin logins are handled by LoginLimiter (200 + 429), not 403.
    for _ in range(5):
        response = client.post("/api/admin/login", json={"username": "admin", "code": "wrong"})
        assert response.status_code == 200
    assert client.post("/api/admin/login", json={"username": "admin", "code": "wrong"}).status_code == 429
