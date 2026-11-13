"""wsgi.py — точка входа для gunicorn/uWSGI.

Приложение уже собирается в ``app.py`` (Flask + все blueprint'ы +
production-обвязка). Здесь только WSGI-имя, которое ждут серверы:

    gunicorn -c gunicorn.conf.py wsgi:application
"""
from app import app as application  # noqa: F401 — имя для WSGI-сервера

# Удобно для отладки: python -c "import wsgi; wsgi.app.test_client().get('/healthz')"
app = application


if __name__ == "__main__":
    import os
    application.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
