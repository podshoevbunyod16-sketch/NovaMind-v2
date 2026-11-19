"""
routes/admin.py — Административные маршруты
"""
import base64
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import subprocess
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import Blueprint, jsonify, redirect, render_template, request, session

import config
import groq_rotation
from config import (
    ADMIN_CREDENTIALS,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI,
    PROVIDERS,
)
from groq_rotation import GROQ_KEY_COOLDOWN, GROQ_KEYS, get_groq_key_status

log = logging.getLogger(__name__)

_LOOPBACK_ADDRS = {"127.0.0.1", "::1"}
_PROXY_HEADERS = (
    "X-Forwarded-For",
    "X-Real-IP",
    "CF-Connecting-IP",
    "Forwarded",
    "True-Client-IP",
)


def _admin_nicks() -> set[str]:
    """Ники из ``ADMIN_NICKS`` в .env (через запятую, без учёта регистра)."""
    raw = os.getenv("ADMIN_NICKS", "")
    return {
        item.strip().strip("\"'").lower()
        for item in raw.split(",")
        if item.strip().strip("\"'")
    }


def _is_local_loopback(req) -> bool:
    """Проверяет, что запрос пришёл напрямую с 127.0.0.1 или ::1 без прокси/туннеля."""
    remote = (req.remote_addr or "").strip()
    if remote not in _LOOPBACK_ADDRS:
        return False
    # ВНИМАНИЕ: туннели (ngrok, cloudflared) и reverse-proxy подключаются к Flask
    # с 127.0.0.1, но добавляют заголовки пересылки. При наличии таких заголовков
    # локальный вход админа по нику запрещаем.
    for header in _PROXY_HEADERS:
        if (req.headers.get(header) or "").strip():
            return False
    return True

# Папка с сохранённым кодом лежит в корне проекта (а не в routes/) — как и generated_*
SAVED_CODES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "saved_codes")

admin_bp = Blueprint("admin", __name__)


def _admin_required(view):
    """Require the server-side admin session for data-changing/admin data APIs."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return jsonify({"error": "Требуется вход администратора"}), 401
        return view(*args, **kwargs)
    return wrapped


def _verify_google_id_token(token, expected_nonce=None):
    """Verify Google's ID token, or fail closed when google-auth is unavailable."""
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        claims = id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except Exception:  # noqa: BLE001 - verifier failures must reject the token
        log.warning("Google ID-token verification failed")
        return None

    if not isinstance(claims, dict):
        log.warning("Google ID-token claims rejected")
        return None
    issuer = claims.get("iss")
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        log.warning("Google ID-token issuer rejected")
        return None
    if expected_nonce is not None:
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, expected_nonce):
            log.warning("Google ID-token nonce rejected")
            return None
    if not claims.get("email") or claims.get("email_verified") is not True:
        log.warning("Google ID-token email is not verified")
        return None
    return claims


def _safe_provider_models():
    """Expose model identifiers only; never serialize provider configuration."""
    safe = {}
    for provider, details in PROVIDERS.items():
        if not isinstance(details, dict):
            safe[str(provider)] = []
            continue
        models = details.get("models", [])
        safe[str(provider)] = [
            str(model.get("id")) for model in models
            if isinstance(model, dict) and model.get("id")
        ]
    return safe

@admin_bp.route('/admin/login')
def admin_login_page():
    return render_template('admin.html')

# ========== GOOGLE OAUTH ==========

def _establish_google_session(user_info):
    nick = str(user_info.get("name") or user_info.get("email", "User").split("@")[0])[:64]
    email = str(user_info.get("email") or "").strip().lower()
    picture = str(user_info.get("picture") or "")[:2048]
    session.clear()
    session.permanent = True
    import identity
    identity.browser_id()
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session["nova_user_nick"] = nick
    session["nova_user_email"] = email
    session["nova_user_avatar"] = picture
    session["nova_google_login"] = True
    session["nova_is_admin"] = False
    return {"nick": nick, "email": email, "avatar": picture}


def _rotate_admin_session(username):
    session.clear()
    session.permanent = True
    import identity
    identity.browser_id()
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session["admin_logged_in"] = True
    session["admin_username"] = username


