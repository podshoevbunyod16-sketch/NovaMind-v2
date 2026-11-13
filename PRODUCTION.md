# NovaMind · Production

Runbook для вывода NovaMind в продакшен и для ежедневной эксплуатации.
Всё, что описано ниже, лежит в репозитории и проверяется тестами:
`tests/test_production.py`, `tests/test_receipts.py`, `tests/test_council.py`,
`tests/test_trust_center.py`.

---

## 1. Что даёт версия 3.0

| Блок | Где | Зачем |
|---|---|---|
| Проверка окружения до старта | `production.py` | В `NOVA_ENV=production` приложение **не запускается** с дефолтным админ-кодом, включённым debug или нерабочей папкой БД |
| Пробы | `/healthz`, `/readyz` | Платформа понимает, жив ли процесс и можно ли на него лить трафик |
| Информация о сборке | `/version` | Версия, коммит, фичи, аптайм, все замечания по окружению |
| Метрики | `/metrics` | Prometheus text-format без клиентской библиотеки |
| Безопасные заголовки | `production.install` | `nosniff`, `Referrer-Policy`, `Permissions-Policy`, анти-clickjacking и HSTS в проде |
| Request id и access-лог | `production.install` | Каждый запрос трассируется, ошибка находит свой запрос |
| Проверяемые квитанции | `receipts.py` | Каждый ответ подтверждён хэш-цепочкой и HMAC-подписью |
| Совет моделей | `council.py` | Несколько моделей отвечают независимо, видно согласие и расхождения |
| Trust Center | `/trust` | Живая страница доверия: цепочка, стоимость, готовность, история советов |
| Preflight | `scripts/preflight.py` | Проверка «можно ли деплоить» одной командой |
| Бэкапы | `scripts/backup.py` | Согласованный снимок SQLite + экспорт квитанций |
| Запуск на Python | `scripts/serve.py` | `python scripts/serve.py` — gunicorn через Python-API, без Docker и shell-обёрток |
| Деплой на Python | `scripts/deploy.py` | venv → зависимости → preflight → бэкап → рестарт → health |
| systemd | `scripts/install_service.py`, `deploy/` | Unit, nginx и logrotate под ваш путь и порт |
| CI | `.github/workflows/production.yml` | Компиляция, preflight, тесты подсистем, проверка gunicorn-конфига (Docker — опционально) |

---

## 2. Быстрый старт (Python)

Docker не обязателен. Всё, что нужно, — Python 3.10+ и pip; gunicorn уже есть
в `requirements.txt`. Ниже — основной путь, Docker вынесен в конец как опция.

### 2.1. Один раз на сервере

```bash
git clone <repo> /opt/novamind && cd /opt/novamind

python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

cp .env.production.example .env.production
chmod 600 .env.production
# откройте .env.production и замените ADMIN_CODE / SESSION_SECRET / RECEIPT_SECRET

.venv/bin/python scripts/preflight.py        # что не так с окружением
.venv/bin/python scripts/serve.py --check    # баннер и проверка запуска
```

### 2.2. Запуск

```bash
# обычный запуск в терминале
.venv/bin/python scripts/serve.py

# или с параметрами
.venv/bin/python scripts/serve.py --host 127.0.0.1 --port 5000 --workers 1 --threads 8
```

`scripts/serve.py` — это не обёртка над shell-командой. Он читает `.env`,
прогоняет те же проверки, что и приложение, печатает баннер (версия, база,
модели, квитанции) и поднимает gunicorn через `gunicorn.app.base.BaseApplication`.
`Ctrl+C` — мягкая остановка: текущие стримы доживают, `/readyz` сразу
показывает «не готов».

Проверка:

```bash
curl -s http://127.0.0.1:5000/healthz
curl -s http://127.0.0.1:5000/readyz
curl -s http://127.0.0.1:5000/version
```

### 2.3. systemd: автозапуск и перезапуск

