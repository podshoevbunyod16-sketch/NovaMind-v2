"""Тесты production-обвязки: пробы, заголовки, метрики, строгая проверка."""
import production


def test_healthz_is_alive(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["status"] == "alive"
    assert data["version"]


def test_readyz_checks_dependencies(client):
    response = client.get("/readyz")
    assert response.status_code in {200, 503}
    data = response.get_json()
    assert "checks" in data
    assert "database" in data["checks"]
    assert "storage" in data["checks"]
    assert "receipts" in data["checks"]
    assert data["checks"]["database"]["ok"] is True


def test_version_reports_features(client):
    data = client.get("/version").get_json()
    assert data["name"] == "NovaMind"
    assert data["version"]
    assert "receipts" in data["features"]
    assert "council" in data["features"]
    assert isinstance(data["checks"], list)


def test_security_headers_present(client):
    response = client.get("/healthz")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["X-Request-Id"]
    assert response.headers["Permissions-Policy"]


def test_request_id_is_echoed(client):
    response = client.get("/healthz", headers={"X-Request-Id": "test-request-42"})
    assert response.headers["X-Request-Id"] == "test-request-42"


def test_metrics_are_prometheus_text(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.mimetype.startswith("text/plain")
    body = response.get_data(as_text=True)
    assert "novamind_info" in body
    assert "novamind_requests_total" in body
    assert "novamind_ready" in body


def test_metrics_token_protects_endpoint(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "secret-metrics")
    assert client.get("/metrics").status_code == 403
    allowed = client.get("/metrics", headers={"X-Metrics-Token": "secret-metrics"})
    assert allowed.status_code == 200
    assert client.get("/metrics?token=secret-metrics").status_code == 200


def test_api_errors_return_json(client):
    response = client.get("/api/definitely-missing")
    assert response.status_code == 404
    data = response.get_json()
    assert data["ok"] is False
    assert data["status"] == 404
    assert data["request_id"]


def test_html_errors_return_page(client):
    response = client.get("/definitely-missing")
    assert response.status_code == 404
    assert "text/html" in response.mimetype
    assert "NovaMind" in response.get_data(as_text=True)


def test_validate_flags_missing_admin_code(monkeypatch):
    monkeypatch.delenv("ADMIN_CODE", raising=False)
    monkeypatch.setenv("NOVA_ENV", "production")
    keys = {item["key"] for item in production.validate()}
    assert "admin_code" in keys


def test_production_startup_refuses_insecure_defaults(monkeypatch):
    monkeypatch.delenv("ADMIN_CODE", raising=False)
    monkeypatch.setenv("NOVA_ENV", "production")
    assert production.is_production() is True
    try:
        production.enforce_startup()
    except RuntimeError as error:
        assert "production" in str(error).lower() or "критичес" in str(error).lower()
    else:  # pragma: no cover
        raise AssertionError("production должен остановиться без ADMIN_CODE")


def test_development_startup_does_not_raise(monkeypatch):
    monkeypatch.delenv("ADMIN_CODE", raising=False)
    monkeypatch.setenv("NOVA_ENV", "development")
    assert isinstance(production.enforce_startup(), list)


def test_install_is_idempotent(client):
    from app import app
    assert app.config.get("NOVA_PRODUCTION_INSTALLED") is True
    production.install(app)
    assert app.config.get("NOVA_PRODUCTION_INSTALLED") is True


def test_report_contains_environment(client):
    report = production.report()
    assert report["python"]
    assert report["env"]
    assert report["db_path"]
    assert report["ok"] in {True, False}
