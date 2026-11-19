# Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/). Версии следуют SemVer; для MVP используется `0.x`. Дата: 2026-11-19.

## [Unreleased] — production hardening (branch `arena/b009de6e-novamind-v2`)

### Added
- `AUDIT_BEFORE_CHANGES.md`, `BASELINE_TEST_RESULTS.md`, characterization-тесты публичных контрактов.
- `observability.py`: `X-Request-ID`, структурированные JSON-логи, redaction, метрики, `/healthz`, `/readyz`, `/metrics`, единые JSON-ошибки.
- `web_hardening.py`: CSP/X-Content-Type-Options/Referrer-Policy/Permissions-Policy/frame protection/HSTS, безопасные cookie, Origin/Referer CSRF, опциональный double-submit, production config validation.
- `upload_security.py`: normalize filename, magic bytes, ZIP-bomb guard, antivirus hook; интеграция в `/api/attachments/analyze` и `/generated_image`.
- `auth.py`: User/Session/Workspace/Organization/Membership/Role, session rotation, logout-all, ownership helper.
- `provider_policy.py`: capability registry, токены/оценка стоимости, circuit breaker.
- `db_config.py`, `alembic.ini`, `migrations/`: конфигурация PostgreSQL и baseline migration.
- `jobs.py`: JobStore/Executor с состояниями queued…expired.
- Endpoint rate limits в `security.py` с локальным in-process fallback.
- `scripts/scan_secrets.py`, `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.editorconfig`, `.github/dependabot.yml`, `.github/workflows/ci.yml`.
- Документы: SECURITY, ARCHITECTURE, DEPLOYMENT, OPERATIONS, DATABASE_MIGRATIONS, API, THREAT_MODEL, AI_QUALITY; eval'ы multilingual/factuality/citations/fallback/long-context.
- Тесты: `test_hardening_characterization`, `test_web_hardening`, `test_terminal_hardening`, `test_upload_hardening`, `test_auth_hardening`, `test_provider_policy`, `test_infrastructure_abstractions`, `test_rate_limits`.

### Changed
- Одноразовый `ADMIN_CODE` больше **не печатается** в логи.
- Терминал: resource limits (CPU/RAM/file size/опционально processes), аудит каждой команды, санитизированное окружение без секретов.
- Лимит загрузки по умолчанию снижен с 500 МБ до 25 МБ; файл проверяется до записи на диск.
- `defusedxml` для PPTX, timeouts в demo-плагинах, «nosec» только с justification.
- `requirements.txt`: pin `soupsieve>=3.0.2` (CVE-2026-126190/126191/126193) и `defusedxml`.
- Админ-вход теперь проходит через `auth.grant_admin`, поддерживает logout-all и ротацию.
- README: правильное имя проекта и URL клонирования (дополнено в этой ветке).

### Deprecated
- Legacy admin cookie без `admin_epoch` — только для совместимости, отключается `ADMIN_LEGACY_SESSIONS=0`.
- In-process rate limits и JobStore — только для одного процесса; мигрировать на Redis.

### Breaking changes
- `MAX_CONTENT_LENGTH` снижен с 500 МБ до 25 МБ (`MAX_UPLOAD_BYTES`).
- Сессия живёт 90 дней вместо 365; владельцы чатов не меняются.
- Файлы `routes/uploads/` больше не отслеживаются git (файл остался на диске).
- Добавлены security headers; CSP по умолчанию разрешает inline-скрипты legacy-UI (`CSP_STRICT=1` ужесточает).

## [0.1.0] — базовая версия

Исходная система: Flask, ReAct-агент, поиск, MCP, медиа, SQLite, тесты и eval'ы.
