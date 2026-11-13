#!/usr/bin/env python3
"""scripts/backup.py — резервная копия данных NovaMind без остановки сервиса.

Что копируется:

* база чатов/агента/квитанций — через ``sqlite3.Connection.backup`` (согласованный
  снимок, не «на живой файл»);
* JSONL-экспорт квитанций — чтобы цепочку можно было проверить независимо;
* метаданные: версия, время, размеры, голова цепочки.

Запуск:

   python scripts/backup.py                 # в ./backups
   python scripts/backup.py --dir /data/backups --keep 7
   python scripts/backup.py --json          # вывод для cron/мониторинга

Код возврата: 0 — успех, 1 — ошибка.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def backup(target_dir: str, keep: int = 0) -> dict:
    import database
    import receipts
    import version

    source = os.path.abspath(database.DB_PATH)
    if not os.path.exists(source):
        raise FileNotFoundError(f"База не найдена: {source}")

    stamp = _timestamp()
    folder = os.path.join(os.path.abspath(target_dir), f"novamind-{stamp}")
    os.makedirs(folder, exist_ok=True)

    db_copy = os.path.join(folder, "novamind_chats.db")
    with sqlite3.connect(source) as src, sqlite3.connect(db_copy) as dst:
        src.backup(dst)

    receipts_file = os.path.join(folder, "receipts.jsonl")
    lines = receipts.export_lines(limit=1000000)
    with open(receipts_file, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + ("\n" if lines else ""))

    meta = {
        "created_at": time.time(),
        "created_label": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": version.__version__,
        "build": version.BUILD,
        "source": source,
        "database_bytes": os.path.getsize(db_copy),
        "receipts": len(lines),
        "chain": receipts.verify(),
    }
    with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)

    removed = []
    if keep > 0:
        base = os.path.abspath(target_dir)
        folders = sorted(
            (os.path.join(base, name) for name in os.listdir(base)
             if name.startswith("novamind-") and os.path.isdir(os.path.join(base, name))),
            key=os.path.getmtime)
        for old in folders[:-keep]:
            shutil.rmtree(old, ignore_errors=True)
            removed.append(os.path.basename(old))

    return {"folder": folder, "meta": meta, "removed": removed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Резервная копия NovaMind")
    parser.add_argument("--dir", default=os.path.join(ROOT, "backups"),
                        help="куда писать (по умолчанию ./backups)")
    parser.add_argument("--keep", type=int, default=0,
                        help="сколько последних копий оставить (0 — все)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = backup(args.dir, args.keep)
    except Exception as error:  # noqa: BLE001
        print(f"❌ Резервная копия не создана: {type(error).__name__}: {error}",
              file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        meta = result["meta"]
        chain = meta["chain"]
        print(f"✓ Копия: {result['folder']}")
        print(f"  база    {meta['database_bytes'] / 1048576:.2f} МБ")
        print(f"  квитанций {meta['receipts']} · цепочка "
              f"{'цела' if chain.get('ok') else 'ПОВРЕЖДЕНА'}")
        if result["removed"]:
            print(f"  удалено старых копий: {len(result['removed'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
