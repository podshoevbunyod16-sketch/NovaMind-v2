# AUDIT_BEFORE_CHANGES.md — аудит NovaMind-v2 до изменений

Дата: 2026-11-19. Ветка: `arena/b009de6e-novamind-v2` (база `868df79`). Аудит проведён до изменения исходного кода. Ниже — фактические находки, а не обещания.

## 1. Область и архитектура

- Точка входа: `app.py` (Flask + 19 blueprint'ов из `routes/__init__.py`), запуск через `app.run()` / Gunicorn (`Procfile`, `render.yaml`).
- Конфигурация: `config.py` (.env, провайдеры, runtime_settings.json), `groq_rotation.py`, `provider_manager.py`.
- Данные: `database.py` (SQLite, `novamind_chats.db`), `memory.py`, `knowledge.py`, `usage_stats.py`, файлы в `workspace/`, `generated_media/`, `routes/uploads/`.
- AI: `ai_providers.py`, `provider_manager.py`, `local_providers.py`, `prompt_registry.py`, `language_policy.py`.
- Агент: `agent/*`, `routes/agent.py`, `routes/react_agent.py`, `tool_registry.py`, `guardrails.py`, `mcp_*`.
- Инструменты: `routes/terminal.py`, `routes/search.py`, `routes/media.py`, `routes/workspace.py`, `routes/tts.py`.
- Фронтенд: `templates/*`, `static/*.js/css`, smoke-тест `tests/frontend/smoke.mjs`.
- Тесты: 1089 pytest-тестов, `evals/*`, 4 GitHub Actions workflow.

Границы доверия: браузер (недоверенный ввод) → Flask-сессия (только `nova_sid` для анонимов) → агенты и инструменты → внешние HTTP/МСР-источники → модель провайдера. Внешний контент (веб, файлы, MCP) потенциально содержит инструкции, но коду нужно останавливать его на границе.

## 2. Найденные проблемы

Обозначения: P0 — exploiting/утечка секретов или произвольное выполнение; P1 — существенный риск/отсутствие критичной защиты; P2 — качество/надежность; P3 — улучшения.

| ID | Criticality | Finding | Files | Risk of change | Plan |
|---|---|---|---|---|---|
| F-01 | P0 | При старте без `ADMIN_CODE` одноразовый код администратора пишется в логи/stdout (`logging.warning`). Логи доступны операторам и CI. | `app.py`, `config.py` | Low | Убрать вывод кода; при сгенерированном коде писать только факт и рекомендацию; в production требовать явный `ADMIN_CODE`. |
| F-02 | P0 | Терминал — это не sandbox ОС: whitelist команд, но в нём есть `curl/wget/nc/ssh/apt/pip/python3`, наследуется **полное окружение сервера** (включая ключи) и общее ядро; `start_new_session` без resource limits. | `routes/terminal.py`, `agent/*` | Medium | Санитизировать env, добавить resource limits (CPU/AS/processes/FD), audit-log, явный запрет на передачу секретов; оставить выключенным по умолчанию; документировать, что нужен контейнер для реальной изоляции. |
| F-03 | P0 | Нет security headers (CSP, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, frame protection, HSTS), нет CSRF-проверки на state-changing запросах, cookie не помечаются `HttpOnly/Secure/SameSite` явно. | `app.py` | Medium | Добавить after_request, Origin/Referer CSRF-check и конфигурацию сессионных cookie; обратная совместимость — проверка только при наличии cookie/Origin. |
| F-04 | P0 | `routes/uploads/*.jpg` (файл пользователя) закоммичен в git, хотя `.gitignore` запрещает `uploads/`: приватные данные в истории репозитория. | `routes/uploads/` | Medium | Разе индексировать файл (`git rm --cached`, без удаления с диска), не трогать историю, документировать остаточный риск. |
| F-05 | P1 | Загрузка файла до проверки размера сохраняет до 500 МБ на диск (`MAX_CONTENT_LENGTH = 500MB`, лимит анализа 25 МБ применяется после `save()`); MIME берётся от клиента; нет проверки magic bytes, ZIP-bomb, quotas, Content-Disposition. | `app.py`, `routes/media.py` | Medium | Ограничить `MAX_CONTENT_LENGTH`, проверять magic bytes и расширение, лимитировать размер потока до записи, добавить safe filename/Content-Disposition, ZIP-ratio guard и antivirus hook. |
| F-06 | P1 | Нет единого request ID / correlation ID, структурированных JSON-логов, central redaction, unified error format, readiness endpoint и Prometheus-ready метрик. `/api/system/health` возвращает подробности о внутреннем состоянии. | `app.py`, `routes/diagnostics.py`, `config.py` | Medium | Добавить `observability.py`: request-id, JSON formatter, redaction filter, counters, `/healthz`, `/readyz`, error handlers без stack trace в production. |
| F-07 | P1 | Идентичность анонима — подписанная cookie `nova_sid`; это изоляция браузера, не аккаунт. Нет User/Session/Workspace/Organization/Membership/Role-моделей, logout всех устройств и ротации сессий. | `identity.py`, `routes/admin.py`, `database.py` | High | Добавить слой абстракций и документировать миграционный путь; не ломать текущие владельцев чатов (`anon:<sid>`). |
| F-08 | P1 | SQLite hard-coded в `database.py`; нет `DATABASE_URL`/PostgreSQL-режима, Alembic-миграций, явных transaction/retry политик и backup-инструкции. | `database.py`, `requirements.txt` | High | Добавить конфигурацию подключения и документацию миграций без изменения текущих вызовов. |
| F-09 | P1 | `pip-audit`: 3 CVE в transitive `soupsieve 2.10` (через `beautifulsoup4<5`). | `requirements.txt` | Low | Зафиксировать минимальную исправленную версию, перепроверить аудит. |
| F-10 | P1 | Bandit (first-party): 75 findings (65 low, 10 medium), включая subprocess без timeout, XML parsing, requests без timeout, SQL-строки. Часть — ложные срабатывания, но требуют разбора. | несколько модулей | Medium | Добавить целевой Bandit CI, точечно исправить значимые, остальные задокументировать с основаниями. |
| F-11 | P1 | Ruff: 566 замечаний; MyPy: `Duplicate module named app` из-за `examples/flask_shop/app.py`. Нет единого форматтера/type-check конфига. | репозиторий | Medium | Добавить `pyproject.toml` с разумными исключениями и запускать проверки на first-party коде, не переписывая всё сразу. |
| F-12 | P1 | Нет Dockerfile/docker-compose, `.dockerignore`, `.editorconfig`, Dependabot, SECURITY.md, CODE_OF_CONDUCT.md, LICENSE, ARCHITECTURE/DEPLOYMENT/OPERATIONS/API/THREAT_MODEL/DATABASE_MIGRATIONS/AI_QUALITY/FINAL_AUDIT. README содержит старое имя `YouTubegopro` и неверный URL клонирования `NovaMind.git`. | docs, `.github/`, корень | Low | Добавить недостающие артефакты и обновить README. |
| F-13 | P2 | Провайдеры и MCP в основном имеют timeout/retry/failover (`provider_manager.py`), но нет централизованного registry возможностей моделей, accounting_tokens/cost, circuit breaker на уровне API и health/diagnostics в одном формате. | `provider_manager.py`, `ai_providers.py`, `config.py` | Medium | Добавить декларативный registry и метрики, не меняя интерфейс `chat_completion`. |
| F-14 | P2 | Много старых тестов фиксируют текущие ответы; нет characterization-тестов на security headers/cookie/upload/request-id/readyz. | `tests/` | Low | Добавить отдельные тесты до изменения поведения. |
| F-15 | P2 | `routes/react_agent.py.conflict` — артефакт конфликта в репозитории; `examples/*` содержит исполняемый код, попадающий в проверки. | `routes/`, `examples/` | Low | Исключить из инструментов разработки и пометить в документации. |
| F-16 | P3 | `pytest` coverage 81% (включая тесты в знаменателе);CI не проверяет coverage/threshold, matrix, bandit/pip-audit/secret scan/docker build. | `.github/workflows/`, `tests/` | Low | Обновить CI после стабилизации. |

## 3. Что нельзя решить без внешней инфраструктуры

- Настоящая изоляция терминала (отдельный UID, cgroups, seccomp, планировщик контейнеров) — требует Docker/Kubernetes и отдельного deployment; в коде можно только запретить небезопасный fallback и подготовить интерфейс.
- Redis для распределённого rate limiting,_job state и distributed locks; текущая реализация — in-process (`security.py`).
- PostgreSQL, репликация и backup — требует базы и секретов доступа.
- S3-compatible хранилище, антивирус (ClamAV), quarantine — требует сервисов и ключей.
- Sentry/OpenTelemetry/Prometheus бэкенды, платные провайдеры AI — требуют конфигурации и (для платных) явного подтверждения.
- Удаление уже закоммиченного пользовательского файла из истории Git —— потребует history rewrite, что запрещено условиями задачи; достаточно untrack + ротация данных владельцем.

## 4. Приоритет исправлений в этой ветке

1. P0: F-01…F-04 (секреты, заголовки/CSRF, sandbox-границы, приватный файл в git).
2. P1: F-05…F-12 (upload, observability, auth-ready abstraction, db abstraction, dependencies, quality gates, docs/CI/DevOps).
3. P2: F-13…F-16 (registry, characterization tests, уборка артефактов, CI).

Каждое изменение сопровождается тестом и запуском релевантной проверки; итог — `FINAL_AUDIT.md` с честным списком пройденных/непройденных проверок.
