"""Тесты Python-деплоя: единая точка входа, деплой-план, systemd-шаблоны.

Docker в проекте не обязателен, поэтому production-путь должен проверяться
без него: `python app.py` (логика в `server.py`), `scripts/deploy.py`,
`scripts/install_service.py`. `scripts/serve.py` остаётся тонкой обёрткой.
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, relative):
    path = os.path.join(ROOT, relative)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Модуль нужно зарегистрировать до exec: dataclass с отложенными
    # аннотациями ищет себя в sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def serve():
    return _load("nova_serve", "scripts/serve.py")


@pytest.fixture(scope="module")
def server():
    return _load("nova_server", "server.py")


@pytest.fixture(scope="module")
def deploy():
    return _load("nova_deploy", "scripts/deploy.py")


@pytest.fixture(scope="module")
def service():
    return _load("nova_service", "scripts/install_service.py")


@pytest.fixture(scope="module")
def gunicorn_conf():
    return _load("nova_gunicorn_conf", "gunicorn.conf.py")


# ── server.py / app.py — единая точка входа ─────────────────────────────
def test_app_py_delegates_to_server():
    text = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert "from server import main as run_server" in text
    assert "run_server(app)" in text
    # сервер не должен стартовать при импорте: только под __main__
    tail = text[text.index('if __name__ == \'__main__\''):]
    assert "app.run(" not in tail


def test_runtime_mode_selects_flask_for_dev(server, monkeypatch):
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    assert server.runtime_mode() == "gunicorn"
    assert server.runtime_mode(dev=True) == "flask"
    assert server.runtime_mode(flask=True) == "flask"
    assert server.runtime_mode(debug_env="true") == "flask"
    assert server.runtime_mode(debug_env="0") == "gunicorn"


def _flask_app():
    import app as flask_app
    return flask_app.app


def test_server_check_passes_in_development(server, monkeypatch):
    monkeypatch.setenv("NOVA_ENV", "development")
    assert server.main(_flask_app(), ["--check"]) == 0


def test_server_check_fails_without_admin_code_in_production(server, monkeypatch):
    monkeypatch.delenv("ADMIN_CODE", raising=False)
    monkeypatch.setenv("NOVA_ENV", "production")
    assert server.main(_flask_app(), ["--check"]) == 1


def test_scheduler_reset_after_fork():
    import scheduler
    scheduler._thread = None
    scheduler._stop_event.set()
    scheduler.reset_after_fork()
    assert scheduler._thread is None
    assert not scheduler._stop_event.is_set()


def test_gunicorn_post_fork_restarts_scheduler():
    text = open(os.path.join(ROOT, "gunicorn.conf.py"), encoding="utf-8").read()
    assert "reset_after_fork()" in text
    assert "NOVA_DEFER_SCHEDULER" in text


# ── scripts/serve.py (совместимость) ────────────────────────────────────
def test_build_options_defaults(serve):
    options = serve.build_options(port=8080, workers=2, threads=4)
    assert options["bind"].endswith(":8080")
    assert options["workers"] == 2
    assert options["threads"] == 4
    assert options["worker_class"] == "gthread"
    assert options["timeout"] >= 60
    assert options["preload_app"] is False


def test_build_options_reads_env(serve, monkeypatch):
    monkeypatch.setenv("BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.setenv("WEB_THREADS", "3")
    monkeypatch.setenv("WEB_PRELOAD", "1")
    options = serve.build_options()
    assert options["bind"] == "127.0.0.1:9090"
    assert options["threads"] == 3
    assert options["preload_app"] is True


def test_banner_shows_build_and_env(serve):
    text = serve.banner({
        "name": "NovaMind", "version": "3.0.0", "codename": "Khirad",
        "build": "test123", "env": "production", "production": True,
        "python": "3.11.9", "pid": 42, "db_path": "/data/nova.db",
        "features": ["receipts", "council"], "checks": [],
    })
    assert "3.0.0" in text
    assert "production" in text
    assert "receipts" in text
    assert "/data/nova.db" in text


def test_serve_check_passes_in_development(serve, monkeypatch):
    monkeypatch.setenv("NOVA_ENV", "development")
    assert serve.main(["--check"]) == 0


def test_serve_check_fails_without_admin_code_in_production(serve, monkeypatch):
    monkeypatch.delenv("ADMIN_CODE", raising=False)
    monkeypatch.setenv("NOVA_ENV", "production")
    assert serve.main(["--check"]) == 1


# ── scripts/deploy.py ───────────────────────────────────────────────────
def test_venv_python_path(deploy, tmp_path):
    path = deploy.venv_python(str(tmp_path))
    assert path.startswith(str(tmp_path))
    assert path.endswith("python") or path.endswith("python.exe")


def test_deploy_plan_is_ordered(deploy, tmp_path):
    steps = deploy.build_steps(
        venv=str(tmp_path / ".venv"), service="novamind",
        requirements=os.path.join(ROOT, "requirements.txt"),
        backup=True, restart=True)
    names = [step.name for step in steps]
    assert names[0] in {"Создать venv", "Обновить pip"}
    assert any("preflight" in " ".join(step.command or []) for step in steps)
    assert any("backup.py" in " ".join(step.command or []) for step in steps)
    assert any(step.kind == "systemd" for step in steps)
    assert steps[-1].kind == "health"
    assert steps[-1].extra["url"].startswith("http")


def test_deploy_plan_skips_optional_steps(deploy, tmp_path):
    steps = deploy.build_steps(
        venv=str(tmp_path / ".venv"), service="nova",
        requirements=os.path.join(ROOT, "requirements.txt"),
        backup=False, restart=False)
    assert not any(step.kind == "systemd" for step in steps)
    assert not any("backup.py" in " ".join(step.command or []) for step in steps)
    assert [step.kind for step in steps][-1] == "health"


def test_health_check_reports_failure_quickly(deploy):
    assert deploy.wait_health("http://127.0.0.1:9", retries=1, delay=0) is False


def test_deploy_dry_run_succeeds(deploy, tmp_path, capsys):
    code = deploy.run(
        venv=str(tmp_path / ".venv"), service="novamind",
        requirements=os.path.join(ROOT, "requirements.txt"),
        dry_run=True)
    assert code == 0
    output = capsys.readouterr().out
    assert "deploy" not in output.lower() or "NovaMind" in output
    assert "pip install" in output


# ── scripts/install_service.py + deploy/ ────────────────────────────────
def test_render_unit_replaces_every_marker(service, tmp_path):
    unit = service.render_unit(
        app_dir=str(tmp_path), user="novamind", group="novamind",
        port=8080, python=str(tmp_path / ".venv" / "bin" / "python"))
    assert "__APP_DIR__" not in unit
    assert "__PORT__" not in unit
    assert "app.py" in unit
    assert "scripts/serve.py" not in unit
    assert "--port 8080" in unit
    assert "User=novamind" in unit
    assert "Environment=NOVA_ENV=production" in unit
    assert "Restart=always" in unit
    assert str(tmp_path) in unit


def test_deploy_templates_exist():
    for name in ("novamind.service", "nginx.conf", "logrotate.conf"):
        path = os.path.join(ROOT, "deploy", name)
        assert os.path.exists(path), name
    nginx = open(os.path.join(ROOT, "deploy", "nginx.conf"), encoding="utf-8").read()
    assert "proxy_buffering off" in nginx
    assert "X-Forwarded-Proto" in nginx


def test_gunicorn_config_is_sane(gunicorn_conf):
    assert gunicorn_conf.worker_class in {"gthread", "sync", "gevent"}
    assert gunicorn_conf.timeout >= 60
    assert gunicorn_conf.graceful_timeout > 0
    # Приложение запускает фоновые потоки: предзагрузка по умолчанию выключена.
    assert gunicorn_conf.preload_app is False
    assert gunicorn_conf.bind.endswith(str(gunicorn_conf._int("PORT", 5000))) or ":" in gunicorn_conf.bind
