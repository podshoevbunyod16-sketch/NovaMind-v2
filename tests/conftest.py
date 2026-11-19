"""Общие фикстуры: изолированное окружение без API-ключей и тестовый клиент Flask."""
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault(
    "NOVAMIND_DB_PATH",
    os.path.join(tempfile.gettempdir(), f"novamind-test-bootstrap-{os.getpid()}.db"),
)

# `config` читает `.env` при импорте, поэтому provider credentials и endpoint
# overrides должны быть обезврежены до его импорта и до application modules.
KEY_VARS = (
    "GEMINI_API_KEY", "GOOGLE_AI_STUDIO_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
    "GROQ_API_KEY_1", "GROQ_API_KEY_2", "CEREBRAS_API_KEY", "OPENAI_API_KEY",
    "OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_COMPATIBLE_URL", "LOCAL_LLM_URL",
    "OPENAI_COMPATIBLE_KEY", "LOCAL_LLM_API_KEY",
    "IMAGE_GENERATION_URL", "IMAGE_EDIT_URL", "IMAGE_MODEL", "IMAGE_API_KEY",
    "AUDIO_TTS_URL", "AUDIO_TTS_MODEL", "AUDIO_API_KEY",
    "VIDEO_API_URL", "VIDEO_MODEL", "VIDEO_API_KEY", "POLLINATIONS_TOKEN",
    "POLLINATIONS_API_KEY", "POLLINATIONS_KEY", "MEDIA_ALLOW_PAID",
)
for _provider_var in KEY_VARS:
    # Пустое значение не даёт config.py восстановить ключ через setdefault из .env.
    os.environ[_provider_var] = ""

from flask.testing import FlaskClient
from werkzeug.datastructures import Headers

import config  # Delayed until after test environment initialization.


class CsrfTestClient(FlaskClient):
    """Make ordinary tests use the real double-submit CSRF contract."""

    def open(self, *args, **kwargs):
        method = str(kwargs.get("method", "GET")).upper()
        headers = Headers(kwargs.get("headers"))
        if method not in {"GET", "HEAD", "OPTIONS", "TRACE"} and "X-CSRF-Token" not in headers:
            cookie = self.get_cookie("nova_csrf")
            if cookie is None:
                super().open("/api/csrf-token", method="GET")
                cookie = self.get_cookie("nova_csrf")
            if cookie is not None:
                headers["X-CSRF-Token"] = cookie.value
            kwargs["headers"] = headers
        return super().open(*args, **kwargs)

#: Заводские настройки озвучки: снимок до того, как тесты начали их менять.
SPEECH_DEFAULTS = dict(config.speech_settings)


def reset_speech_settings():
    """Возвращает настройки озвучки к заводским (глобальный объект config)."""
    config.speech_settings.clear()
    config.speech_settings.update(SPEECH_DEFAULTS)

