"""version.py — единая версия продукта и список возможностей.

Версию видно в ``/version``, в шапке здоровья и в квитанциях. Список
``FEATURES`` — не реклама, а контракт: production-проба и страница доверия
показывают ровно то, что действительно включено в этой сборке.
"""
from __future__ import annotations

import os

__version__ = "3.0.0"

#: Идентификатор сборки: CI/платформа кладут сюда коммит.
BUILD = (os.getenv("NOVA_BUILD") or os.getenv("GIT_COMMIT")
         or os.getenv("RENDER_GIT_COMMIT") or "dev").strip()

NAME = "NovaMind"
CODENAME = "Khirad"

#: Фирменные подсистемы этой сборки.
FEATURES = (
    "receipts",       # проверяемые квитанции с хэш-цепочкой
    "council",        # совет моделей с картой расхождений
    "trust-center",   # страница доверия /trust
    "production",     # health/readiness/metrics/security headers
)
