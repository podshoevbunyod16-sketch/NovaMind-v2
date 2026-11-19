"""Database settings and background job abstraction checks."""
import os
import time
from pathlib import Path

import pytest

import db_config
import jobs


def test_default_database_settings_are_sqlite(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SQLITE_DB_PATH", raising=False)
    config = db_config.settings()
    assert config.is_sqlite and config.driver == "sqlite"
    assert config.url.startswith("sqlite:///")


def test_postgres_url_is_redacted_in_diagnostics(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:very-secret@db.internal:5432/nova")
    config = db_config.settings()
    assert config.driver.startswith("postgresql")
    assert config.safe_url() == "postgresql+psycopg://db.internal:5432/nova"
    assert "very-secret" not in str(db_config.diagnostics())
    assert db_config.diagnostics()["mode"] == "postgres"


def test_unsupported_database_driver_rejected(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mongodb://localhost/nova")
    with pytest.raises(ValueError):
        db_config.settings()


def test_alembic_baseline_migration_exists():
    path = Path("migrations/versions/0001_baseline.py")
    assert path.exists()
    assert "revision = \"0001_baseline\"" in path.read_text(encoding="utf-8")


def test_job_lifecycle(monkeypatch):
    monkeypatch.setenv("MODEL_TOKEN_BUDGET", "0")
    store = jobs.JobStore()
    job = store.create("web_search", owner="anon:1")
    assert job.status is jobs.JobStatus.QUEUED
    store.set_status(job.id, jobs.JobStatus.RUNNING, progress=0.2)
    assert store.get(job.id, owner="anon:1").status is jobs.JobStatus.RUNNING
    assert store.get(job.id, owner="anon:2") is None
    store.set_status(job.id, jobs.JobStatus.SUCCEEDED, result={"ok": True}, progress=1.0)
    assert store.get(job.id).public()["result"] == {"ok": True}
    # Terminal states are immutable.
    store.set_status(job.id, jobs.JobStatus.FAILED, error="late")
    assert store.get(job.id).status is jobs.JobStatus.SUCCEEDED


def test_job_expiration():
    store = jobs.JobStore()
    job = store.create("agent_run")
    job.created_at = time.time() - 500
    assert store.expire(older_than_seconds=60) == 1
    assert store.get(job.id).status is jobs.JobStatus.EXPIRED


def test_inprocess_executor_completes_work():
    pool = jobs.InProcessExecutor(jobs.JobStore(), workers=2)
    try:
        job = pool.submit("sum", lambda a, b: a + b, 2, 3, owner="anon:1")
        for _ in range(100):
            current = pool.store.get(job.id)
            if current.status in jobs.TERMINAL_STATES:
                break
            time.sleep(0.01)
        assert current.status is jobs.JobStatus.SUCCEEDED
        assert current.result == 5
    finally:
        pool.shutdown()
