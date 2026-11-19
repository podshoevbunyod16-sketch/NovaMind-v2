# FINAL_AUDIT.md — итог production-hardening

Дата: 2026-11-19. Ветка: `arena/b009de6e-novamind-v2` (база `868df79`). Работа выполнена **только** в текущей ветке: имя `agent/production-hardening` из ТЗ не может быть использовано, потому что сессия закреплена за текущей веткой. `main` не изменялся.

Этот отчёт не утверждает «всё исправлено»: ниже указаны и зелёные, и красные проверки, и остаточные риски.

## Результаты проверок

| Проверка | Baseline | После изменений | Статус |
|---|---|---|---|
| `pytest -q tests` | 1089 passed | **1155 passed** | ✅ PASS |
| `pytest --cov --cov-fail-under=70` | 81% (с тестами в знаменателе) | **72.92%** (first-party без tests/examples), порог 70% достигнут | ✅ PASS |
| `ruff check .` | 566 errors | 0 (_scope_ E9/F63/F7/F82/B, legacy style не переписывался) | ✅ PASS (с оговоркой) |
| `mypy` | duplicate module `app` | 0 (8 новых типизированных модулей, `follow_imports=skip`) | ✅ PASS (с оговоркой) |
| `bandit -ll` (first-party) | 10 medium issues | **0 medium/high**, low-severity findings остаются | ✅ PASS (с оговоркой) |
| `pip-audit -r requirements.txt` | 3 CVE в soupsieve 2.10 | pin `soupsieve>=3.0.2`; **локально FAIL**: sandbox Python 3.11.2 < 3.11.5, fixed version недоступна | ⚠️ ENVIRONMENT FAIL |
| `python scripts/scan_secrets.py --history` | частично вручную | **0 finding** (working tree + история, без test fixtures) | ✅ PASS |
| `alembic upgrade head --sql` | конфигурации не было | exit 0, 4 `CREATE TABLE` | ✅ PASS |
| `compileall` | PASS | PASS | ✅ PASS |
| `node tests/frontend/smoke.mjs` | 196 ✓ | **196 ✓**, exit 0 | ✅ PASS |
| Docker build | CLI отсутствует | не запускался | ⚠️ NOT RUN |
| Live smoke `/healthz`, `/readyz`, headers, cookies | — | 200/200, CSP + X-Request-ID + HttpOnly/SameSite presence | ✅ PASS |

## Исправлено

**P0:**
- `app.py` больше не логирует сгенерированный `ADMIN_CODE`; startup validation отключает запуск в production без сильных секретов.
- Терминал: санитизированное окружение (без ключей), resource limits CPU/RAM/file size/опционально processes, структурированный audit-log каждой команды, сохранение запрета на host-команды и path traversal-проверок.
- Security headers, cookie policy (HttpOnly/SameSite/Secure), CSRF Origin/Referer + double-submit режим, `Cache-Control: no-store`.
- Пользовательский скриншот `routes/uploads/*.jpg` убран из индекса git (физически остался на диске) и добавлен в `.gitignore`.

**P1:**
- Загрузка файлов: проверка до записи на диск, normalize filename, magic bytes, ZIP-bomb guard, antivirus hook, безопасный `Content-Disposition` и realpath-проверка в `/generated_image`.
- `observability.py`: `X-Request-ID`, correlation, JSON-логи с redaction, метрики, единый формат ошибок, `/healthz`, `/readyz`, `/metrics`.
- `auth.py`: User/Session/Workspace/Organization/Membership/Role, server-side admin check, `/api/session/rotate`, `/api/admin/sessions/logout-all`, ownership helper.
- Rate limits на дорогие endpoint'ы (upload/terminal/media/agent/API) с in-process fallback.
- `defusedxml` для PPTX, timeout'ы в demo-плагинах, аннотации `# nosec B608` с обоснованием.
- `soupsieve>=3.0.2` зафиксирован (CVE-2026-126190/126191/126193), `requirements-dev` дополнен Alembic/SQLAlchemy.
- Dockerfile (non-root, healthcheck, gunicorn gthread), docker-compose, `.dockerignore`, `.editorconfig`, Dependabot, unified CI workflow.
- Документация: README (имя/URL/ограничения/troubleshooting), SECURITY, THREAT_MODEL, ARCHITECTURE, DEPLOYMENT, OPERATIONS, DATABASE_MIGRATIONS, API, AI_QUALITY, CHANGELOG, CONTRIBUTING, CODE_OF_CONDUCT, LICENSE.
- Evals: добавлены multilingual, factuality/citations, provider fallback, long context (итого 55 YAML-задач с учётом arena_signals).

**P2:**
- `provider_policy.py`: capability registry, token/cost estimate, circuit breaker, ledger; `/api/ai/capabilities`.
- `db_config.py` + Alembic baseline migration; `SQLITE_DB_PATH` support.
- `jobs.py`: JobStore/Executor с полным набором состояний и TTL/expire.
- Characterization-тесты публичных контрактов + 8 новых тестовых модулей hardening.

