"""Regression tests for admin authorization and Google token verification."""
from routes import admin

SENSITIVE_ADMIN_ROUTES = (
    ("get", "/api/admin/stats"),
    ("post", "/api/admin/settings"),
    ("post", "/api/admin/save_code"),
    ("post", "/api/admin/run_saved_code"),
    ("get", "/api/admin/saved_codes"),
    ("get", "/api/admin/load_code?file=missing.py"),
    ("get", "/api/admin/groq_keys"),
    ("post", "/api/admin/groq_keys/reset"),
)


def _request(client, method, path):
    return getattr(client, method)(path, json={} if method == "post" else None)


def test_sensitive_admin_apis_deny_unauthenticated(client):
    for method, path in SENSITIVE_ADMIN_ROUTES:
        response = _request(client, method, path)
        assert response.status_code == 401, (method, path, response.get_data(as_text=True))
        assert response.get_json() == {"error": "Требуется вход администратора"}


def test_sensitive_admin_apis_allow_authenticated_access(admin_client, monkeypatch):
    monkeypatch.setattr(admin, "get_groq_key_status", list)
    for method, path in SENSITIVE_ADMIN_ROUTES:
        response = _request(admin_client, method, path)
        assert response.status_code != 401, (method, path)


def test_admin_stats_contains_no_provider_credentials(admin_client, monkeypatch):
    monkeypatch.setattr(
        admin,
        "PROVIDERS",
        {
            "fake": {
                "headers": {"Authorization": "synthetic-header"},
                "base_url": "https://synthetic.invalid/api",
                "api_key": "synthetic-key",
                "models": [{"id": "safe-model", "url": "https://synthetic.invalid/model"}],
            }
        },
    )
    payload = admin_client.get("/api/admin/stats").get_json()
    assert payload["models"] == {"fake": ["safe-model"]}
    serialized = str(payload)
    for forbidden in ("synthetic-header", "synthetic.invalid", "synthetic-key", "Authorization"):
        assert forbidden not in serialized


