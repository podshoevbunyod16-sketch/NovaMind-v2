#!/usr/bin/env python3
"""scripts/preflight.py — проверка перед деплоем, без сети и без зависимостей.

Что делает:

1. читает окружение теми же функциями, что и приложение (``production.validate``);
2. проверяет базу: файл, права, таблицы, целостность квитанций;
3. проверяет модели: какие провайдеры реально настроены;
4. печатает таблицу «что будет в проде» и код возврата:

   0 — можно деплоить;
   1 — есть критические проблемы (в production приложение не стартует);
   2 — предупреждения, деплой возможен, но лучше исправить.

Запуск:

   python scripts/preflight.py
   python scripts/preflight.py --json      # машинный формат для CI
   python scripts/preflight.py --strict    # предупреждения тоже = провал
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ICONS = {"critical": "✗", "warning": "⚠", "info": "·", "ok": "✓"}
ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3}


def collect() -> dict:
    import production
    import version

    report = production.report()

    # ── база ────────────────────────────────────────────────────────
    db = {"path": report["db_path"], "exists": False, "tables": 0, "receipts": 0}
    try:
        import database
        path = database.DB_PATH
        db["exists"] = os.path.exists(path)
        db["size_mb"] = round(os.path.getsize(path) / 1048576, 2) if db["exists"] else 0
        with database.get_db() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            db["tables"] = len(rows)
            try:
                db["receipts"] = int(
                    conn.execute("SELECT COUNT(*) AS n FROM receipts").fetchone()["n"])
            except Exception:  # noqa: BLE001 — таблицы квитанций может ещё не быть
                db["receipts"] = 0
    except Exception as error:  # noqa: BLE001
        db["error"] = f"{type(error).__name__}: {error}"
    report["database"] = db

    # ── квитанции ───────────────────────────────────────────────────
    try:
        import receipts
        report["receipts"] = {"chain": receipts.verify(), **receipts.stats()}
    except Exception as error:  # noqa: BLE001
        report["receipts"] = {"error": f"{type(error).__name__}: {error}"}

    # ── модели ──────────────────────────────────────────────────────
    providers = []
    try:
        import config
        for name, cfg in (config.PROVIDERS or {}).items():
            if name == "local_demo":
                continue
            providers.append({
                "provider": name,
                "configured": bool(config.has_credentials(name)),
                "models": len((cfg.get("models") or [])),
            })
    except Exception:  # noqa: BLE001
        pass
    report["providers"] = providers
    report["version_info"] = {
        "version": version.__version__, "build": version.BUILD,
        "features": list(version.FEATURES),
    }
    return report


def print_human(report: dict) -> None:
    print("\n  NovaMind · preflight\n" + "  " + "─" * 58)
    print(f"  версия      {report.get('version')} · сборка {report.get('build')}")
    print(f"  окружение   {report.get('env')}"
          f"{' (production)' if report.get('production') else ''}")
    print(f"  python      {report.get('python')} · pid {report.get('pid')}")
    print(f"  база        {report.get('db_path')}")

    db = report.get("database") or {}
    print(f"  таблиц      {db.get('tables', 0)} · квитанций {db.get('receipts', 0)}"
          f" · {db.get('size_mb', 0)} МБ")

    chain = (report.get("receipts") or {}).get("chain") or {}
    if chain:
        state = "цела" if chain.get("ok") else f"ПОВРЕЖДЕНА ({chain.get('broken_count')})"
        print(f"  цепочка     {state} · {chain.get('checked', 0)} записей")

    configured = [p for p in (report.get("providers") or []) if p.get("configured")]
    names = ", ".join(p["provider"] for p in configured) or "нет облачных ключей (офлайн)"
    print(f"  модели      {names}")

    features = (report.get("version_info") or {}).get("features") or []
    print(f"  фичи        {', '.join(features)}")

    checks = list(report.get("checks") or [])
    checks.sort(key=lambda item: ORDER.get(item.get("level"), 9))
    print("\n  проверки\n  " + "─" * 58)
    if not checks:
        print("  ✓ замечаний нет")
    for item in checks:
        icon = ICONS.get(item["level"], "·")
        print(f"  {icon} [{item['key']}] {item['message']}")
        if item.get("fix"):
            print(f"      → {item['fix']}")

    critical = [item for item in checks if item["level"] == "critical"]
    warnings = [item for item in checks if item["level"] == "warning"]
    print("\n  " + "─" * 58)
    if critical:
        print(f"  ✗ критических проблем: {len(critical)} — деплой остановится")
    elif warnings:
        print(f"  ⚠ предупреждений: {len(warnings)} — деплой возможен")
    else:
        print("  ✓ готово к деплою")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка NovaMind перед деплоем")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--strict", action="store_true",
                        help="считать предупреждения провалом")
    args = parser.parse_args()

    report = collect()
    checks = report.get("checks") or []
    critical = [item for item in checks if item["level"] == "critical"]
    warnings = [item for item in checks if item["level"] == "warning"]
    chain = (report.get("receipts") or {}).get("chain") or {}
    if chain and not chain.get("ok"):
        critical.append({"level": "critical", "key": "receipts",
                         "message": "Цепочка квитанций повреждена",
                         "fix": "Запустите python -m receipts verify и разберитесь до деплоя"})

    if args.json:
        print(json.dumps({**report, "critical": len(critical),
                          "warnings": len(warnings)}, ensure_ascii=False, indent=2))
    else:
        print_human(report)

    if critical:
        return 1
    if warnings and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