## Частично исправлено

- **CSRF**: default проверяет Origin/Referer; без заголовка Origin (не-браузерные клиенты) запрос проходит, чтобы не ломать совместимость. Полная защита требует `CSRF_REQUIRE_HEADER=1` и обновления frontend.
- **CSP**: default сохраняет `'unsafe-inline'` из-за inline-обработчиков в legacy-шаблонах; `CSP_STRICT=1` доступен после миграции шаблонов.
- **Аутентификация**: browser ID остаётся владельцем анонимных чатов; полноценные аккаунты/MFA/Organization membership — только модель и helper'ы, не готовая БД.
- **База данных**: SQLite repository остался прямым SQL; PostgreSQL работает только через Alembic/`DATABASE_URL`, ORM-слой не внедрён.
- **Токены/стоимость/метрики**: есть ledger и Prometheus-ready counters, но OpenTelemetry/Sentry/Prometheus серверы не подключены.
- **Rate limit/Job store**: in-process; горизонтальное масштабирование требует Redis.
- **Ruff/MyPy**: gate сужен до significant rules / typed modules; остальные 500+ stylistic замечаний не переписаны.

## Требует внешней инфраструктуры

- Redis для распределённых лимитов, job state и distributed locks.
- PostgreSQL с бэкапами и репликацией; S3-совместимое хранилище с карантином.
- ClamAV или другой антивирус (`AV_SCAN_COMMAND`), отдельный UID/контейнер/cgroups/seccomp для терминала.
- Sentry DSN, OpenTelemetry collector, Prometheus, реальные ключи провайдеров и платные модели (не подключались).
- Human-labeled eval set и прогон eval'ов с живой моделью (в офлайне `evals.gate` возвращает `skipped`).

## Требует ручного решения

- Удаление пользовательского скриншота из **истории** Git — history rewrite запрещён ТЗ; владельцу файла нужно удалить данные и, при необходимости, инициировать отдельный инцидент.
- Выбор `ADMIN_LEGACY_SESSIONS=0` в production (разовый логаут старых админов).
- Включение `CSP_STRICT=1` после перевода inline-handlers на внешние скрипты.
- Подтверждение платных провайдеров/бюджетов и передача реальных секретов через секрет-менеджер.
- Локальный `pip-audit` на Python ≥3.11.5/3.12 в вашем окружении: в sandbox доступен только 3.11.2.
- Docker build в среде с установленным Docker (CI workflow готов).

## Остаточный риск

- Агентный терминал — не полноценная sandbox ОС: при включении внутри одного контейнера возможны атаки через разрешённые интерпретаторы и локальную сеть.
- Prompt injection снижается разметкой и guardrails, но модель может быть обманута; требуется человеческое подтверждение рискованных действий.
- Security headers и rate limits не заменяют reverse proxy/WAF.
- Coverage 72.92% включает ключевые модули, но `routes/agent.py` (~44%) и `routes/composio.py` (~21%) покрыты слабо.
- 65 low-severity Bandit findings остаются (главным образом `try/except pass`, subprocess import, B105 false positives).
- Локальный `pip-audit` формально красный из-за версии Python, а не из-за отсутствия фикса зависимости.

## Breaking changes / migration notes

1. `MAX_CONTENT_LENGTH` снижен 500 МБ → 25 МБ (`MAX_UPLOAD_BYTES`). Клиенты, загружающие больше, получат 413.
2. `permanent_session_lifetime` 365 → 90 дней; владельцы чатов (`nova_sid`) не меняются, но сессия потребует повторного входа.
3. Админ-вход теперь пишет `admin_epoch`; legacy-сессии без него работают только при `ADMIN_LEGACY_SESSIONS=1` (default). Для production установите `0`.
4. `routes/uploads/` больше не отслеживается git; файл остался в рабочей папке и в историческом коммите.
5. Security headers добавлены на все ответы; при жёстком CSP (`CSP_STRICT=1`) legacy-inline UI перестанет работать до миграции шаблонов.
6. Для существующей SQLite-БД: `alembic stamp head` (не `upgrade` — таблицы уже созданы `init_db()`), предварительно `sqlite3 ... ".backup ..."`.
7. Новые переменные окружения — см. `.env.example`: `APP_ENV`, `ADMIN_LEGACY_SESSIONS`, `CSP_STRICT`, `CSRF_*`, `COOKIE_SECURE`, `MAX_UPLOAD_BYTES`, `TERMINAL_*`, `RATE_*`, `PROVIDER_BREAKER_*`, `MODEL_TOKEN_BUDGET`, `DATABASE_URL`, `SQLITE_DB_PATH`, `AV_SCAN_COMMAND`, `LOG_JSON`.

## Нельзя считать production-ready

Без внешней инфраструктуры (Redis/PostgreSQL/S3/контейнер с изоляцией), реальных секретов, успешного `pip-audit` на поддерживаемой версии Python и проверенного Docker build проект остаётся **hardened MVP/local-first**, а не полностью production-ready сервисом.
