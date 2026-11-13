#!/usr/bin/env python3
"""scripts/install_service.py — отрендерить systemd-unit под ваш путь и порт.

    python scripts/install_service.py --app-dir /opt/novamind --port 5000
    sudo python scripts/install_service.py --app-dir /opt/novamind --install

Без `--install` скрипт ничего не меняет в системе: он пишет
`deploy/novamind.service.generated` и печатает команды, которые нужно
выполнить. С `--install` (только root) копирует unit в
`/etc/systemd/system/`, делает `daemon-reload` и `enable --now`.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

try:                       # pwd есть только на POSIX
    import pwd
except ImportError:        # pragma: no cover — Windows
    pwd = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "deploy", "novamind.service")


def _current_user() -> tuple[str, str]:
    try:
        entry = pwd.getpwuid(os.getuid())
        return entry.pw_name, entry.pw_name
    except Exception:  # noqa: BLE001 — Windows или урезанный контейнер
        name = os.getenv("USER") or os.getenv("USERNAME") or "novamind"
        return name, name


def render_unit(*, app_dir: str, user: str, group: str, port: int,
                python: str, template: str | None = None) -> str:
    """Подставляет пути в шаблон. Чистая функция — используется в тестах."""
    with open(template or TEMPLATE, "r", encoding="utf-8") as handle:
        text = handle.read()
    replacements = {
        "__APP_DIR__": os.path.abspath(app_dir),
        "__USER__": user,
        "__GROUP__": group,
        "__PORT__": str(int(port)),
        "__PYTHON__": os.path.abspath(python),
    }
    for marker, value in replacements.items():
        text = text.replace(marker, value)
    return text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="systemd-unit для NovaMind")
    parser.add_argument("--app-dir", default=ROOT, help="папка приложения")
    parser.add_argument("--user", default=None, help="пользователь сервиса")
    parser.add_argument("--group", default=None, help="группа сервиса")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", 5000)))
    parser.add_argument("--python", default=None,
                        help="интерпретатор (по умолчанию .venv/bin/python)")
    parser.add_argument("--service", default="novamind", help="имя unit-файла")
    parser.add_argument("--output", default=None,
                        help="куда сохранить (по умолчанию deploy/<service>.service.generated)")
    parser.add_argument("--install", action="store_true",
                        help="установить в /etc/systemd/system (нужен root)")
    args = parser.parse_args(argv)

    app_dir = os.path.abspath(args.app_dir)
    user, group = _current_user()
    user = args.user or user
    group = args.group or user
    python = args.python or os.path.join(app_dir, ".venv", "bin", "python")
    output = args.output or os.path.join(ROOT, "deploy", f"{args.service}.service.generated")

    if not os.path.exists(python):
        print(f"⚠ Интерпретатор не найден: {python}\n"
              "  Сначала: python -m venv .venv && .venv/bin/pip install -r requirements.txt",
              file=sys.stderr)

    unit = render_unit(app_dir=app_dir, user=user, group=group, port=args.port,
                       python=python)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(unit)
    print(f"✓ Unit записан: {output}")
    print(f"  пользователь {user}:{group} · порт {args.port} · {python}")

    if not args.install:
        print("\nДальше (нужен root):\n"
              f"  sudo cp {output} /etc/systemd/system/{args.service}.service\n"
              "  sudo systemctl daemon-reload\n"
              f"  sudo systemctl enable --now {args.service}\n"
              f"  systemctl status {args.service}\n"
              f"  journalctl -u {args.service} -f\n")
        return 0

    if os.name == "nt" or not shutil.which("systemctl"):
        print("✗ systemd не найден — установка невозможна.", file=sys.stderr)
        return 1
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("✗ Для --install нужен root: sudo python scripts/install_service.py …",
              file=sys.stderr)
        return 1
    target = f"/etc/systemd/system/{args.service}.service"
    shutil.copyfile(output, target)
    for command in (["systemctl", "daemon-reload"],
                    ["systemctl", "enable", "--now", args.service]):
        print("  $", " ".join(command))
        if subprocess.call(command) != 0:
            print(f"✗ Команда не выполнена: {' '.join(command)}", file=sys.stderr)
            return 2
    print(f"✓ Сервис {args.service} установлен и запущен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
