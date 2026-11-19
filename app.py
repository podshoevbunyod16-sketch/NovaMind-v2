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
from flask import Flask, send_from_directory, redirect, jsonify, request

from config import ADMIN_SESSION_KEY, load_plugins, PORT
from database import init_db
from routes import ALL_BLUEPRINTS
import observability
import web_hardening

# ---------- Flask ----------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = ADMIN_SESSION_KEY
from datetime import timedelta as _timedelta  # noqa: E402
app.permanent_session_lifetime = _timedelta(days=90)  # короче: файлы Cookies хранятся на сервере
web_hardening.init_app(app)
observability.init_app(app)

# ---------- Регистрация Blueprint'ов ----------
for bp in ALL_BLUEPRINTS:
    app.register_blueprint(bp)


@app.get("/healthz")
def healthz():
    return observability.health_response()


@app.get("/readyz")
def readyz():
    return observability.ready_response()


@app.get("/metrics")
def metrics():
    return observability.metrics_response()

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


@app.before_request
def _endpoint_rate_limit():
    """Cheap per-IP caps for expensive endpoints; Redis backend can replace this."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        import security
        allowed, retry_after, _limit, _remaining, _scope = security.endpoint_limiter.check(request)
        if not allowed:
            response = jsonify({"error": "Слишком много запросов", "code": "rate_limited"})
            response.headers["Retry-After"] = str(retry_after)
            return response, 429
    return None


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
if __name__ == '__main__':
    port = PORT
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    logging.getLogger(__name__).info("Запуск NovaMind на порту %s, debug=%s", port, debug)
    if web_hardening.is_production():
        problems = web_hardening.validate_production_config()
        if problems:
            raise SystemExit("Некорректная production-конфигурация: "
                             + "; ".join(problems))
    else:
        # Never print the generated code: it is a secret, even for development.
        logging.getLogger(__name__).warning(
            "ADMIN_CODE не задан: используется одноразовый код. "
            "Задайте ADMIN_CODE в окружении для постоянного входа.")
    app.run(host="0.0.0.0", port=port, debug=debug)  # nosec B104