Unit генерируется под ваш путь и пользователя — не редактируйте шаблон руками:

```bash
.venv/bin/python scripts/install_service.py --app-dir /opt/novamind --port 5000
# скрипт покажет готовые команды, выполните их:
sudo cp deploy/novamind.service.generated /etc/systemd/system/novamind.service
sudo systemctl daemon-reload
sudo systemctl enable --now novamind

systemctl status novamind
journalctl -u novamind -f
```

Внутри unit: `User`, `WorkingDirectory`, `EnvironmentFile=.env.production`,
`ExecStart=.venv/bin/python scripts/serve.py`, `Restart=always`,
`KillSignal=SIGTERM`, `TimeoutStopSec=45`, `ReadWritePaths` только на папку
приложения. Если база вынесена (`NOVA_DB_PATH=/data/...`), добавьте `/data`
в `ReadWritePaths` (подсказка есть в комментарии unit-файла).

Готовые шаблоны лежат в `deploy/`:

| Файл | Что это |
|---|---|
| `deploy/novamind.service` | шаблон unit с маркерами `__APP_DIR__`, `__USER__`, `__PORT__` |
| `deploy/nginx.conf` | reverse proxy: TLS, `proxy_buffering off` для NDJSON-стримов, статика |
| `deploy/logrotate.conf` | ротация `*.log`, если пишете не только в journald |

### 2.4. nginx

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/novamind
sudo ln -s /etc/nginx/sites-available/novamind /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Обязательно оставьте `proxy_buffering off`: без него ответы чата и агента
«зависают» до конца генерации. В `.env.production` задайте
`TRUST_PROXY=1` и `SITE_URL=https://ваш-домен`.

### 2.5. Обновление одной командой

```bash
cd /opt/novamind
git pull --ff-only
.venv/bin/python scripts/deploy.py
```

`scripts/deploy.py` по шагам: обновит pip и зависимости в `.venv`, прогонит
preflight (критичные проблемы остановят деплой), сделает бэкап базы и
квитанций, перезапустит `novamind` через systemd (если хватает прав; иначе
напечатает `sudo systemctl restart novamind`) и дождётся `/healthz` + `/readyz`.

Полезные режимы:

```bash
.venv/bin/python scripts/deploy.py --check     # только план и проверки
.venv/bin/python scripts/deploy.py --dry-run   # показать команды, ничего не менять
.venv/bin/python scripts/deploy.py --no-restart --no-backup
```

### 2.6. Render / PaaS

`render.yaml` уже содержит web-сервис. Добавьте переменные из
`.env.production.example` в панели, health-check path — `/healthz`,
start command — `gunicorn -c gunicorn.conf.py wsgi:application`.
Диск подключите в `/data` и задайте `NOVA_DB_PATH=/data/novamind_chats.db`.

### 2.7. Docker (опция, если он вам действительно нужен)

```bash
cp .env.production.example .env.production
python scripts/preflight.py --strict
docker compose up -d --build
docker compose logs -f
```

Данные живут в томе `novamind-data` (`/data`). Этот путь полностью
дублирует Python-вариант: те же `production.py`, `/healthz`, `/readyz`,
`/metrics`. Для локальной разработки на Python Docker не требуется вообще.

---

## 3. Переменные окружения

Полный список — в `.env.example`; ниже то, что важно в проде.

