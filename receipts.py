"""receipts.py — Nova Receipts: проверяемые квитанции каждого ответа.

Идея, которой нет у обычных ИИ-агентов: каждый ответ агента оставляет
криптографическую квитанцию. Квитанции связаны в цепочку хэшей, как блоки:
любая правка, удаление или подмена записи ломает цепочку и видна проверкой.

Что доказывает квитанция
------------------------
* кто ответил (провайдер, модель), в каком чате и запуске;
* какие версии промптов и инструменты были активны;
* сколько токенов и денег стоил ход;
* что именно было сказано — через SHA-256 от входа и выхода, без хранения
  самого текста (содержимое остаётся в чате, квитанция только подтверждает
  его подлинность);
* что запись не менялась после создания — HMAC-SHA256 подписью.

Формат (v1)
-----------
    hash      = SHA-256(canonical(body))
    signature = HMAC-SHA256(secret, hash)
    body      = все поля записи + prev_hash (хэш предыдущей квитанции)

Секрет берётся из ``RECEIPT_SECRET``, а если он не задан — выводится из
``SESSION_SECRET``/``.session_secret``. В кластере задайте ``RECEIPT_SECRET``
одинаковым на всех узлах, иначе квитанции другого узла не пройдут проверку.

Зависимостей нет: только stdlib + общий слой БД проекта.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time

import database

log = logging.getLogger("novamind.receipts")

#: Первая запись цепочки указывает на «генезис».
GENESIS = "0" * 64
#: Версия формата — попадёт в каждую квитанцию и в отчёт проверки.
VERSION = 1

#: Поля, которые входят в каноническое тело квитанции.
BODY_FIELDS = (
    "id", "created_at", "kind", "owner", "chat_id", "run_id",
    "provider", "model", "prompt_versions", "tools", "sources",
    "input_sha256", "output_sha256", "input_chars", "output_chars",
    "tokens", "cost_usd", "meta", "prev_hash",
)

_ready: set[str] = set()
_write_lock = threading.Lock()


# ── служебное ────────────────────────────────────────────────────────────
def enabled() -> bool:
    return os.getenv("RECEIPTS_ENABLED", "1").strip() != "0"


def _secret() -> bytes:
    """Секрет подписи: явный из окружения или производный от сессии."""
    explicit = os.getenv("RECEIPT_SECRET", "").strip()
    if explicit:
        return hashlib.sha256(("novamind-receipt|" + explicit).encode("utf-8")).digest()
    try:
        import config
        base = str(getattr(config, "ADMIN_SESSION_KEY", "") or "")
    except Exception:  # noqa: BLE001 — модуль должен работать и вне приложения
        base = ""
    if not base:
        base = os.getenv("SESSION_SECRET", "") or "novamind-local-receipts"
    return hashlib.sha256(("novamind-receipt|" + base).encode("utf-8")).digest()


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(text) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _json_cell(value, default: str = "[]") -> str:
    """Компактный JSON для хранения: списки/словари/строки."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value
    try:
        return _canonical(value)
    except (TypeError, ValueError):
        return default


