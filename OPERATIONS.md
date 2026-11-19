# OPERATIONS.md — эксплуатация

## Ежедневные проверки

```bash
curl -fsS https://host/healthz      # liveness, не трогает БД и провайдеров
curl -fsS https://host/readyz       # готовность: доступна ли БД
curl -fsS https://host/metrics      # Prometheus-ready счётчики
python scripts/scan_secrets.py      # локальная проверка рабочего дерева
```

`GET /api/system/health` — подробная диагностика провайдера, поиска и агента (не выдавайте наружу без авторизации). В production закрывайте его на прокси или добавьте проверку роли.

## Метрики

`observability.metrics` считает `http_requests_total`, `http_request_duration_seconds_*`, `server_errors_total` с метками `method`/`status`. Добавьте `provider_latency`, `tool_call` и `token_usage` через `metrics.inc/observe` при интеграции с OpenTelemetry.

## Логи

- При `LOG_JSON=1` каждая строка — JSON с `request_id`, `level`, `logger`, `message`. Секреты вырезаются `observability.redact`.
- `novamind.audit` пишет каждую команду терминала (command, code, ms, request_id) без полного вывода.
- `agent/policy.log_action` пишет вызовы инструментов.
- Не логируйте полные документы, cookie и тела запросов с персональными данными.

## Инциденты

| Симптом | Проверка | Действие |
|---|---|---|
| 503 `/readyz` | доступ к БД, диск, размер файла | восстановить БД/диск, перезапустить после успешной проверки |
| Много 429 | `/metrics`, IP/пользователь | увеличить лимиты или подключить Redis; проверить abuse |
| 502 от провайдера | `/api/system/health`, `/api/ai/status` | проверить ключи, включить fallback, не разглашать причину пользователю |
| Подозрительная команда | `novamind.audit` | отозвать сессии (`/api/admin/sessions/logout-all`), проверить workspace |
| Утечка секрета | сканер/логи | немедленно ротировать ключ, не переписывать историю без плана |

## Ротация сессий и ключей

- `POST /api/session/rotate` меняет `auth_version`, сохраняя владельца чатов.
- `POST /api/admin/sessions/logout-all` инвалидирует все админ-сессии с epoch (требует `ADMIN_LEGACY_SESSIONS=0` для старых cookie).
- Ротация `SESSION_SECRET` разлогинит всех; запланируйте окно обслуживания.
- Ротация провайдерских ключей: обновить переменные окружения и перезапустить workers.

## Резервные копии и очистка

- SQLite: `sqlite3 novamind_chats.db ".backup backup-$(date +%F).db"`, хранить вне web-root и вне git.
- `generated_media/`, `routes/uploads/`, `workspace/` — пользовательские данные; бэкапить отдельно по политике конфиденциальности.
- Временные загрузки analysis удаляются в `finally`; при падении процесса очищает ОС.
- Миграции и бэкапы — DATABASE_MIGRATIONS.md.
