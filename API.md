# API.md — публичные HTTP-интерфейсы

Базовый URL: `http://127.0.0.1:5000`. Формат ошибок: `{"error": "...", "code": "...", "request_id": "..."}` с подходящим HTTP-статусом. Каждый ответ содержит `X-Request-ID`; клиент может передать свой заголовок `X-Request-ID` (8–64 символа) для корреляции.

##.system / observability

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/healthz` | liveness, без обращений к БД |
| GET | `/readyz` | readiness, проверка БД (503 при недоступности) |
| GET | `/metrics` | Prometheus-ready text counters |
| GET | `/api/system/health` | подробная диагностика (AI, search, linux, agent) |
| GET | `/api/ai/capabilities` | реестр моделей: контекст, streaming, tools, vision, configured |

## Чат

| Метод | Путь | Тело/результат |
|---|---|---|
| POST | `/send` | `{message, chat_id?}` → `{reply, chat_id, ...}` |
| POST | `/send_stream` | NDJSON: `stage`, `token`, `reasoning`, `done` |
| GET | `/api/chats` | `{chats: [...]}` только владельца |
| POST | `/api/chats` | `{title}` → `{chat_id, title}` |
| GET/DELETE | `/api/chats/<id>` | история чата/удаление; чужой чат — 404 |
| GET | `/api/chats/<id>/traces` | трассы шагов агента |
| POST | `/api/chats/<id>/switch` | сделать чат текущим |
| GET | `/api/history`, DELETE | история/очистка |

## Агент

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/api/agent/react` | ReAct-запуск, NDJSON-события (`auto_tools: true` включает автоподбор инструментов и MCP-серверов) |
| POST | `/api/agent/stream` | потоковый запуск |
| GET | `/api/agent/status`, `/api/agent/runs`, `/api/agent/runs/<id>` | состояние и история запусков |
| POST | `/api/agent/stop` | `{run_id}` — отмена |
| POST | `/api/agent/confirm` | `{id, approve, always}` — ответ на подтверждение |
| GET/DELETE | `/api/agent/permissions` | правила «разрешить всегда» |
| GET | `/api/usage` | учёт токенов/затрат |

Все вызовы инструментов проходят политику рисков; web/document/MCP output помечается как not trusted. Скрытые chain-of-thought пользователю не выдаются. Событие `selection` содержит выбранные local tools, `mcp_servers` и `mcp_tools`; режим `advisory` или `strict`. Явное `tools: false` отключает инструменты и автоподбор.

## Файлы и медиа

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/api/attachments/analyze` (`/upload_file`, `/upload_image`) | multipart `file`, лимит 25 МБ по умолчанию (`MAX_UPLOAD_BYTES`) |
| GET | `/media/<name>` | сгенерированные файлы; `?download=1` → attachment |
| GET | `/generated_image?file=…` | изображение из media catalog, path traversal → 404 |
| POST | `/api/media/upload`, `/api/media/image`, `/api/media/audio` | медиа пайплайн (требует настроенный провайдер) |
| GET | `/api/media/catalog` | каталог моделей |

## Поиск

`POST /api/search`, `/api/auto_search`, `/api/auto_search_stream`, `/api/web_search_groq`, `GET /api/search/health`, `/api/ai/status`. Результаты содержат `sources` с URL и хостом; страницы считаются недоверенными.

## Сессии и админка

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/api/admin/login`, `/api/admin/logout` | вход по коду, выход |
| POST | `/api/session/login`, `/api/session/logout` | вход по нику, выход из «быстрого» входа |
| GET | `/api/session/check`, `/api/admin/check` | текущая роль |
| POST | `/api/session/rotate` | ротация версии сессии |
| POST | `/api/admin/sessions/logout-all` | отзыв всех админ-сессий (только admin) |
| GET | `/api/csrf-token` | cookie для double-submit режима |

## Терминал (по умолчанию выключен)

`GET /api/terminal/status`, `POST /api/terminal/run`, `/api/terminal/file`, `/api/terminal/bootstrap`, `/api/terminal/git`, `GET /api/terminal/files`, `/api/terminal/help`. Ответы: `{code, stdout, stderr, duration_ms, timed_out, cancelled, command, cwd}`. Each command is audited.

## Ограничения

- Лимиты: `RATE_UPLOAD`/`RATE_TERMINAL`/`RATE_MEDIA`/`RATE_API` (in-process), `AGENT_RATE_*`, `TTS_RATE_*`, `ADMIN_LOGIN_*`.
- CSRF: по умолчанию проверяется браузерный `Origin`; `CSRF_REQUIRE_HEADER=1` включает double-submit.
- Публичные ответы не содержат stack trace; детали только в серверных логах с redaction.