@admin_bp.route("/auth/google")
def auth_google_start():
    """Start a state-, nonce- and PKCE-bound Google authorization-code flow."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        return "Google OAuth is not configured", 503
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    code_challenge = code_challenge.rstrip(b"=").decode("ascii")
    import database
    import identity
    try:
        stored = database.create_oauth_challenge(
            "google_oauth_state", state, identity.browser_id(), nonce, ttl=300,
            code_verifier=verifier,
        )
    except sqlite3.Error:
        log.exception("Google OAuth challenge persistence failed")
        return "OAuth is temporarily unavailable", 503
    if not stored:
        return "OAuth is temporarily unavailable", 503
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@admin_bp.route("/api/auth/google/nonce")
def google_one_tap_nonce():
    """Issue a one-use Google Identity Services nonce for this browser session."""
    if not GOOGLE_CLIENT_ID:
        return jsonify({"error": "Google sign-in is not configured"}), 503
    nonce = secrets.token_urlsafe(32)
    import database
    import identity
    try:
        stored = database.create_oauth_challenge(
            "google_gsi_nonce", nonce, identity.browser_id(), nonce, ttl=300,
        )
    except sqlite3.Error:
        log.exception("Google One Tap nonce persistence failed")
        return jsonify({"error": "Google sign-in is temporarily unavailable"}), 503
    if not stored:
        return jsonify({"error": "Google sign-in is temporarily unavailable"}), 503
    response = jsonify({"nonce": nonce})
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_bp.route("/api/auth/google/one-tap", methods=["POST"])
def google_one_tap_login():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid Google credential"}), 400
    credential = str(data.get("credential") or "")
    nonce = str(data.get("nonce") or "")
    if not credential or len(credential) > 20000 or not nonce or len(nonce) > 200:
        return jsonify({"error": "Invalid Google credential"}), 400
    import database
    import identity
    try:
        challenge = database.consume_oauth_challenge(
            "google_gsi_nonce", nonce, identity.browser_id(),
        )
    except sqlite3.Error:
        log.exception("Google One Tap nonce verification failed")
        return jsonify({"error": "Google sign-in is temporarily unavailable"}), 503
    if not challenge or not hmac.compare_digest(challenge["nonce"], nonce):
        return jsonify({"error": "Google sign-in challenge expired; please try again"}), 401
    user_info = _verify_google_id_token(credential, expected_nonce=nonce)
    if not user_info:
        return jsonify({"error": "Invalid Google credential"}), 401
    profile = _establish_google_session(user_info)
    return jsonify({"success": True, "profile": profile})


@admin_bp.route('/auth/google/callback')
def auth_google_callback():
    state = request.args.get("state", "")
    if not state or len(state) > 200:
        return "Недействительный запрос авторизации", 400
    import database
    import identity
    try:
        challenge = database.consume_oauth_challenge(
            "google_oauth_state", state, identity.browser_id(),
        )
    except sqlite3.Error:
        log.exception("Google OAuth state verification failed")
        return "Ошибка авторизации", 503
    if not challenge:
        return "Недействительный или истёкший запрос авторизации", 400
    nonce = challenge.get("nonce")
    code_verifier = challenge.get("code_verifier")
    if (not isinstance(nonce, str) or not nonce
            or not isinstance(code_verifier, str) or not 43 <= len(code_verifier) <= 128):
        return "Недействительный запрос авторизации", 400

    code = request.args.get('code')
    error = request.args.get('error')

    if error:
        return 'Авторизация Google отменена', 400
    if not code or len(code) > 4096:
        return 'Не получен код авторизации', 400
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        return "Google OAuth is not configured", 503

    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }

    try:
        token_resp = requests.post(token_url, data=token_data, timeout=10)
        token_resp.raise_for_status()
        token_json = token_resp.json()
        if not isinstance(token_json, dict):
            return 'Недействительный ответ Google', 502

        id_token_jwt = token_json.get("id_token")
        if not id_token_jwt or not isinstance(id_token_jwt, str) or len(id_token_jwt) > 20000:
            return 'Не получен ID токен', 400

        user_info = _verify_google_id_token(id_token_jwt, expected_nonce=nonce)
        if not user_info:
            return 'Недействительный ID токен', 401

        _establish_google_session(user_info)
        return redirect('/chat')
    except Exception:  # noqa: BLE001 - convert unexpected provider failures to a safe response
        log.warning("Google OAuth callback failed")
        return 'Ошибка авторизации', 500

# ========== API АВТОРИЗАЦИИ ==========

@admin_bp.route('/api/admin/login', methods=['POST'])
def admin_login():
    """Вход в админку с защитой от перебора.

    5 неудачных попыток за 10 минут — блок отдельно по IP и по аккаунту
    (настраивается ADMIN_LOGIN_MAX_ATTEMPTS / ADMIN_LOGIN_WINDOW).
    """
    import security

    data = request.get_json() or {}
    username = (data.get('username') or 'admin').strip()
    code = data.get('code', '').strip()
    ip_key = 'login_ip:' + security.client_ip(request)
    user_key = 'login_user:' + username

    allowed, retry_after, _blocked = security.login_limiter.blocked(ip_key, user_key)
    if not allowed:
        minutes = max(1, retry_after // 60 + (1 if retry_after % 60 else 0))
        response = jsonify({
            'success': False,
            'error': f'Слишком много попыток входа. Повторите через {minutes} мин.',
            'retry_after': retry_after,
        })
        response.headers['Retry-After'] = str(retry_after)
        return response, 429

    if username not in ADMIN_CREDENTIALS or ADMIN_CREDENTIALS[username] != code:
        security.login_limiter.fail(ip_key, user_key)
        error = 'Неверный логин' if username not in ADMIN_CREDENTIALS else 'Неверный код'
        return jsonify({'success': False, 'error': error})

    security.login_limiter.succeed(ip_key, user_key)
    _rotate_admin_session(username)
    return jsonify({'success': True, 'username': username})

@admin_bp.route('/api/session/login', methods=['POST'])
def session_login():
    """Серверный вход по нику.

    Google кладёт флаги в сессию сам (см. выше), а быстрый вход по нику живёт
    только в localStorage — без этого шага сервер не знает, что человек вошёл,
    и не пускает его в рабочую папку и агентный режим.

    Если ник входит в ``ADMIN_NICKS`` из ``.env`` и запрос пришёл строго локально
    (``127.0.0.1`` или ``::1``, без заголовков прокси/туннеля), выставляется
    ``admin_logged_in = True``.
    ВНИМАНИЕ: не используйте ``ADMIN_NICKS``, если сервер опубликован наружу
    через туннель (ngrok, cloudflared, ssh -R) или прокси, скрывающий внешний IP.
    """
    data = request.get_json(silent=True) or {}
    nick = (data.get('nick') or data.get('username') or '').strip()[:64]
    if not nick:
        return jsonify({'success': False, 'error': 'Пустой ник'}), 400
    session['nova_user_nick'] = nick
    session['nova_signed_in'] = True

    allowed_nicks = _admin_nicks()
    if nick.lower() in allowed_nicks:
        if _is_local_loopback(request):
            session['admin_logged_in'] = True
            session['admin_username'] = nick
            log.info("Локальный вход администратора по нику '%s' с %s", nick, request.remote_addr)
        else:
            log.warning(
                "Отклонён вход в админку по нику '%s': нелокальный запрос или обнаружен прокси/туннель (remote_addr=%s)",
                nick, request.remote_addr,
            )

    return jsonify({
        'success': True,
        'nick': nick,
        'admin': bool(session.get('admin_logged_in')),
    })


@admin_bp.route('/api/session/check')
def session_check():
    """Кто вошёл — интерфейсу нужно знать, открывать ли рабочую папку."""
    google_login = bool(session.get("nova_google_login"))
    return jsonify({
        'signed_in': bool(session.get('admin_logged_in') or session.get('nova_google_login')
                          or session.get('nova_signed_in') or session.get('nova_user_nick')
                          or session.get('username')),
        'nick': session.get('admin_username') or session.get('nova_user_nick') or '',
        'admin': bool(session.get('admin_logged_in')),
        'google_login': google_login,
        'email': session.get('nova_user_email', '') if google_login else '',
        'avatar': session.get('nova_user_avatar', '') if google_login else '',
    })


@admin_bp.route('/api/session/logout', methods=['POST'])
def session_logout():
    for key in ('nova_user_nick', 'nova_signed_in'):
        session.pop(key, None)
    return jsonify({'success': True})


@admin_bp.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.clear()
    return jsonify({'success': True})

@admin_bp.route('/api/admin/check')
def admin_check():
    if session.get('admin_logged_in'):
        return jsonify({'logged_in': True, 'username': session.get('admin_username', 'admin')})
    return jsonify({'logged_in': False})

# ========== АДМИН API ==========

@admin_bp.route('/api/admin/stats')
@_admin_required
def admin_stats():
    from routes import chat as chat_routes
    return jsonify({
        "models": _safe_provider_models(),
        "current_provider": config.current_provider,
        "current_model": config.current_model,
        "history_messages": len(chat_routes.contents),
        "plugins_loaded": list(config.plugins.keys()),
        "custom_commands": list(config.CUSTOM_ALIASES.keys()),
        "system_prompt": config.system_prompt,
        "voice_enabled": os.environ.get("ASSISTANT_VOICE_REPLY", "0") == "1"
    })

@admin_bp.route('/api/admin/settings', methods=['POST'])
@_admin_required
def admin_settings():
    data = request.get_json() or {}
    # Настройки живут в config — их читают ai_providers и чат; локальные копии ничего не меняли
    if "system_prompt" in data:
        config.system_prompt = data["system_prompt"]
    if "provider" in data and data["provider"] in PROVIDERS:
        config.current_provider = data["provider"]
    if "model" in data:
        from ai_providers import is_free_ai_model
        selected = None
        for key, pdata in PROVIDERS.items():
            for m in pdata["models"]:
                if m["id"] == data["model"] and is_free_ai_model(key, m["id"], m):
                    selected = (key, m["id"])
                    break
            if selected:
                break
        if selected is None:
            return jsonify({"error": "Разрешены только бесплатные AI-модели"}), 400
        config.current_provider, config.current_model = selected
    return jsonify({"success": True})

@admin_bp.route('/api/admin/save_code', methods=['POST'])
@_admin_required
def admin_save_code():
    data = request.get_json() or {}
    filename = os.path.basename(str(data.get("filename", "script.py"))) or "script.py"
    code = data.get("code", "")
    if not code:
        return jsonify({"error": "Нет кода"}), 400
    code_dir = SAVED_CODES_DIR
    os.makedirs(code_dir, exist_ok=True)
    filepath = os.path.join(code_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(code)
    return jsonify({"success": True, "filepath": filepath})

@admin_bp.route('/api/admin/run_saved_code', methods=['POST'])
@_admin_required
def admin_run_saved_code():
    data = request.get_json() or {}
    filename = os.path.basename(str(data.get("filename", "script.py"))) or "script.py"
    code_dir = SAVED_CODES_DIR
    filepath = os.path.join(code_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Файл не найден"}), 404
    try:
        result = subprocess.run(["python3", filepath], capture_output=True, text=True, timeout=10)
        output = result.stdout
        if result.stderr:
            output += "\nSTDERR: " + result.stderr
        return jsonify({"result": output or "Нет вывода"})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Превышено время (10 сек)"}), 408
    except (OSError, subprocess.SubprocessError):
        log.exception("Saved code execution failed")
        return jsonify({"error": "Не удалось выполнить сохранённый скрипт"}), 500

@admin_bp.route('/api/admin/saved_codes')
@_admin_required
def admin_saved_codes():
    code_dir = SAVED_CODES_DIR
    if not os.path.exists(code_dir):
        return jsonify({"files": []})
    files = sorted([f for f in os.listdir(code_dir) if f.endswith(".py")])
    return jsonify({"files": files})

@admin_bp.route('/api/admin/load_code')
@_admin_required
def admin_load_code():
    filename = os.path.basename(request.args.get("file", ""))
    code_dir = SAVED_CODES_DIR
    filepath = os.path.join(code_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Файл не найден"}), 404
    with open(filepath, "r", encoding="utf-8") as f:
        code = f.read()
    return jsonify({"code": code, "filename": filename})


# ========== СТАТУС КЛЮЧЕЙ GROQ (админка) ==========

@admin_bp.route('/api/admin/groq_keys')
@_admin_required
def admin_groq_keys():
    """Статус всех Groq API ключей"""
    return jsonify({
        "keys": get_groq_key_status(),
        "current_key_index": groq_rotation.groq_key_index,
        "total_keys": len(GROQ_KEYS),
        "cooldown_hours": GROQ_KEY_COOLDOWN / 3600
    })

@admin_bp.route('/api/admin/groq_keys/reset', methods=['POST'])
@_admin_required
def admin_reset_groq_keys():
    """Сбросить все cooldown'ы (для админа)"""
    with groq_rotation._groq_lock:
        for key_info in GROQ_KEYS:
            key_info["exhausted_at"] = None
        groq_rotation.groq_key_index = 0
    return jsonify({"success": True, "message": "Все ключи сброшены"})