def init() -> None:
    """Создаёт таблицу квитанций (лениво, один раз на файл БД)."""
    path = str(getattr(database, "DB_PATH", ""))
    if path in _ready:
        return
    with database.get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                kind TEXT NOT NULL DEFAULT 'chat',
                owner TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                prompt_versions TEXT NOT NULL DEFAULT '{}',
                tools TEXT NOT NULL DEFAULT '[]',
                sources TEXT NOT NULL DEFAULT '[]',
                input_sha256 TEXT NOT NULL DEFAULT '',
                output_sha256 TEXT NOT NULL DEFAULT '',
                input_chars INTEGER NOT NULL DEFAULT 0,
                output_chars INTEGER NOT NULL DEFAULT 0,
                tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                meta TEXT NOT NULL DEFAULT '{}',
                prev_hash TEXT NOT NULL DEFAULT '',
                hash TEXT NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_receipts_owner ON receipts(owner, id DESC);
            CREATE INDEX IF NOT EXISTS idx_receipts_chat ON receipts(chat_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_receipts_run ON receipts(run_id);
            """
        )
    _ready.add(path)


def reset_cache() -> None:
    """Сбрасывает кэш «таблица создана» — нужно тестам и сменам пути БД."""
    _ready.clear()


# ── запись ───────────────────────────────────────────────────────────────
def issue(*, kind="chat", chat_id="", owner=None, run_id="", provider="", model="",
          prompt="", output="", tools=None, sources=None, usage=None,
          prompt_versions=None, meta=None) -> dict:
    """Создаёт квитанцию и дописывает её в цепочку.

    Возвращает публичное представление записи (без служебных полей).
    Бросает исключение только при реальной ошибке БД — вызывающий код обязан
    обернуть вызов в ``try``: квитанция не должна ломать ответ пользователю.
    """
    if not enabled():
        return {}
    init()
    if owner is None:
        try:
            import identity
            owner = identity.chat_owner()
        except Exception:  # noqa: BLE001 — вне запроса владельца нет
            owner = ""

    usage = usage if isinstance(usage, dict) else {}
    tokens = int(usage.get("total_tokens") or usage.get("tokens") or 0)
    cost = float(usage.get("cost_usd") or 0.0)
    source_urls = []
    for item in (sources or [])[:20]:
        url = str((item or {}).get("url") or "") if isinstance(item, dict) else str(item or "")
        if url:
            source_urls.append(url[:500])

    now = time.time()
    meta_json = _json_cell(meta, "{}")
    with _write_lock:
        conn = database.get_db()
        conn.isolation_level = None
        try:
            # BEGIN IMMEDIATE сериализует запись между потоками и процессами:
            # иначе два обработчика прочитают один хвост и цепочка разветвится.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT hash FROM receipts ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = str(row["hash"]) if row else GENESIS
            cursor = conn.execute(
                "INSERT INTO receipts (created_at, kind, owner, chat_id, run_id, provider, model,"
                " prompt_versions, tools, sources, input_sha256, output_sha256, input_chars,"
                " output_chars, tokens, cost_usd, meta, prev_hash, hash, signature)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    now, str(kind or "chat")[:40], str(owner or "")[:200], str(chat_id or "")[:80],
                    str(run_id or "")[:80], str(provider or "")[:60], str(model or "")[:120],
                    _json_cell(prompt_versions, "{}"), _json_cell(tools, "[]"),
                    _json_cell(source_urls, "[]"),
                    _digest(prompt), _digest(output),
                    len(str(prompt or "")), len(str(output or "")),
                    max(0, tokens), max(0.0, cost), meta_json, prev_hash,
                    "", "",  # hash/signature заполним ниже по реальному id
                ),
            )
            receipt_id = int(cursor.lastrowid)
            body = {
                "id": receipt_id, "created_at": now, "kind": str(kind or "chat")[:40],
                "owner": str(owner or "")[:200], "chat_id": str(chat_id or "")[:80],
                "run_id": str(run_id or "")[:80], "provider": str(provider or "")[:60],
                "model": str(model or "")[:120],
                "prompt_versions": _json_cell(prompt_versions, "{}"),
                "tools": _json_cell(tools, "[]"), "sources": _json_cell(source_urls, "[]"),
                "input_sha256": _digest(prompt), "output_sha256": _digest(output),
                "input_chars": len(str(prompt or "")), "output_chars": len(str(output or "")),
                "tokens": max(0, tokens), "cost_usd": max(0.0, cost),
                "meta": meta_json, "prev_hash": prev_hash,
            }
            entry_hash = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
            signature = hmac.new(_secret(), entry_hash.encode("ascii"), hashlib.sha256).hexdigest()
            conn.execute("UPDATE receipts SET hash=?, signature=? WHERE id=?",
                         (entry_hash, signature, receipt_id))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 — соединение уже могло закрыться
                pass
            raise
        finally:
            conn.close()

    return {
        "id": receipt_id,
        "kind": body["kind"],
        "created_at": now,
        "hash": entry_hash,
        "prev_hash": prev_hash,
        "provider": body["provider"],
        "model": body["model"],
        "tokens": body["tokens"],
        "cost_usd": round(body["cost_usd"], 6),
        "chat_id": body["chat_id"],
        "run_id": body["run_id"],
        "verify_url": f"/api/receipts/{receipt_id}",
        "algorithm": "sha256+hmac-sha256/v1",
    }


def issue_safe(**kwargs) -> dict:
    """То же, что :func:`issue`, но никогда не бросает исключение."""
    try:
        return issue(**kwargs)
    except Exception:  # noqa: BLE001 — квитанция не должна ломать ответ
        log.warning("Не удалось создать квитанцию", exc_info=True)
        return {}


# ── чтение ───────────────────────────────────────────────────────────────
def _row_body(row) -> dict:
    """Тело записи ровно в том виде, в каком оно подписывалось."""
    return {field: row[field] for field in BODY_FIELDS}


def _row_public(row) -> dict:
    """Публичное представление: без содержимого, только факты и хэши."""
    def parsed(value, fallback):
        try:
            return json.loads(value) if value else fallback
        except (TypeError, ValueError):
            return fallback

    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "created_at": float(row["created_at"]),
        "chat_id": row["chat_id"],
        "run_id": row["run_id"],
        "provider": row["provider"],
        "model": row["model"],
        "prompt_versions": parsed(row["prompt_versions"], {}),
        "tools": parsed(row["tools"], []),
        "sources": parsed(row["sources"], []),
        "input_sha256": row["input_sha256"],
        "output_sha256": row["output_sha256"],
        "input_chars": int(row["input_chars"] or 0),
        "output_chars": int(row["output_chars"] or 0),
        "tokens": int(row["tokens"] or 0),
        "cost_usd": round(float(row["cost_usd"] or 0.0), 6),
        "meta": parsed(row["meta"], {}),
        "prev_hash": row["prev_hash"],
        "hash": row["hash"],
        "signature": row["signature"],
        "algorithm": "sha256+hmac-sha256/v1",
    }


def get(receipt_id) -> dict | None:
    init()
    with database.get_db() as conn:
        row = conn.execute("SELECT * FROM receipts WHERE id=?", (int(receipt_id),)).fetchone()
    return _row_public(row) if row else None


def chain(*, owner=None, chat_id=None, limit=50) -> list[dict]:
    """Последние квитанции владельца (или чата) — в порядке убывания."""
    init()
    query = "SELECT * FROM receipts WHERE 1=1"
    params: list = []
    if owner:
        query += " AND owner=?"
        params.append(str(owner))
    if chat_id:
        query += " AND chat_id=?"
        params.append(str(chat_id))
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(500, int(limit or 50))))
    with database.get_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_row_public(row) for row in rows]


def stats(*, owner=None) -> dict:
    init()
    query = ("SELECT COUNT(*) AS total, COALESCE(SUM(tokens),0) AS tokens,"
             " COALESCE(SUM(cost_usd),0) AS cost, MIN(id) AS first_id,"
             " MAX(id) AS last_id FROM receipts")
    params: tuple = ()
    if owner:
        query += " WHERE owner=?"
        params = (str(owner),)
    with database.get_db() as conn:
        row = conn.execute(query, params).fetchone()
        head = conn.execute("SELECT hash, id FROM receipts ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "total": int(row["total"] or 0),
        "tokens": int(row["tokens"] or 0),
        "cost_usd": round(float(row["cost"] or 0.0), 6),
        "first_id": int(row["first_id"] or 0),
        "last_id": int(row["last_id"] or 0),
        "head_hash": head["hash"] if head else GENESIS,
        "algorithm": "sha256+hmac-sha256/v1",
    }


# ── проверка ─────────────────────────────────────────────────────────────
def verify() -> dict:
    """Проверяет всю цепочку: хэши, подписи и связи между записями.

    Возвращает вердикт: ``ok``, сколько записей проверено, голову цепочки и
    список поломок с причинами. Проверка идёт по всей цепочке, даже если
    пользователь видит только свои квитанции: иначе разрыв можно скрыть.
    """
    init()
    secret = _secret()
    with database.get_db() as conn:
        rows = conn.execute("SELECT * FROM receipts ORDER BY id ASC").fetchall()

    broken: list[dict] = []
    prev = GENESIS
    checked = 0
    for row in rows:
        checked += 1
        reasons = []
        if str(row["prev_hash"] or "") != prev:
            reasons.append("prev_hash")
        expected = hashlib.sha256(_canonical(_row_body(row)).encode("utf-8")).hexdigest()
        if not hmac.compare_digest(str(row["hash"] or ""), expected):
            reasons.append("hash")
        expected_sig = hmac.new(secret, str(row["hash"]).encode("ascii"),
                                hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(row["signature"] or ""), expected_sig):
            reasons.append("signature")
        if reasons:
            broken.append({"id": int(row["id"]), "reasons": reasons})
        prev = str(row["hash"] or "")

    head = rows[-1]["hash"] if rows else GENESIS
    return {
        "ok": not broken,
        "checked": checked,
        "broken": broken[:50],
        "broken_count": len(broken),
        "head_hash": head,
        "algorithm": "sha256+hmac-sha256/v1",
        "version": VERSION,
        "verified_at": time.time(),
    }


def verify_one(receipt_id) -> dict:
    """Проверяет одну квитанцию и её связь с предыдущей записью."""
    init()
    secret = _secret()
    try:
        rid = int(receipt_id)
    except (TypeError, ValueError):
        return {"ok": False, "id": None, "reasons": ["id"]}
    with database.get_db() as conn:
        row = conn.execute("SELECT * FROM receipts WHERE id=?", (rid,)).fetchone()
        if not row:
            return {"ok": False, "id": rid, "reasons": ["missing"]}
        before = conn.execute("SELECT hash FROM receipts WHERE id<? ORDER BY id DESC LIMIT 1",
                              (rid,)).fetchone()
        after = conn.execute("SELECT prev_hash FROM receipts WHERE id>? ORDER BY id ASC LIMIT 1",
                             (rid,)).fetchone()
    reasons = []
    expected_prev = str(before["hash"]) if before else GENESIS
    if str(row["prev_hash"] or "") != expected_prev:
        reasons.append("prev_hash")
    expected_hash = hashlib.sha256(_canonical(_row_body(row)).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(str(row["hash"] or ""), expected_hash):
        reasons.append("hash")
    expected_sig = hmac.new(secret, str(row["hash"]).encode("ascii"),
                            hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(row["signature"] or ""), expected_sig):
        reasons.append("signature")
    if after is not None and str(after["prev_hash"] or "") != str(row["hash"] or ""):
        reasons.append("next_link")
    return {
        "ok": not reasons,
        "id": rid,
        "reasons": reasons,
        "algorithm": "sha256+hmac-sha256/v1",
        "checked_at": time.time(),
    }


def export_lines(*, owner=None, chat_id=None, limit=5000) -> list[str]:
    """JSONL-экспорт для независимого аудита (по одному объекту в строке)."""
    init()
    query = "SELECT * FROM receipts WHERE 1=1"
    params: list = []
    if owner:
        query += " AND owner=?"
        params.append(str(owner))
    if chat_id:
        query += " AND chat_id=?"
        params.append(str(chat_id))
    query += " ORDER BY id ASC LIMIT ?"
    params.append(max(1, min(100000, int(limit or 5000))))
    with database.get_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_canonical(_row_public(row)) for row in rows]


# ── CLI: python -m receipts [verify|stats|show <id>|export] ──────────────
def _cli(argv=None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    command = (args[0] if args else "verify").lower()
    if command == "verify":
        report = verify()
        if report["ok"]:
            print(f"✅ Цепочка цела: {report['checked']} квитанций, "
                  f"голова {report['head_hash'][:16]}…")
            return 0
        print(f"❌ Цепочка повреждена: {report['broken_count']} из {report['checked']}")
        for item in report["broken"]:
            print(f"   #{item['id']}: {', '.join(item['reasons'])}")
        return 1
    if command == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
        return 0
    if command == "show" and len(args) > 1:
        entry = get(args[1])
        if not entry:
            print("Квитанция не найдена", file=sys.stderr)
            return 2
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        return 0
    if command == "export":
        for line in export_lines():
            print(line)
        return 0
    print("Использование: python -m receipts [verify|stats|show <id>|export]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
