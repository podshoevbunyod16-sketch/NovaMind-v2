# THREAT_MODEL.md — границы доверия и атаки

## Акторы и активы

| Актор | Возможности | Что пытается получить |
|---|---|---|
| Анонимный браузер | HTTP-запросы, загрузка файлов | чужие чаты, секреты сервера, выполнение команд |
| Вошедший пользователь | сессия, рабочая папка, агент | доступ к другим workspace, повышение до админа |
| Администратор | настройки, терминал, MCP | — (но сессия может быть украдена) |
| Внешний сайт / MCP | текст, ссылки, XML/JSON | indirect prompt injection, SSRF |
| Злонамеренный AI-провайдер | ответ модели, tool calls | эксфильтрация ключей через контекст |

Активы: API-ключи, `SESSION_SECRET`, `ADMIN_CODE`, приватные чаты и загрузки, файлы workspace, целостность агента.

## Доверие

1. браузер → Flask: cookie подписана, но данные запроса недоверенны;
2. Flask → database: владелец чата проверяется на сервере (`database.chat_allowed`);
3. Flask → агент: только зарегистрированные инструменты с JSON-схемами и политикой подтверждений (`agent/policy.py`);
4. агент → терминал: allow-list, отдельный workspace, санитизированное окружение, resource limits, аудит; изоляция ОС требует контейнера;
5. агент → внешний контент: обёртка `<untrusted>`, SSRF-проверки, MIME/размер/zip-bomb проверки;
6. агент → модель: system/developer/user/tool разделены, скрытая цепочка рассуждений не выдаётся как «доказательство».

## Основные угрозы и текущие контроли

| Угроза | Контроль | Остаточный риск |
|---|---|---|
| Path traversal в файлах/загрузках | `safe_path`, `normalize_filename`, realpath-проверки | симлинки внутри workspace при включённом терминале |
| Command injection | без `shell=True`, `shlex`, allow-list, запрет `&&`, `$(...)` | сложные выражения в разрешённых интерпретаторах (нужен контейнер) |
| Чтение `.env` / SSH-ключей | env песочницы не наследует секреты, `FORBIDDEN`, проверка путей | пользователь может создать свой `.env` внутри workspace |
| CSRF | Origin/Referer проверка, SameSite=Lax, опциональный double-submit | без Origin (не-браузерные клиенты) защита не требуется |
| XSS | CSP, `escapeHtml` в UI, redaction | inline-обработчики в legacy-шаблонах требуют `CSP_STRICT=1` после миграции |
| Утечка секретов в логи | `observability.redact`, `admin code` больше не печатается | сторонние библиотеки могут логировать собственные данные |
| Prompt injection | `guardrails`, разметка untrusted, подтверждения | не даёт 100% гарантии на уровне модели |
| SSRF | схемы/хосты проверяются в search и MCP | внутренние сети при неверной конфигурации инстансов |
| ZIP-bomb / malicious docx | `upload_security.check_zip_bomb` + лимиты парсинга | exotic-форматы вне allow-list отклоняются |
| Brute force / abuse | `LoginLimiter`, `RateLimiter`, `EndpointRateLimiter` | in-process состояние не общее между нодами |
| Session fixation / logout-all | `auth.rotate_session`, `auth.logout_everywhere` | полная multi-device инвалидация требует `ADMIN_LEGACY_SESSIONS=0` |
| Утечка пользовательского файла из git | `git rm --cached`, `.gitignore` |istory rewrite запрещён условиями задачи |

## Что находится вне зоны доверия

Любая строка из веба, документа или MCP-инструмента может содержать «инструкции». Она должна оставаться данными и не может изменять политики, роль или подтверждения. Всегда проверяйтеocode-блоки и ссылки перед открытием.

## Требования к production

- `APP_ENV=production`, HTTPS, `SESSION_SECRET` ≥ 32 символов, сильный `ADMIN_CODE`;
- контейнер с non-root пользователем,.readonly rootfs, cgroup CPU/RAM/pids, сетевой policy;
- Redis для лимитов и job state, PostgreSQL с бэкапами, S3 с карантином + антивирус;
- централизованные логи с redaction, мониторинг `/readyz` и `/metrics`, Sentry DSN через секрет.
