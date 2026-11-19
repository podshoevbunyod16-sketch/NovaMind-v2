"""
routes/composio.py — Интеграция с Composio (внешние сервисы)
"""
from flask import Blueprint, request, jsonify, render_template, session
import requests
import os
import secrets
import time
import hmac
import json
import re
import threading

composio_bp = Blueprint("composio", __name__)

# Flask's default session cookie is signed but client-side, so it cannot hold
# one-shot authorization state: an old cookie could replay a consumed token.
_COMPOSIO_SESSION_BINDING_KEY = "composio_session_binding"
_COMPOSIO_CONFIRMATION_TTL_SECONDS = 300
_COMPOSIO_MAX_PENDING = 128
_COMPOSIO_PENDING = {}
_COMPOSIO_PENDING_LOCK = threading.Lock()


def _session_binding():
    binding = session.get(_COMPOSIO_SESSION_BINDING_KEY)
    if not isinstance(binding, str) or len(binding) < 32:
        binding = secrets.token_urlsafe(32)
        session[_COMPOSIO_SESSION_BINDING_KEY] = binding
    return binding


def _prune_pending(now):
    for challenge_id, pending in list(_COMPOSIO_PENDING.items()):
        try:
            expired = now - float(pending.get("created_at", 0)) > _COMPOSIO_CONFIRMATION_TTL_SECONDS
        except (TypeError, ValueError):
            expired = True
        if expired:
            _COMPOSIO_PENDING.pop(challenge_id, None)


def _create_pending(action, params, connected_account_id, binding):
    now = time.time()
    with _COMPOSIO_PENDING_LOCK:
        _prune_pending(now)
        if len(_COMPOSIO_PENDING) >= _COMPOSIO_MAX_PENDING:
            return None
        challenge_id = secrets.token_urlsafe(32)
        _COMPOSIO_PENDING[challenge_id] = {
            "session_binding": binding,
            "action": action,
            "params": params,
            "connected_account_id": connected_account_id,
            "created_at": now,
        }
        return challenge_id


def _consume_pending(challenge_id, binding):
    now = time.time()
    with _COMPOSIO_PENDING_LOCK:
        pending = _COMPOSIO_PENDING.get(challenge_id)
        if not isinstance(pending, dict):
            return None
        try:
            expired = now - float(pending.get("created_at", 0)) > _COMPOSIO_CONFIRMATION_TTL_SECONDS
        except (TypeError, ValueError):
            expired = True
        if expired:
            _COMPOSIO_PENDING.pop(challenge_id, None)
            return None
        expected = pending.get("session_binding", "")
        if not isinstance(binding, str) or not hmac.compare_digest(expected, binding):
            return None
        return _COMPOSIO_PENDING.pop(challenge_id)


@composio_bp.before_request
def _composio_admin_only():
    """Composio работает с аккаунтами ВЛАДЕЛЬЦА (GitHub, почта...): только админ."""
    from routes.terminal import is_admin
    if not is_admin():
        return jsonify({"error": "Composio доступен только администратору"}), 403
    return None

@composio_bp.route('/composio')
def composio_page():
    return render_template('composio.html')

