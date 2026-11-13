"""routes/council.py — HTTP-слой «Совета моделей».

Один маршрут, который делает то, чего нет у обычного чата: задаёт один вопрос
нескольким моделям одновременно и показывает, где они согласны, а где нет.

Права и лимиты
--------------
Совет дороже обычного ответа (несколько запросов вместо одного), поэтому у
него собственный суточный лимит: ``COUNCIL_RATE_GUEST`` (10/час) и
``COUNCIL_RATE_USER`` (60/час). Лимит отключается ``COUNCIL_RATE_LIMIT=0``.
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request, session

import council
import security
from routes.terminal import signed_in

council_bp = Blueprint("council", __name__)
log = logging.getLogger("novamind.council.routes")


def _rate_key() -> str:
    return str(session.get("nova_user_email") or session.get("nova_user_nick") or "")


@council_bp.post("/api/council/ask")
def ask_council():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question") or payload.get("message") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "Нужен вопрос"}), 400
    if len(question) > 4000:
        question = question[:4000]

    signed = bool(signed_in())
    if not security.is_internal_run(request):
        allowed, retry_after, limit, remaining = security.council_rate_check(
            request, signed, _rate_key())
        if not allowed:
            response = jsonify({
                "ok": False, "error": "Совет моделей — дорогая операция. Попробуйте позже.",
                "retry_after": retry_after, "limit": limit,
            })
            response.headers["Retry-After"] = str(retry_after)
            return response, 429
    else:
        limit, remaining = 0, 0

    try:
        import guardrails
        verdict = guardrails.check_input(question)
        if not verdict.allowed:
            return jsonify({"ok": False, "error": "Вопрос отклонён проверкой безопасности",
                            "guard": verdict.event("council")}), 400
        question = verdict.text.strip() or question
    except Exception:  # noqa: BLE001 — гардрейлы не обязательны для совета
        log.debug("Guardrails недоступны для совета", exc_info=True)

    seats = payload.get("seats")
    try:
        seats = int(seats) if seats is not None else None
    except (TypeError, ValueError):
        seats = None

    try:
        result = council.ask(question, limit=seats,
                             chat_id=str(payload.get("chat_id") or ""))
    except council.NoModels:
        return jsonify({
            "ok": False,
            "error": ("Совету нужны модели. Добавьте бесплатный ключ Groq, Cerebras или "
                      "Gemini в настройках — или задайте COUNCIL_MODELS."),
            "needs": ["GROQ_API_KEY", "CEREBRAS_API_KEY", "GEMINI_API_KEY"],
        }), 503
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    except Exception:  # noqa: BLE001 — совет не должен отдавать 500 без текста
        log.exception("Совет моделей упал")
        return jsonify({"ok": False, "error": "Совет не собрался: внутренняя ошибка"}), 500

    response = jsonify({"ok": True, **result})
    if limit:
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
    return response


@council_bp.get("/api/council/history")
def council_history():
    """История советов: свои — всем, чужие — администратору."""
    from routes.terminal import is_admin
    try:
        import identity
        owner = identity.chat_owner() or ""
    except Exception:  # noqa: BLE001 — вне запроса владельца нет
        owner = ""
    items = council.history(owner=None if is_admin() else owner, limit=20)
    return jsonify({"ok": True, "items": items, "admin": bool(is_admin())})
