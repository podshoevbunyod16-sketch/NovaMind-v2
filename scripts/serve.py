#!/usr/bin/env python3
"""scripts/serve.py — совместимость: то же, что `python app.py`.

Основной запуск теперь живёт в `app.py` и `server.py`:

    python app.py            # production (gunicorn через Python-API)
    python app.py --dev      # Flask dev-сервер

Этот файл оставлен, чтобы продолжали работать старые команды,
systemd-юниты и скрипты:

    python scripts/serve.py
    python scripts/serve.py --check
    python scripts/serve.py --port 8080

Он не дублирует логику: просто берёт WSGI-приложение и передаёт управление
в `server.main()`.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Реэкспорт для тестов и внешних вызовов: раньше эти имена жили здесь.
from server import banner, build_options, check, runtime_mode  # noqa: F401,E402


def main(argv=None) -> int:
    from wsgi import application
    import server
    return server.main(application, argv)


if __name__ == "__main__":
    raise SystemExit(main())
