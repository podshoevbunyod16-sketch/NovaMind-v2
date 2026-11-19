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
import hmac
import secrets
from urllib.parse import urlsplit
from flask import Flask, send_from_directory, redirect, request, session, jsonify

from config import (
    ADMIN_SESSION_KEY,
    ADMIN_CODE_IS_DEFAULT,
    COOKIE_SECURE,
    HSTS_ENABLED,
    SITE_URL,
    load_plugins,
    PORT,
)
from database import init_db
from routes import ALL_BLUEPRINTS

# ---------- Flask ----------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = ADMIN_SESSION_KEY
from datetime import timedelta as _timedelta  # noqa: E402
SESSION_LIFETIME_DAYS = max(1, min(int(os.getenv("SESSION_LIFETIME_DAYS", "30")), 90))
app.permanent_session_lifetime = _timedelta(days=SESSION_LIFETIME_DAYS)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
)

# ---------- Регистрация Blueprint'ов ----------
for bp in ALL_BLUEPRINTS:
    app.register_blueprint(bp)

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
def _csrf_protection():
    """Require a session-bound token for every cookie-authenticated mutation."""
    token = session.get("_csrf_token")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token

    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return None
    # These integration routes authenticate exclusively with explicit bearer-like
    # secrets and do not rely on the browser session cookie.
    if request.path in {"/run", "/api/platform/run"}:
        return None

    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return jsonify({"error": "CSRF validation failed"}), 403
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlsplit(origin)
        expected = urlsplit(SITE_URL) if COOKIE_SECURE else urlsplit(request.host_url)
        if (parsed.scheme.lower() != expected.scheme.lower()
                or parsed.netloc.lower() != expected.netloc.lower()
                or parsed.path or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            return jsonify({"error": "CSRF validation failed"}), 403

    supplied = request.headers.get("X-CSRF-Token", "")
    cookie_token = request.cookies.get("nova_csrf", "")
    if (not supplied or not cookie_token
            or not hmac.compare_digest(token, supplied)
            or not hmac.compare_digest(token, cookie_token)):
        return jsonify({"error": "CSRF validation failed"}), 403
    return None


@app.after_request
def _set_security_headers(response):
    """Apply baseline browser protections in one place for every response."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'self'; frame-ancestors 'self'; "
        "form-action 'self' https:; script-src 'self' 'unsafe-inline' https:; "
        "style-src 'self' 'unsafe-inline' https:; img-src 'self' data: blob: https:; "
        "font-src 'self' data: https:; media-src 'self' data: blob: https:; "
        "connect-src 'self' https: ws: wss:",
    )
    if HSTS_ENABLED:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    csrf_token = session.get("_csrf_token")
    if not isinstance(csrf_token, str) or len(csrf_token) < 32:
        csrf_token = secrets.token_urlsafe(32)
        session["_csrf_token"] = csrf_token
    response.set_cookie(
        "nova_csrf", csrf_token, httponly=False, secure=COOKIE_SECURE,
        samesite="Strict", path="/",
        max_age=int(app.permanent_session_lifetime.total_seconds()) if session.permanent else None,
    )
    return response


# ---------- Статика ----------
@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


@app.route("/api/csrf-token")
def csrf_token():
    response = jsonify({"csrf_token": session.get("_csrf_token", "")})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/')
def index():
    return redirect('/chat')

# ---------- Внешний HTTP-агент для платформы Agent-Civilization ----------
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "").strip()

@app.route("/run", methods=["POST"])
def agent_run():
    from flask import request, jsonify
    if not AGENT_TOKEN:
        return jsonify({"error": "agent endpoint is not configured"}), 503
    supplied_token = request.headers.get("X-Agent-Token", "")
    if not supplied_token or not hmac.compare_digest(supplied_token, AGENT_TOKEN):
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
    if ADMIN_CODE_IS_DEFAULT:
        logging.getLogger(__name__).warning("ADMIN_CODE не задан")
    else:
        logging.getLogger(__name__).info("ADMIN_CODE задан")
    app.run(host="0.0.0.0", port=port, debug=debug)
