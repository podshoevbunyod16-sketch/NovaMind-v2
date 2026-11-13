"""routes/receipts.py — публичный API проверяемых квитанций Nova Receipts.

Маршруты:

  * ``GET /api/receipts`` — последние квитанции владельца (сводка + список);
  * ``GET /api/receipts/verify`` — проверка всей цепочки и своей части;
  * ``GET /api/receipts/export`` — JSONL-выгрузка для независимого аудита;
  * ``GET /api/receipts/<id>`` — одна квитанция с проверкой её звена.

Доступ: пользователь видит только свои квитанции, администратор — все.
Проверка цепочки всегда идёт по всей базе (иначе разрыв можно было бы
спрятать), но из отчёта не раскрываются чужие записи — только их количество
и идентификаторы поломок.
"""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

import identity
import receipts
from routes.terminal import is_admin

receipts_bp = Blueprint("receipts", __name__)


def _owner() -> str:
    try:
        return identity.chat_owner() or ""
    except Exception:  # noqa: BLE001 — вне запроса владельца нет
        return ""


@receipts_bp.get("/api/receipts")
def list_receipts():
    owner = _owner()
    admin = bool(is_admin())
    limit = request.args.get("limit", "50")
    chat_id = (request.args.get("chat_id") or "").strip()[:80]
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = 50
    items = receipts.chain(owner=None if admin else owner, chat_id=chat_id or None,
                           limit=limit)
    return jsonify({
        "ok": True,
        "admin": admin,
        "items": items,
        "stats": receipts.stats(owner=None if admin else owner),
    })


@receipts_bp.get("/api/receipts/verify")
def verify_receipts():
    report = receipts.verify()
    owner = _owner()
    mine = receipts.stats(owner=owner)
    report["scope"] = "all"
    report["mine"] = mine
    return jsonify({"ok": True, "report": report})


@receipts_bp.get("/api/receipts/export")
def export_receipts():
    owner = _owner()
    admin = bool(is_admin())
    lines = receipts.export_lines(owner=None if admin else owner, limit=100000)
    body = "\n".join(lines) + ("\n" if lines else "")
    return Response(
        body,
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="nova-receipts.jsonl"'},
    )


@receipts_bp.get("/api/receipts/<int:receipt_id>")
def get_receipt(receipt_id: int):
    entry = receipts.get(receipt_id)
    if not entry:
        return jsonify({"ok": False, "error": "Квитанция не найдена"}), 404
    owner = _owner()
    if entry.get("chat_id"):
        try:
            import database
            chat_owner = database.chat_owner_of(entry["chat_id"])
        except Exception:  # noqa: BLE001 — чат мог быть удалён
            chat_owner = ""
        if chat_owner and chat_owner != owner and not is_admin():
            return jsonify({"ok": False, "error": "Квитанция не найдена"}), 404
    elif not is_admin() and owner:
        # Квитанции без чата (совет, служебные) сверяем по сводке: чужой id
        # пользователю не отдаём.
        mine = {item["id"] for item in receipts.chain(owner=owner, limit=200)}
        if receipt_id not in mine:
            return jsonify({"ok": False, "error": "Квитанция не найдена"}), 404
    entry["verification"] = receipts.verify_one(receipt_id)
    return jsonify({"ok": True, "receipt": entry})
