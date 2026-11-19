# Changelog

## Unreleased — production hardening

### Security
- Защитил административные API серверной admin-сессией и перестал возвращать конфигурацию провайдеров с credentials; Google ID-токены проверяются через `google-auth`.
- Добавил CSRF-проверку для изменяющих cookie-authenticated запросов: токен связан с сессией, проверяются заголовок, отдельная cookie, Origin и `Sec-Fetch-Site`. Интеграционные маршруты `/run` и `/api/platform/run` используют отдельную проверку токена.
- Защитил Google OAuth одноразовыми state/nonce и PKCE: challenge хранится в SQLite с привязкой к браузерной сессии, истечением срока и атомарным потреблением.
- Закрыл fail-open поведение `/run`: без `AGENT_TOKEN` endpoint недоступен. Секрет admin-кода больше не попадает в логи.
- Ужесточил policy terminal-команд: host-исполнение выключено по умолчанию, неподтверждённые опасные действия блокируются, таймауты ограничены; проверенные операции установки/venv получают окно 60–120 секунд.
- Добавил проверку публичных IP перед web-fetch и каждым redirect, ручную обработку redirect и лимит размера ответа.
- Ограничил загрузки фактическим размером и сигнатурами, запретил SVG, нормализовал имена; добавил долговременную регистрацию владельцев медиа и проверки доступа.
- Добавил владельца для `knowledge_search`, подтверждение Composio через одноразовый challenge, лимиты TTS/media jobs и удаление reasoning из пользовательского потока.
- Усилил базовые browser security headers и проверку URL перед вставкой в frontend.

### Надёжность и тесты
- Добавил атомарное резервирование запланированных задач в SQLite с lease для повторного запуска после сбоя; расширил защиту параллельных media-job polling и очистку устаревших задач.
- Добавил ресурсный wrapper для terminal-процессов и регрессионные тесты admin/auth, Composio, frontend, knowledge scope, медиа, search, runtime и terminal security.
- Тестовый GIF создаётся во временном каталоге; fixture разрешает host-команды только для временной рабочей папки.
- Изолировал тесты от унаследованных provider credentials и OpenAI-compatible endpoint overrides; каждый полный прогон использовал отдельную временную SQLite-базу и coverage-файл.
- Некорректный JSON во встроенной Python CLI возвращает process-shaped ошибку с `code=2`; frontend smoke игнорирует только точное ограничение навигации jsdom и ожидает завершения асинхронного mock-login.
- Добавил `tests/network_isolation/sitecustomize.py` и GitHub Actions workflow для сетево-изолированных pytest/frontend smoke, Bandit high-severity gate, pip-audit и Gitleaks.
- Проверка таймаутов медленных команд выполняется mock-тестом; реальная установка пакетов не запускалась.

### Финальная локальная проверка
- Полный pytest с network guard: **1 158 passed** за **65,12 с**; coverage **82%** (26 262 statements, включая тестовые файлы).
- Frontend smoke пройден полностью на loopback-сервере; раздел админских настроек в этом запуске пропущен из-за отсутствия admin-сессии.
- Ruff baseline gate: **566 → 533** диагностики, 33 устранены, новых нет.
- Bandit: 176 Python-файлов; **0 High, 11 Medium, 2 984 Low**. Это статический отчёт, не доказательство отсутствия уязвимостей.
- `pip-audit -r requirements.txt`: известных уязвимостей не найдено.
- `node --check` для `static/app.js`, `static/csrf.js` и `tests/frontend/smoke.mjs`, разбор YAML workflow (6 jobs) и `git diff --check` пройдены.
- Gitleaks локально недоступен; GitHub Actions workflow здесь не запускался.

### Возможные несовместимости
- `/api/admin/*` требует admin-сессию; `/run` без токена отвечает `503`.
- Composio execute сначала возвращает `409 confirmation_required`; выполнение требует отдельного POST на `/api/composio/confirm` с одноразовым challenge.
- Медиа без зарегистрированного владельца больше не выдаётся. Старые файлы автоматически не удаляются, но перед доступом к ним может потребоваться контролируемое сопоставление владельцев.

Изменения локальны для ветки `agent/production-hardening`. Коммита, push, merge и production deployment не выполнялось; PR #12 не затрагивался.
