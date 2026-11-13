#!/usr/bin/env python3
"""scripts/deploy.py — деплой NovaMind на Python: venv → проверки → бэкап → сервис.

Docker не нужен: всё, что делает скрипт, — обычные шаги Python-разработчика,
собранные в один воспроизводимый сценарий.

    python scripts/deploy.py --check     # только проверить окружение и план
    python scripts/deploy.py             # обновить зависимости, бэкап, рестарт, health
    python scripts/deploy.py --dry-run   # показать команды, ничего не выполняя

Что происходит по шагам:

1. `.venv` создаётся, если его нет (`python -m venv`);
2. `pip install -r requirements.txt` — зависимости в venv, не в систему;
3. `scripts/preflight.py` — критичные проблемы останавливают деплой;
4. `scripts/backup.py` — согласованный снимок базы и квитанций;
5. `systemctl restart <service>` — если прав хватает, иначе печатает команду;
6. `/healthz` и `/readyz` — проверка, что новая версия реально поднялась.

Скрипт никогда не делает `sudo` сам и не трогает секреты.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@dataclass
class Step:
    """Один шаг деплоя: имя, команда (или None) и пояснение."""

    name: str
    command: list[str] | None = None
    note: str = ""
    fatal: bool = True
    kind: str = "command"          # command | health | systemd
    extra: dict = field(default_factory=dict)


def venv_python(venv: str) -> str:
    """Путь к интерпретатору внутри venv (Windows и POSIX)."""
    if os.name == "nt":
        candidate = os.path.join(venv, "Scripts", "python.exe")
    else:
        candidate = os.path.join(venv, "bin", "python")
    return candidate


def build_steps(*, venv: str, service: str, requirements: str,
                backup: bool = True, restart: bool = True,
                health_url: str = "http://127.0.0.1:5000") -> list[Step]:
    """План деплоя. Отдельная чистая функция — её проверяют тесты."""
    python = venv_python(venv)
    steps: list[Step] = []

    if not os.path.exists(python):
        steps.append(Step(
            "Создать venv", [sys.executable, "-m", "venv", venv],
            f"изолированное окружение в {venv}"))
    steps += [
        Step("Обновить pip", [python, "-m", "pip", "install", "-U", "pip", "-q"],
             "свежий установщик"),
        Step("Поставить зависимости", [python, "-m", "pip", "install", "-r", requirements],
             "requirements.txt внутри venv"),
        Step("Проверка окружения", [python, "scripts/preflight.py"],
             "критичные проблемы остановят деплой", fatal=True),
    ]
    if backup:
        steps.append(Step(
            "Резервная копия", [python, "scripts/backup.py", "--keep", "7"],
            "база + экспорт квитанций, 7 последних копий"))
    if restart:
        steps.append(Step(
            "Перезапустить сервис", ["systemctl", "restart", service],
            f"systemctl restart {service}", fatal=False, kind="systemd"))
    steps.append(Step(
        "Проверить здоровье", None,
        f"{health_url}/healthz и /readyz", fatal=True, kind="health",
        extra={"url": health_url}))
    return steps


def _run(command: list[str], dry_run: bool, cwd: str = ROOT) -> int:
    printable = " ".join(command)
    if dry_run:
        print(f"      $ {printable}")
        return 0
    try:
        return subprocess.call(command, cwd=cwd)
    except FileNotFoundError:
        print(f"      ✗ команда не найдена: {command[0]}", file=sys.stderr)
        return 127


def _systemd_active(service: str) -> bool:
    if os.name == "nt" or not shutil.which("systemctl"):
        return False
    try:
        result = subprocess.run(["systemctl", "is-active", service],
                                capture_output=True, text=True, timeout=10)
        return result.stdout.strip() == "active"
    except Exception:  # noqa: BLE001 — нет systemd/прав, это не ошибка деплоя
        return False


def _http_ok(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(400).decode("utf-8", "replace")
            return response.status == 200, body
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}"
    except Exception as error:  # noqa: BLE001
        return False, f"{type(error).__name__}: {error}"


def wait_health(base_url: str, retries: int = 20, delay: float = 1.0) -> bool:
    """Ждёт, пока /healthz и /readyz ответят 200. Печатает прогресс."""
    for attempt in range(1, retries + 1):
        alive, _ = _http_ok(base_url.rstrip("/") + "/healthz")
        ready, detail = _http_ok(base_url.rstrip("/") + "/readyz")
        if alive and ready:
            print(f"      ✓ {base_url}/healthz — жив, /readyz — готов")
            return True
        if attempt == 1 or attempt % 5 == 0:
            print(f"      … попытка {attempt}/{retries}: "
                  f"healthz={'ok' if alive else 'нет'}, readyz={detail[:80]}")
        time.sleep(delay)
    return False


def run(*, venv: str, service: str, requirements: str, backup: bool = True,
        restart: bool = True, health_url: str = "http://127.0.0.1:5000",
        dry_run: bool = False, check_only: bool = False) -> int:
    steps = build_steps(venv=venv, service=service, requirements=requirements,
                        backup=backup, restart=restart, health_url=health_url)
    print("\n  NovaMind · деплой" + (" (проверка плана)" if check_only or dry_run else ""))
    print("  " + "─" * 60)
    for index, step in enumerate(steps, 1):
        print(f"  {index}. {step.name}")
        if step.note:
            print(f"     {step.note}")
        if check_only:
            if step.command:
                print(f"     $ {' '.join(step.command)}")
            continue
        if step.kind == "health":
            if dry_run:
                print("     (пропущено в dry-run)")
                continue
            if not wait_health(step.extra["url"]):
                print("  ✗ сервис не поднялся. Смотрите: journalctl -u "
                      f"{service} -n 80 --no-pager", file=sys.stderr)
                return 4
            continue
        if step.kind == "systemd":
            is_root = hasattr(os, "geteuid") and os.geteuid() == 0
            if not is_root and not dry_run:
                print(f"     ⚠ нет root-прав — выполните вручную: "
                      f"sudo systemctl restart {service}")
                continue
            code = _run(step.command or [], dry_run)
            if code != 0 and step.fatal:
                return 2
            continue
        code = _run(step.command or [], dry_run)
        # preflight: 0 — чисто, 2 — предупреждения, 1 — критично.
        if step.name.startswith("Проверка") and code not in (0, 2):
            print("  ✗ Деплой остановлен: preflight нашёл критические проблемы.",
                  file=sys.stderr)
            return 3
        if code != 0 and step.fatal:
            print(f"  ✗ Шаг «{step.name}» завершился с кодом {code}.", file=sys.stderr)
            return 2
    print("  " + "─" * 60)
    if check_only:
        print("  ✓ План проверен. Запуск без --check выполнит шаги.\n")
    elif dry_run:
        print("  ✓ Dry-run завершён: ничего не выполнено.\n")
    else:
        print("  ✓ Деплой завершён.\n")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Деплой NovaMind на Python (без Docker)")
    parser.add_argument("--venv", default=os.path.join(ROOT, ".venv"),
                        help="путь к виртуальному окружению (по умолчанию ./.venv)")
    parser.add_argument("--service", default=os.getenv("NOVA_SERVICE", "novamind"),
                        help="имя systemd-сервиса")
    parser.add_argument("--requirements", default=os.path.join(ROOT, "requirements.txt"))
    parser.add_argument("--health-url", default=os.getenv("NOVA_HEALTH_URL",
                                                         "http://127.0.0.1:5000"))
    parser.add_argument("--no-backup", action="store_true", help="не делать бэкап")
    parser.add_argument("--no-restart", action="store_true", help="не перезапускать сервис")
    parser.add_argument("--dry-run", action="store_true", help="показать команды без выполнения")
    parser.add_argument("--check", action="store_true", help="только показать план и проверки")
    args = parser.parse_args(argv)

    return run(venv=os.path.abspath(args.venv), service=args.service,
               requirements=os.path.abspath(args.requirements),
               backup=not args.no_backup, restart=not args.no_restart,
               health_url=args.health_url, dry_run=args.dry_run,
               check_only=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
