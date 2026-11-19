# ARCHITECTURE.md — устройство NovaMind-v2

NovaMind-v2 — Flask-приложение, которое wraps подключаемые AI-модели. Это **не** собственная foundation model: качества и инфраструктура Groq/Gemini/OpenAI и др. принадлежат их провайдерам.

## Слои

```
Browser (templates/, static/*.js)
   │  HTTP JSON / NDJSON SSE-like streams
Flask app (app.py) ── observability (request-id, errors, metrics)
   │                 ── web_hardening (CSP, cookie, CSRF, rate limits)
   ├── routes/*     HTTP-адаптеры: chat, agent, terminal, media, search, admin, ...
   ├── auth.py      сессии, ownership, RBAC-модели, logout-all
   ├── agent/*      ReAct-цикл, политики, подтверждения, workspace, трассы
   │     ├── tool_registry.py  первый набор инструментов
   │     ├── guardrails.py     фильтр ввода/вывода
   │     └── mcp_*             внешние MCP-серверы
   ├── ai_providers.py / provider_manager.py / provider_policy.py
   │                  провайдеры, ключи, backoff/circuit breaker, capabilities
   ├── security.py  in-process rate limits
   ├── jobs.py      фоновые задачи и состояния
   ├── database.py  SQLite (локально), db_config.py + Alembic → PostgreSQL
   └── upload_security.py  проверка файлов до записи
```

## Потоки данных

1. **Чат**: `POST /send_stream` → `routes/chat.py` → `ai_providers.chat_completion_stream` → NDJSON-события в браузер.
2. **Агент**: `POST /api/agent/react` → `agent.loop` → инструмент → результат помечается как `<untrusted>` → модель → проверенный ответ. Опасные действия требуют подтверждения.
3. **Терминал**: `POST /api/terminal/run` → `check_command` (allow-list, пути, пайпы) → `subprocess` без shell с санитизированным env и resource limits → аудит.
4. **Файлы**: multipart → `upload_security.validate_upload` → временная папка → парсер → AI-анализ → удаление временных файлов.
5. **Поиск**: несколько бэкендов с фолбэком; страницы читаются как недоверенный текст.

## Границы и состояния

- Владелец чата: `anon:<cookie id>`, `admin:<name>`, `email:<google email>`; проверяется сервером.
- Агентные запуски: `queued/running/waiting_confirmation/succeeded/failed/cancelled/expired` (`jobs.py`) и отдельный реестр запусков в `routes/agent.py`.
- Провайдеры: selected provider/model в `runtime_settings.json`; health/failover в `provider_manager.py`.
- Хранилище: SQLite сегодня, PostgreSQL через `DATABASE_URL` + Alembic; S3 — через будущий storage abstraction.

## Расширение

Новый инструмент → JSON-схема в `tool_registry`, ограничения в `agent/policy`, тест, документация. Новый провайдер → `config.PROVIDERS`, `ai_providers`, capabilities в `provider_policy`, тест failover.
