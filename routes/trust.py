"""routes/trust.py — страница доверия NovaMind (/trust).

Одна страница, где видно всё, что обычно спрятано: целостность цепочки
квитанций, стоимость токенов, состояние готовности и история советов моделей.
Данные она берёт из уже существующих API, поэтому страница ничего не пишет
и не меняет состояние.
"""
from __future__ import annotations

from flask import Blueprint, render_template

trust_bp = Blueprint("trust", __name__)


@trust_bp.get("/trust")
def trust_page():
    return render_template("trust.html")
