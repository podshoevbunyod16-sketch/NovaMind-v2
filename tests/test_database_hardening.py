"""SQLite safety defaults and additive migration regressions."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import database


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    path = tmp_path / "hardening.db"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    database.init_db()
    return path


def test_every_connection_enables_foreign_keys_and_busy_timeout(isolated_database):
    with database.get_db() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("missing-chat", "user", "orphan", 0),
            )


def test_init_db_adds_security_schema_without_losing_legacy_rows(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE chats (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL
            );
            INSERT INTO chats VALUES ('legacy', 'old chat', 1, 2);
            INSERT INTO messages (chat_id, role, content, created_at)
            VALUES ('legacy', 'user', 'preserved', 1);
        """)
    monkeypatch.setattr(database, "DB_PATH", str(path))

    database.init_db()

    with database.get_db() as conn:
        assert conn.execute("SELECT title FROM chats WHERE id='legacy'").fetchone()[0] == "old chat"
        assert conn.execute("SELECT content FROM messages WHERE chat_id='legacy'").fetchone()[0] == "preserved"
        assert "owner" in {row[1] for row in conn.execute("PRAGMA table_info(chats)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"media_file_owners", "oauth_challenges"} <= tables


def test_oauth_challenge_can_only_be_consumed_once_concurrently(isolated_database):
    assert database.create_oauth_challenge(
        "google_gsi_nonce", "one-time-challenge", "browser-id", "one-time-challenge"
    )
    barrier = threading.Barrier(2)

    def consume():
        barrier.wait()
        return database.consume_oauth_challenge(
            "google_gsi_nonce", "one-time-challenge", "browser-id"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: consume(), range(2)))
    assert sum(result is not None for result in results) == 1
