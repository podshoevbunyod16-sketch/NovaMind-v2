# DATABASE_MIGRATIONS.md — база данных и миграции

## Текущее состояние

`database.py` использует SQLite и прямой SQL с параметризованными запросами. Путь задаётся `SQLITE_DB_PATH`, по умолчанию `./novamind_chats.db`. Таблицы создаются идемпотентно в `init_db()`; схема включает индексы `idx_messages_chat`, `idx_chats_updated`, `idx_chats_owner` и внешние ключи.

Для production подготовлены `DATABASE_URL=postgresql+psycopg://...` и Alembic. `db_config.py` создаёт SQLAlchemy engine лениво и скрывает пароль в диагностике.

## Первый переход на Alembic (без потери данных)

1. Сделайте бэкап: `sqlite3 novamind_chats.db ".backup backup-$(date +%F).db"`.
2. Убедитесь, что текущая схема совпадает с `migrations/versions/0001_baseline.py`.
3. Пометьте существующую БД как уже мигрированную: `alembic stamp head`.
   **Не** выполняйте `upgrade` поверх существующей SQLite-БД без бэкапа: `CREATE TABLE` конфликтует с таблицами, созданными `init_db()`.
4. Для новой PostgreSQL-БД: `DATABASE_URL=... alembic upgrade head`.
5. Проверка в CI: `alembic upgrade head --sql` (offline SQL, без подключения).

## Правила миграций

- Каждая миграция = отдельный файл в `migrations/versions`, с `downgrade()` (или явный отказ с причиной).
- Не удаляйте колонки и tables в одной миграции с кодом, который их ещё читает: сначала expand, затем миграция данных, затем contract.
- Изменение владельца чата (`owner`) при переходе на реальные аккаунты — отдельная миграция с обратной совместимостью `anon:<sid>`.
- Для PostgreSQL включите `pool_pre_ping=True` (уже в `db_config.engine()`), добавьте retry на transient errors на уровне вызовов.
- Не храните пароли базы в `.env.example`, логах или URL в сообщениях об ошибках.

## Резервное копирование

- SQLite: онлайн-бэкап через `.backup`, крон с проверкой контрольной суммы и хранением копий вне репозитория.
- PostgreSQL: `pg_dump -Fc` + проверка восстановления в staging; point-in-time recovery при критичности.
- Документы и загрузки бэкапятся отдельно (`generated_media/`, `routes/uploads/`, `workspace/`).

## Ограничения

Текущий слой `database.py` не является полноценным repository/SQLAlchemy ORM. Миграция к ORM — отдельный этап с characterization-тестами; она не включена в этот hardening-патч.