def test_google_callback_rejects_unverified_id_token(client, monkeypatch):
    monkeypatch.setattr(admin, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(admin, "GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr(admin, "GOOGLE_REDIRECT_URI", "https://app.example.test/auth/google/callback")
    monkeypatch.setattr(admin, "_verify_google_id_token", lambda token, expected_nonce=None: None)
    import database
    import identity
    nonce = "test-nonce-value"
    assert database.create_oauth_challenge(
        "google_oauth_state", "test-state-value", identity.browser_id(), nonce,
        code_verifier="p" * 64,
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id_token": "opaque-test-token"}

    monkeypatch.setattr(admin.requests, "post", lambda *args, **kwargs: FakeResponse())
    response = client.get("/auth/google/callback?code=authorization-code&state=test-state-value")
    assert response.status_code == 401
    assert response.get_data(as_text=True) == "Недействительный ID токен"


def test_google_callback_exchanges_pkce_and_rotates_session(client, monkeypatch):
    monkeypatch.setattr(admin, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(admin, "GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr(admin, "GOOGLE_REDIRECT_URI", "https://app.example.test/auth/google/callback")
    import database
    import identity

    old_browser_id = identity.browser_id()
    state, nonce, verifier = "callback-state", "callback-nonce", "s" * 64
    assert database.create_oauth_challenge(
        "google_oauth_state", state, old_browser_id, nonce, code_verifier=verifier
    )
    exchanged = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id_token": "verified-test-id-token"}

    def fake_post(_url, data=None, **_kwargs):
        exchanged.update(data or {})
        return FakeResponse()

    def verify(token, expected_nonce=None):
        if token == "verified-test-id-token" and expected_nonce == nonce:
            return {
                "iss": "https://accounts.google.com",
                "email": "callback@example.test",
                "email_verified": True,
                "name": "Callback User",
            }
        return None

    monkeypatch.setattr(admin.requests, "post", fake_post)
    monkeypatch.setattr(admin, "_verify_google_id_token", verify)
    response = client.get(f"/auth/google/callback?code=auth-code&state={state}")

    assert response.status_code == 302 and response.location.endswith("/chat")
    assert exchanged["code_verifier"] == verifier
    assert exchanged["code"] == "auth-code"
    with client.session_transaction() as stored:
        assert stored["nova_user_email"] == "callback@example.test"
        assert stored["nova_sid"] != old_browser_id


def test_google_oauth_start_uses_state_nonce_and_pkce(client, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setattr(admin, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(admin, "GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr(admin, "GOOGLE_REDIRECT_URI", "https://app.example.test/auth/google/callback")
    response = client.get("/auth/google")
    assert response.status_code == 302
    params = parse_qs(urlparse(response.location).query)
    assert params["state"] and params["nonce"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"]


def test_google_callback_requires_state_and_binds_it_to_browser(client):
    assert client.get("/auth/google/callback?code=authorization-code").status_code == 400

    other = client.application.test_client()
    other.get("/api/csrf-token")
    response = other.get("/auth/google/callback?code=authorization-code&state=not-issued-here")
    assert response.status_code == 400


def test_google_one_tap_consumes_nonce_and_sets_verified_server_session(client, monkeypatch):
    monkeypatch.setattr(admin, "GOOGLE_CLIENT_ID", "test-client-id")
    issued = client.get("/api/auth/google/nonce")
    assert issued.status_code == 200
    nonce = issued.get_json()["nonce"]

    def verify(credential, expected_nonce=None):
        if credential == "valid-test-jwt" and expected_nonce == nonce:
            return {
                "iss": "https://accounts.google.com",
                "email": "verified@example.test",
                "email_verified": True,
                "name": "Verified User",
                "picture": "https://images.example.test/avatar.png",
            }
        return None

    monkeypatch.setattr(admin, "_verify_google_id_token", verify)
    response = client.post(
        "/api/auth/google/one-tap",
        json={"credential": "valid-test-jwt", "nonce": nonce},
    )
    assert response.status_code == 200
    assert response.get_json()["profile"]["email"] == "verified@example.test"
    session = client.get("/api/session/check").get_json()
    assert session["google_login"] is True
    assert session["email"] == "verified@example.test"

    replay = client.post(
        "/api/auth/google/one-tap",
        json={"credential": "valid-test-jwt", "nonce": nonce},
    )
    assert replay.status_code == 401


def test_google_token_verifier_rejects_nonce_mismatch(monkeypatch):
    import sys
    import types

    requests_module = types.ModuleType("google.auth.transport.requests")
    requests_module.Request = type("Request", (), {})
    id_token_module = types.ModuleType("google.oauth2.id_token")
    id_token_module.verify_oauth2_token = lambda *args: {
        "iss": "https://accounts.google.com",
        "email": "user@example.com",
        "email_verified": True,
        "nonce": "attacker-controlled-nonce",
    }
    for name, module in {
        "google": types.ModuleType("google"),
        "google.auth": types.ModuleType("google.auth"),
        "google.auth.transport": types.ModuleType("google.auth.transport"),
        "google.auth.transport.requests": requests_module,
        "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.id_token": id_token_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    assert admin._verify_google_id_token("opaque-test-token", expected_nonce="server-nonce") is None


def test_google_token_verifier_requires_verified_email_and_google_issuer(monkeypatch):
    class FakeIdToken:
        @staticmethod
        def verify_oauth2_token(*args):
            return {"iss": "https://accounts.google.com", "email": "user@example.com", "email_verified": False}

    class FakeGoogleRequests:
        class Request:
            pass

    import sys
    import types

    google = types.ModuleType("google")
    auth = types.ModuleType("google.auth")
    transport = types.ModuleType("google.auth.transport")
    requests_module = types.ModuleType("google.auth.transport.requests")
    requests_module.Request = FakeGoogleRequests.Request
    oauth2 = types.ModuleType("google.oauth2")
    id_token_module = types.ModuleType("google.oauth2.id_token")
    id_token_module.verify_oauth2_token = FakeIdToken.verify_oauth2_token
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.auth", auth)
    monkeypatch.setitem(sys.modules, "google.auth.transport", transport)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", requests_module)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2)
    monkeypatch.setitem(sys.modules, "google.oauth2.id_token", id_token_module)

    assert admin._verify_google_id_token("opaque-test-token") is None


def test_google_token_verifier_rejects_wrong_issuer(monkeypatch):
    import sys
    import types

    requests_module = types.ModuleType("google.auth.transport.requests")
    requests_module.Request = type("Request", (), {})
    id_token_module = types.ModuleType("google.oauth2.id_token")
    id_token_module.verify_oauth2_token = lambda *args: {
        "iss": "https://accounts.example.invalid",
        "email": "user@example.com",
        "email_verified": True,
    }
    for name, module in {
        "google": types.ModuleType("google"),
        "google.auth": types.ModuleType("google.auth"),
        "google.auth.transport": types.ModuleType("google.auth.transport"),
        "google.auth.transport.requests": requests_module,
        "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.id_token": id_token_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    assert admin._verify_google_id_token("opaque-test-token") is None