@composio_bp.route('/api/composio/connect', methods=['POST'])
def composio_connect():
    data = request.get_json() or {}
    api_key = data.get('api_key', '')
    if not api_key:
        return jsonify({'error': 'API ключ не указан'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        resp = requests.get('https://backend.composio.dev/api/v3.1/toolkits?limit=5', headers=headers, timeout=10)
        if resp.status_code == 401:
            return jsonify({'error': 'Неверный API ключ'}), 401
        resp.raise_for_status()
        os.environ['COMPOSIO_API_KEY'] = api_key
        return jsonify({'success': True, 'message': 'Подключено к Composio'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@composio_bp.route('/api/composio/integrations', methods=['GET'])
def composio_integrations():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        resp = requests.get('https://backend.composio.dev/api/v3.1/toolkits?limit=50', headers=headers, timeout=10)
        resp.raise_for_status()
        return jsonify({'integrations': resp.json()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@composio_bp.route('/api/composio/connected_accounts', methods=['GET'])
def composio_connected_accounts():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        resp = requests.get('https://backend.composio.dev/api/v3.1/connected_accounts', headers=headers, timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@composio_bp.route('/api/composio/connect_account', methods=['POST'])
def composio_connect_account():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    data = request.get_json() or {}
    toolkit = data.get('toolkit', '')
    if not toolkit:
        return jsonify({'error': 'Укажи toolkit'}), 400
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        session_resp = requests.post('https://backend.composio.dev/api/v3.1/tool_router/session', headers=headers, json={"user_id": "novauser"}, timeout=10)
        session_resp.raise_for_status()
        session_data = session_resp.json()
        session_id = session_data.get('session_id', '')
        if not session_id:
            return jsonify({'error': 'Не удалось создать сессию'}), 500
        link_resp = requests.post(f'https://backend.composio.dev/api/v3/tool_router/session/{session_id}/link', headers=headers, json={"toolkit": toolkit}, timeout=10)
        link_resp.raise_for_status()
        link_data = link_resp.json()
        redirect_url = link_data.get('redirect_url', '')
        return jsonify({'success': True, 'redirect_url': redirect_url, 'connected_account_id': link_data.get('connected_account_id', '')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@composio_bp.route('/api/composio/execute', methods=['POST'])
def composio_execute():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': 'Ожидается объект запроса'}), 400
    action_name = data.get('action', '')
    params = data.get('params', {})
    connected_account_id = data.get('connected_account_id', '')
    if not isinstance(action_name, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', action_name):
        return jsonify({'error': 'Некорректное имя действия'}), 400
    if not isinstance(params, dict):
        return jsonify({'error': 'Параметры действия должны быть объектом'}), 400
    if not isinstance(connected_account_id, str) or len(connected_account_id) > 200:
        return jsonify({'error': 'Некорректный connected_account_id'}), 400
    if len(json.dumps(params, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > 65536:
        return jsonify({'error': 'Параметры действия слишком велики'}), 413

    # Store the exact operation in process-side state. The signed browser cookie
    # carries only an unpredictable binding, never the payload or one-shot token.
    challenge_id = _create_pending(
        action_name, params, connected_account_id, _session_binding()
    )
    if challenge_id is None:
        return jsonify({'error': 'Слишком много ожидающих подтверждений'}), 503
    return jsonify({
        'confirmation_required': True,
        'challenge_id': challenge_id,
        'action': action_name,
        'params': params,
        'expires_in': _COMPOSIO_CONFIRMATION_TTL_SECONDS,
    }), 409


@composio_bp.route('/api/composio/confirm', methods=['POST'])
def composio_confirm():
    """Execute exactly one previously displayed Composio challenge.

    Confirmation is intentionally not accepted together with action/params:
    those values must come from an atomic server-side pending operation bound
    to the current authenticated session. Consuming before provider execution
    prevents replay, including replay with an older signed cookie.
    """
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    challenge_id = data.get('challenge_id')
    if not isinstance(challenge_id, str) or not challenge_id or data.get('confirmed') is not True:
        return jsonify({'error': 'Требуется явное подтверждение ожидающей операции'}), 400
    pending = _consume_pending(challenge_id, session.get(_COMPOSIO_SESSION_BINDING_KEY))
    if pending is None:
        return jsonify({'error': 'Challenge не найден, истёк или уже использован'}), 409
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        payload = {"input": pending['params'], "allow_tracing": True}
        if pending.get('connected_account_id'):
            payload['connected_account_id'] = pending['connected_account_id']
        resp = requests.post(
            f"https://backend.composio.dev/api/v2/actions/{pending['action']}/execute",
            json=payload, headers=headers, timeout=30,
        )
        resp.raise_for_status()
        return jsonify({'result': resp.json()})
    except Exception:
        return jsonify({'error': 'Не удалось выполнить подтверждённое действие'}), 502

@composio_bp.route('/api/composio/actions', methods=['GET'])
def composio_actions():
    api_key = os.getenv('COMPOSIO_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Composio не подключен'}), 400
    toolkit = request.args.get('toolkit', '')
    try:
        headers = {'x-api-key': api_key, 'Content-Type': 'application/json'}
        url = 'https://backend.composio.dev/api/v2/actions?limit=20'
        if toolkit:
            url += f'&apps={toolkit}'
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ========== LEGACY HISTORY API ==========
