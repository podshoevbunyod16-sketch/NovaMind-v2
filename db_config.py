"""db_config.py — database configuration and a PostgreSQL-ready abstraction.

Local development keeps using SQLite through ``database.py``.  Deployments can
set ``DATABASE_URL`` (``postgresql+psycopg://...``) and run Alembic migrations;
the SQLAlchemy engine is imported lazily so the local install stays dependency
free.

Secrets: never place the password in code or logs.  ``safe_url()`` strips it
for diagnostics.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

DEFAULT_SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "novamind_chats.db")


@dataclass(frozen=True)
class DatabaseSettings:
    url: str
    driver: str
    sqlite_path: str | None

    @property
    def is_sqlite(self) -> bool:
        return self.driver.startswith("sqlite")

    def safe_url(self) -> str:
        """Database URL with credentials removed (safe for logs)."""
        parts = urlsplit(self.url)
        if not parts.netloc or "@" not in parts.netloc:
            return self.url
        host = parts.netloc.rsplit("@", 1)[1]
        return urlunsplit((parts.scheme, host, parts.path, "", ""))


def settings() -> DatabaseSettings:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        path = os.getenv("SQLITE_DB_PATH", DEFAULT_SQLITE_PATH)
        return DatabaseSettings(f"sqlite:///{path}", "sqlite", path)
    driver = urlsplit(url).scheme or "unknown"
    if not (driver.startswith("sqlite") or driver.startswith("postgresql")):
        raise ValueError("Поддерживаются только sqlite:// и postgresql:// DATABASE_URL")
    sqlite_path = urlsplit(url).path[1:] if driver.startswith("sqlite") else None
    return DatabaseSettings(url, driver, sqlite_path)


def engine():
    """Create a SQLAlchemy engine for migrations/PostgreSQL deployments."""
    config = settings()
    try:
        from sqlalchemy import create_engine
    except ImportError as exc:  # pragma: no cover - depends on deployment deps
        raise RuntimeError(
            "Для DATABASE_URL на PostgreSQL установите sqlalchemy, alembic и psycopg[binary]") from exc
    return create_engine(config.url, pool_pre_ping=True, future=True)


def diagnostics() -> dict[str, str]:
    """Operator-safe database info for health endpoints."""
    config = settings()
    return {"driver": config.driver, "url": config.safe_url(),
            "mode": "sqlite" if config.is_sqlite else "postgres"}