| Переменная | Значение | Зачем |
|---|---|---|
| `NOVA_ENV` | `production` | Строгие проверки и анти-clickjacking |
| `ADMIN_CODE` | случайная строка ≥ 12 символов | Без него код генерируется на каждый старт |
| `SESSION_SECRET` | случайная строка ≥ 32 | Подпись cookie; без него ключ лежит в `.session_secret` |
| `RECEIPT_SECRET` | общий для всех узлов | Подпись квитанций между репликами |
| `SITE_URL` | `https://…` | OAuth-редиректы и проверка HTTPS |
| `TRUST_PROXY` | `1` за прокси | Клиентский IP и схема из `X-Forwarded-*` |
| `NOVA_DB_PATH` | `/data/novamind_chats.db` | База на отдельном томе |
| `METRICS_TOKEN` | секрет | Если задан, `/metrics` требует токен |
| `NOVA_ACCESS_LOG` | `1` | Access-лог (без `/static`) |
| `NOVA_LOG_JSON` | `0/1` | Структурные логи для Loki/CloudWatch |
| `COUNCIL_SEATS` | `3` | Мест в совете моделей |
| `COUNCIL_RATE_GUEST/USER` | `10/60` | Советов в час |
| `RECEIPTS_ENABLED` | `1` | Выключение квитанций (не рекомендуется) |

Сгенерировать секреты:

```bash
openssl rand -base64 24   # ADMIN_CODE
openssl rand -hex 32      # SESSION_SECRET / RECEIPT_SECRET
```

---

## 4. Пробы и наблюдаемость

```bash
curl -s localhost:5000/healthz   # {"ok":true,"status":"alive",...}
curl -s localhost:5000/readyz    # 200/503 + проверки database/storage/receipts
curl -s localhost:5000/version   # версия, сборка, фичи, замечания
curl -s localhost:5000/metrics   # Prometheus
```

`/healthz` — только процесс: не трогает БД и диск, поэтому не может «мигать»
из-за медленного диска. `/readyz` — зависимости: БД, запись в папку,
инициализация квитанций.

Метрики (все — `novamind_*`):

| Метрика | Тип | Смысл |
|---|---|---|
| `novamind_info{version,env,build}` | gauge | Что за сборка |
| `novamind_uptime_seconds` | gauge | Аптайм процесса |
| `novamind_requests_total{class="2xx"}` | counter | Запросы по классам |
| `novamind_request_duration_ms_sum/count` | counter | Средняя латентность |
| `novamind_path_requests_total{path}` | counter | Нагрузка по маршрутам |
| `novamind_receipts_total` | counter | Квитанций в цепочке |
| `novamind_receipts_tokens_total` | counter | Токенов по квитанциям |
| `novamind_receipts_cost_usd_total` | counter | Оценка стоимости |
| `novamind_council_runs_total` | counter | Советов собрано |
| `novamind_db_size_bytes` | gauge | Размер SQLite |
| `novamind_ready` | gauge | 1 = готов |

Пример scrape:

```yaml
scrape_configs:
  - job_name: novamind
    metrics_path: /metrics
    static_configs:
      - targets: ["novamind:5000"]
    # если задан METRICS_TOKEN:
    # authorization: { type: Bearer, credentials: "<token>" }
```

> Важно: счётчики живут в памяти **воркера**. При `WEB_CONCURRENCY > 1`
> Prometheus увидит несколько целей — либо scrape каждого воркера, либо
> оставьте один воркер (значение по умолчанию).

Алерты, которые имеют смысл:

* `up == 0` 2 минуты → сервис лежит;
* `novamind_ready == 0` 5 минут → не готова БД/диск;
* рост `novamind_requests_total{class="5xx"}` → смотрите лог по `request_id`;
* `novamind_db_size_bytes` > 80% тома.

---

## 5. Nova Receipts — проверяемые квитанции

### 5.1. Что внутри

Каждый ответ чата, агента и каждый совет оставляет запись:

```
id, created_at, kind, owner, chat_id, run_id,
provider, model, prompt_versions, tools, sources,
input_sha256, output_sha256, input_chars, output_chars,
tokens, cost_usd, meta, prev_hash, hash, signature
```

```
hash      = SHA-256(canonical(body))
signature = HMAC-SHA256(secret, hash)
prev_hash = hash предыдущей записи
```

Текст ответа **не хранится** в квитанции — только его SHA-256. Квитанция
доказывает факт и подлинность, не размножая содержимое по базе.

