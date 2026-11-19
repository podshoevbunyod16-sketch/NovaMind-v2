# DEPLOYMENT.md — запуск и деплой

## Локальная разработка

```bash
git clone https://github.com/podshoevbunyod16-sketch/NovaMind-v2.git
cd NovaMind-v2
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # заполните свои значения, реальные ключи не коммитьте
python app.py             # http://127.0.0.1:5000/chat
```

Docker: `docker compose up --build` (терминал выключен). Инфраструктура для PostgreSQL/Redis: `docker compose --profile infra up -d`.

## Production checklist

1. Python 3.11.5+ или 3.12 (нужна исправленная версия `soupsieve`), зависимости из `requirements.txt`.
2. `APP_ENV=production`, `FLASK_DEBUG=false`, HTTPS на прокси.
3. Обязательные переменные: `SESSION_SECRET` (≥32 случайных символов), `ADMIN_CODE` (≥12), `SITE_URL=https://…`.
4. `ADMIN_LEGACY_SESSIONS=0`, `COOKIE_SECURE=1`, при необходимости `CSRF_REQUIRE_HEADER=1` и `CSP_STRICT=1` после миграции шаблонов.
5. `TERMINAL_ENABLED=0`, если терминал нужен — только в изолированном контейнере с `SANDBOX_CONTAINER=1`, отдельным UID и cgroup limits.
6. PostgreSQL через `DATABASE_URL`, миграции: `alembic upgrade head`; бэкапы — см. DATABASE_MIGRATIONS.md.
7. Redis для общих лимитов и job state (сейчас in-process).
8. Логи: `LOG_JSON=1`, централизованное хранилище с доступом только у операторов. Sentry DSN — через секрет, не в коде.
9. Проверки: `curl /healthz`, `/readyz`, `/metrics`; в Dockerfile уже настроен Docker healthcheck.
10. Грейсфул-старт: `gunicorn -k gthread -w 2 --threads 4 --timeout 120 --graceful-timeout 30 app:app`.

See `ops` section in OPERATIONS.md for incidents and routine maintenance.

## Что НЕ делать

- Не запускать `app.run(debug=True)` в prod и не публиковать Werkzeug dev-сервер напрямую.
- Не включать терминал без контейнерной изоляции и подтверждений.
- Не хранить `.env`, `.session_secret`, `*.db`, `generated_media/`, `routes/uploads/` в git или публичных static.
- Не подключать платные модели/провайдеры без явного подтверждения и бюджетов.
