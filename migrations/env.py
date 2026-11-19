"""Alembic environment for NovaMind-v2 (SQLite local, PostgreSQL production)."""
from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure the repository root is importable when Alembic is run from anywhere.
from db_config import settings as db_settings

config = context.config
url = db_settings().url.replace("%", "%%")
config.set_main_option("sqlalchemy.url", url)


def run_migrations_offline() -> None:
    context.configure(
        url=db_settings().url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