### 5.2. Где смотреть

* бейдж **🧾 Квитанция** под ответом в чате;
* кнопка **Совет** рядом — она тоже оставляет квитанцию;
* страница **`/trust`** — вся цепочка, проверка, экспорт;
* API: `GET /api/receipts`, `GET /api/receipts/verify`,
  `GET /api/receipts/<id>`, `GET /api/receipts/export`;
* CLI:

```bash
python -m receipts verify        # проверка всей цепочки (код 1 при поломке)
python -m receipts stats
python -m receipts show 42
python -m receipts export > receipts.jsonl
```

### 5.3. Независимый аудит

```bash
python -m receipts export > audit.jsonl
# на любой машине с тем же RECEIPT_SECRET:
RECEIPT_SECRET=… python -m receipts verify
```

Без секрета проверка подписей провалится — это ожидаемо и правильно:
секрет и есть доказательство авторства.

### 5.4. Ограничения, которые надо знать

* SQLite — один писатель. Цепочка корректна при одном процессе-писателе
  (`WEB_CONCURRENCY=1`, значение по умолчанию). `BEGIN IMMEDIATE`
  сериализует запись между потоками, но не заменяет внешнюю БД в кластере.
* Смена `RECEIPT_SECRET` делает старые подписи невалидными. Меняйте секрет
  вместе с архивированием `receipts.jsonl` и запишите дату ротации.
* Удаление старых записей «для экономии места» ломает цепочку — это не баг,
  а смысл функции. Чистите через бэкап и пересоздание, если действительно надо.
* Владелец видит только свои квитанции; проверка цепочки идёт по всей базе,
  чтобы разрыв нельзя было спрятать.

---

## 6. Совет моделей

```bash
curl -s -X POST localhost:5000/api/council/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"Стоит ли переписывать сервис на async?"}' | python -m json.tool
```

* `COUNCIL_SEATS` — 2–4 места (по умолчанию 3);
* при одном ключе совет работает в трёх ролях: аналитик, критик, практик;
* при нескольких провайдерах места разводятся по разным моделям;
* `COUNCIL_MODELS=groq:openai/gpt-oss-120b,cerebras:zai-glm-4.7` — жёсткий список;
* `COUNCIL_MATCH` — порог сходства предложений (0.5 по умолчанию);
* лимиты: `COUNCIL_RATE_GUEST` / `COUNCIL_RATE_USER`, окно — час.

Стоимость: один совет = до `COUNCIL_SEATS` запросов. На бесплатных тарифах
Groq/Cerebras/Gemini это ноль рублей, но расход квоты реальный — поэтому у
совета отдельный лимит, а не общий с чатом.

Если моделей нет, API вернёт `503` с подсказкой, какие ключи добавить.
Приложение не подставляет офлайн-модель молча.

---

## 7. Бэкапы и восстановление

```bash
python scripts/backup.py --dir /data/backups --keep 7
```

Копия содержит:

```
novamind-YYYYmmdd-HHMMSS/
├── novamind_chats.db      # согласованный снимок sqlite .backup
├── receipts.jsonl         # цепочка для независимой проверки
└── meta.json              # версия, размеры, голова цепочки, вердикт
```

Восстановление:

```bash
systemctl stop novamind
cp /data/backups/novamind-…/novamind_chats.db /data/novamind_chats.db
python -m receipts verify
systemctl start novamind
curl -s localhost:5000/readyz
```

Cron:

```cron
15 3 * * * cd /opt/novamind && .venv/bin/python scripts/backup.py --dir /data/backups --keep 14 >> /var/log/novamind-backup.log 2>&1
```

---

## 8. Чеклист безопасности

