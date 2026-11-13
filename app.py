"""
app.py — Точка входа NovaMind. Только Flask init + регистрация Blueprint'ов.
Вся логика разделена по модулям:
  config.py         — настройки, провайдеры, переменные
  groq_rotation.py  — ротация Groq API ключей
  ai_providers.py   — запросы к AI (Groq, Gemini, OpenAI-compatible)
  database.py       — SQLite история чатов
  routes/
    chat.py         — /send, /send_stream, /api/chats/*, /api/history/*
    commands.py     — /command
    settings.py     — /settings, /api/settings/*
    admin.py        — /admin/*, /api/admin/*
    media.py        — /upload_*, /api/media/*
    search.py       — /api/auto_search, /api/web_search_groq
    composio.py     — /composio, /api/composio/*
"""
import os
import logging
from flask import Flask, send_from_directory, redirect

from config import ADMIN_SESSION_KEY, ADMIN_CODE, ADMIN_CODE_IS_DEFAULT, load_plugins, PORT
from database import init_db
from routes import ALL_BLUEPRINTS

# ---------- Flask ----------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = ADMIN_SESSION_KEY
from datetime import timedelta as _timedelta  # noqa: E402
app.permanent_session_lifetime = _timedelta(days=365)   # id браузера - основа изоляции данных
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

# ---------- Регистрация Blueprint'ов ----------
for bp in ALL_BLUEPRINTS:
    app.register_blueprint(bp)

# ---------- Production-обвязка ----------
# Проверки окружения, доверенный прокси, security-заголовки, request id,
# здоровье /healthz и /readyz, /version и Prometheus /metrics.
import production  # noqa: E402 — после регистрации маршрутов
production.install(app)
try:
    production.enforce_startup()
except RuntimeError as startup_error:
    # При импорте (wsgi/gunicorn/тесты) пусть падает как раньше — это
    # осознанная защита от production-конфигурации с дырами.
    if __name__ != "__main__":
        raise
    # А из `python app.py` показываем баннер с проблемой, без traceback.
    from server import check as startup_check
    code = startup_check()
    if code == 0:
        print(startup_error, file=sys.stderr)
        code = 1
    raise SystemExit(code)

# ---------- Инициализация ----------
init_db()
load_plugins()

# Версии промптов: фиксируем текущие тексты в манифесте, чтобы каждое изменение
# агента было видно в трассе и могло быть откачено одной переменной окружения.
try:
    import prompt_registry
    prompt_registry.ensure()
except Exception:  # noqa: BLE001 — версионирование не должно мешать запуску
    logging.getLogger(__name__).warning("Не удалось обновить манифест промптов", exc_info=True)

# ---------- Планировщик (Б4) ----------
# В тестах и при SCHEDULER_ENABLED=0 поток не поднимается: он не должен
# влиять ни на прогон pytest, ни на отладку.
def _start_scheduler():
    import sys
    if os.getenv("SCHEDULER_ENABLED", "1") != "1":
        return False
    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        return False
    try:
        import scheduler
        return scheduler.start(app)
    except Exception:
        logging.getLogger(__name__).warning("Планировщик не запустился", exc_info=True)
        return False


# При запуске через ``python app.py`` планировщик поднимается в воркере
# сервера (см. server.py): поток, созданный до fork, в дочернем процессе
# не живёт. При импорте как модуля (gunicorn/wsgi/тесты) — стартуем сразу.
if __name__ != "__main__" and os.getenv("NOVA_DEFER_SCHEDULER") != "1":
    _start_scheduler()

# ---------- Идентичность браузера ----------
@app.before_request
def _ensure_browser_identity():
    """Закрепляем browser id на первом же запросе.

    Раньше ``nova_sid`` создавался лениво внутри конкретного запроса: соседние
    запросы одной и той же freshly-opened вкладки могли получить разные id, а
    значит разные личные рабочие папки (файл записан в одну, читается из другой).
    """
    import identity
    identity.browser_id()


# ---------- Статика ----------
@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

@app.route('/')
def index():
    return redirect('/chat')

# ---------- Внешний HTTP-агент для платформы Agent-Civilization ----------
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")

@app.route("/run", methods=["POST"])
def agent_run():
    from flask import request, jsonify
    if AGENT_TOKEN and request.headers.get("X-Agent-Token") != AGENT_TOKEN:
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    payload = body.get("input") or body
    text = str(payload.get("text", "")).strip()
    if not text:
        return jsonify({"output": {"error": "empty text"}}), 400
    from ai_providers import chat_completion
    answer, meta = chat_completion([{"role": "user", "content": text}], max_tokens=800)
    if answer is None:
        return jsonify({"output": {"error": str(meta)[:300]}}), 502
    return jsonify({"output": {"answer": answer, "source": "novamind-v2"}})

# ========== ЗАПУСК ==========
# Одна команда на всё: python app.py.
#   python app.py            — production-сервер (gunicorn через Python-API)
#   python app.py --dev      — Flask dev-сервер с отладкой
#   python app.py --check    — только проверка окружения
if __name__ == '__main__':
    logging.getLogger(__name__).info("Запуск NovaMind, порт по умолчанию %s", PORT)
    if ADMIN_CODE_IS_DEFAULT:
        logging.getLogger(__name__).warning(
            "ADMIN_CODE не задан — сгенерирован одноразовый код администратора: %s "
            "(задайте ADMIN_CODE в .env, чтобы код был постоянным)", ADMIN_CODE)
    from server import main as run_server
    raise SystemExit(run_server(app))