@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Каждый тест: чистые ключи, своя папка медиа, свои runtime-настройки."""
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    # config/.env must not decide whether a test silently waits for approval.
    monkeypatch.setenv("AGENT_CONFIRM", "danger")
    # No test gets the host fallback merely by importing the application.
    monkeypatch.delenv("TERMINAL_HOST_EXECUTION", raising=False)
    # Подтверждение опасных действий в тестах не ждёт 120 секунд:
    # тест, которому нужен таймаут, выставляет своё значение сам.
    monkeypatch.setenv("AGENT_CONFIRM_TIMEOUT", "0.05")
    import agent.policy as agent_policy
    agent_policy.reset_cache()
    agent_policy.reset_confirmations()
    import mcp_registry
    mcp_registry._TOOL_CACHE.clear()
    mcp_registry._FAILURE_UNTIL.clear()
    mcp_registry._close_stdio_sessions()
    import media_generation as mg
    media_dir = tmp_path / "generated_media"
    monkeypatch.setattr(mg, "MEDIA_DIR", str(media_dir))
    mg.reset_catalog_cache()
    import media_agent
    media_agent.reset_cache()

    import config
    monkeypatch.setattr(config, "RUNTIME_SETTINGS_FILE", str(tmp_path / "runtime_settings.json"))
    # Локальный движок — глобальное состояние: без сброса один тест оставлял
    # адрес Ollama следующему (и каталог моделей искался не там).
    monkeypatch.setattr(config, "local_engine",
                        {"preset": "auto", "base_url": "", "api_key": ""})
    # Язык интерфейса — тоже глобальное состояние; ответы моделей зависят от него.
    monkeypatch.setattr(config, "ui_language", "tg")
    config.apply_local_engine(config.local_engine)
    import local_providers
    local_providers._CATALOG_CACHE.clear()
    local_providers._SHOW_CACHE.clear()
    config.save_selected_media_model({})
    # Настройки озвучки живут в глобальном объекте config: без сброса один тест
    # «выбирал» модель для следующего (и тот падал на чужом провайдере).
    reset_speech_settings()

    import groq_rotation
    monkeypatch.setattr(groq_rotation, "GROQ_KEYS", [], raising=False)

    # Счётчики частоты живут в памяти процесса — каждый тест начинает с нуля.
    import security
    security.login_limiter.clear()
    security.agent_limiter.clear()
    security.tts_limiter.clear()

    # Трассы и база знаний: у каждого теста свои файлы. Иначе «последние
    # трассы» и документы предыдущего теста попадали в следующий, и тесты
    # начинали зависеть от порядка запуска.
    import telemetry
    monkeypatch.setattr(telemetry.TRACER, "directory", str(tmp_path / "traces"))
    telemetry.TRACER._recent.clear()

    import knowledge
    monkeypatch.setattr(knowledge, "DB_PATH", str(tmp_path / "knowledge.db"))
    monkeypatch.setattr(knowledge, "_DEFAULT", None)

    yield

    security.login_limiter.clear()
    security.agent_limiter.clear()
    security.tts_limiter.clear()

    config.save_selected_media_model({})
    reset_speech_settings()
    agent_policy.reset_confirmations()
    agent_policy.reset_cache()
    mcp_registry._TOOL_CACHE.clear()
    mcp_registry._FAILURE_UNTIL.clear()
    mcp_registry._close_stdio_sessions()
    mg.reset_catalog_cache()
    mg.reset_tts_catalog_cache()


@pytest.fixture
def gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")


@pytest.fixture
def client(tmp_path, monkeypatch, isolated_env):
    """Flask test client с отдельной базой данных (репозиторий не трогает)."""
    import database
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test_chats.db"))
    # Lazy schema caches outlive a temporary file if pytest reuses its path.
    database._runs_ready.discard(database.DB_PATH)
    database.init_db()

    import routes.media as media_routes
    monkeypatch.setattr(media_routes, "MEDIA_DIR", str(tmp_path / "generated_media"))

    from app import app as flask_app
    flask_app.config.update(TESTING=True)
    flask_app.test_client_class = CsrfTestClient
    with flask_app.test_client() as test_client:
        test_client.get("/api/csrf-token")
        yield test_client


class FakeResponse:
    """Мини-замена requests.Response для тестов без сети."""

    def __init__(self, status_code=200, json_data=None, content=b"", headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.headers = headers or {}
        self.text = text or ("" if json_data is None else str(json_data))

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


# ══════════════ «маленький Linux»: песочница и вход администратора ══════════════
@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Temporary test workspace; explicit host-command opt-in, not OS isolation."""
    import os

    import routes.agent as agent_routes
    import routes.terminal as term_routes

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(term_routes, "WORKSPACE", os.path.realpath(str(workspace)))
    # Песочница в продукте много-пользовательская (_ws добавляет users/<hash>),
    # но тесты смотрят в одну временную папку. Фиксируем её явно, иначе
    # google_client читает/пишет не ту же папку, что проверяет тест.
    monkeypatch.setattr(term_routes, "_ws", lambda: os.path.realpath(str(workspace)))
    monkeypatch.setattr(agent_routes, "run_terminal", term_routes.run_agent)
    # First-party registry adapters resolve these module functions at call time.
    import routes.search as search_routes
    monkeypatch.setattr(search_routes, "search_web",
                        lambda query, num=8: agent_routes.nova_search(query, limit=num))
    monkeypatch.setenv("TERMINAL_ENABLED", "1")
    monkeypatch.setenv("AGENT_ENABLED", "1")
    # Only tests requesting this fixture may run their hard-coded allowlisted
    # integration commands. Production remains fail-closed by default.
    monkeypatch.setenv("TERMINAL_HOST_EXECUTION", "1")
    return workspace


@pytest.fixture
def admin_client(client, sandbox):
    """Администратор в уже включённой песочнице."""
    with client.session_transaction() as session:
        session["admin_logged_in"] = True
        session["admin_username"] = "admin"
    return client


@pytest.fixture
def google_client(client, sandbox):
    """Обычный пользователь: вошёл через Google — песочница ему доступна."""
    with client.session_transaction() as session:
        session["nova_google_login"] = True
        session["nova_user_nick"] = "Иван"
        session["nova_user_email"] = "ivan@example.com"
    return client


@pytest.fixture
def nick_client(client, sandbox):
    """Вход по нику: серверная сессия ставится через /api/session/login."""
    response = client.post("/api/session/login", json={"nick": "Мария"})
    assert response.status_code == 200
    return client