- [ ] `NOVA_ENV=production` — иначе проверки не строгие.
- [ ] `ADMIN_CODE` задан, ≥ 12 символов, не из примеров.
- [ ] `SESSION_SECRET` и `RECEIPT_SECRET` заданы и хранятся вне git.
- [ ] HTTPS на прокси, `SITE_URL=https://…`, `TRUST_PROXY=1`.
- [ ] `FLASK_DEBUG=false`.
- [ ] `TERMINAL_ENABLED=0`, если песочница не изолирована в контейнере.
- [ ] `GUARDRAILS_ENABLED=1`.
- [ ] Лимиты `AGENT_RATE_*` и `COUNCIL_RATE_*` соответствуют нагрузке.
- [ ] `METRICS_TOKEN` задан, если `/metrics` доступен из интернета.
- [ ] `.env.production` с правами `600`, владелец — сервисный пользователь.
- [ ] Бэкапы настроены и **проверены восстановлением**.
- [ ] `python scripts/preflight.py --strict` — зелёный.

---

## 9. Масштабирование: где предел

| Ограничение | Сейчас | Что делать при росте |
|---|---|---|
| Писатель SQLite | 1 процесс | Вынести БД на Postgres/MySQL отдельным слоем |
| Лимиты запросов | В памяти процесса | Redis или общий шлюз |
| Квитанции | Локальная цепочка | Один писатель цепочки + архив в объектное хранилище |
| Совет | Потоки воркера | Очередь задач (RQ/Celery) + отдельные воркеры |
| Медиа | Локальная папка | S3-совместимое хранилище |

Практический потолок текущей архитектуры — один узел с 4–8 потоками и
базой до нескольких гигабайт. Для большего нужна внешняя БД, иначе цепочка
квитанций и лимиты теряют смысл.

---

## 10. Обновление и откат

```bash
# обновление (один шаг: зависимости → preflight → бэкап → рестарт → health)
git pull --ff-only
.venv/bin/python scripts/deploy.py

# откат
git checkout <prev-commit>
.venv/bin/python scripts/deploy.py --no-backup
```

Миграции схемы аддитивные (`CREATE TABLE IF NOT EXISTS` + `_ensure_column`),
поэтому откат кода на предыдущую версию не требует отката базы.

---

## 11. Диагностика

| Симптом | Причина | Действие |
|---|---|---|
| Не стартует, в логе «Запуск остановлен» | Критичная production-проверка | `python scripts/preflight.py`, исправьте `key` из сообщения |
| `/readyz` = 503, `database.ok=false` | Нет прав/папки/файла БД | Проверьте `NOVA_DB_PATH` и владельца |
| `/readyz` = 503, `storage=false` | Диск только для чтения или полон | `df -h`, права на `/data` |
| `/metrics` = 403 | Задан `METRICS_TOKEN` | Передайте `X-Metrics-Token` |
| Квитанции «повреждены» | Правка/удаление строк или другой `RECEIPT_SECRET` | `python -m receipts verify`, сверьте секрет и бэкап |
| Совет отвечает 503 | Нет ключей/моделей | Добавьте ключ или `COUNCIL_MODELS` |
| Совет 429 | Лимит совета | `COUNCIL_RATE_*` или подождите окно |
| Стрим чата «зависает» | Прокси буферизует | `proxy_buffering off` |
| Ошибка 500 без текста | Необработанное исключение | Найдите `request_id` в логе и в ответе |
| Высокая латентность агента | Долгие инструменты | `AGENT_TIMEOUT`, `AGENT_MAX_STEPS`, профиль трасс `/api/ops/traces` |

Полезные команды:

```bash
python scripts/preflight.py --json        # машинный отчёт
python scripts/serve.py --check           # баннер + проверка запуска
journalctl -u novamind -n 80 --no-pager   # логи systemd-сервиса
python -m receipts verify                 # целостность цепочки
tail -f /var/log/novamind.log | jq .      # при NOVA_LOG_JSON=1
curl -s localhost:5000/api/ops/health     # подсистемы приложения
```
