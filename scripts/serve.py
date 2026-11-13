#!/usr/bin/env python3
"""scripts/serve.py — production-запуск NovaMind без Docker и без shell-магии.

Одна команда на Python:

    python scripts/serve.py

Скрипт сам:

1. читает `.env`/`.env.production` (как это делает приложение);
2. прогоняет те же проверки окружения, что и `production.validate()`;
3. печатает баннер: версия, сборка, окружение, база, модели, квитанции;
4. поднимает gunicorn через его Python-API (`BaseApplication`), а не через
   вызов внешнего процесса.

Полезные режимы:

    python scripts/serve.py --check        # только проверка, без запуска
    python scripts/serve.py --port 8080
    python scripts/serve.py --workers 2 --threads 4
    python scripts/serve.py --reload       # dev-режим авто-перезагрузки

Почему gunicorn, а не `app.run()`: встроенный сервер Flask однопоточный и
не умеет graceful-остановку. gunicorn — обычный Python-пакет из
`requirements.txt`, никакого Docker для него не нужно.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return int(default)


def build_options(host: str | None = None, port: int | None = None,
                  workers: int | None = None, threads: int | None = None,
                  reload: bool | None = None, preload: bool | None = None) -> dict:
    """Собирает опции gunicorn. Отдельная функция — чтобы её можно было тестировать."""
    bind_host = host or os.getenv("BIND_HOST", "0.0.0.0")
    bind_port = int(port or _env_int("PORT", 5000))
    tmp_dir = os.getenv("WEB_WORKER_TMP", "/dev/shm")
    if not os.path.isdir(tmp_dir):
        tmp_dir = None
    return {
        "bind": f"{bind_host}:{bind_port}",
        "workers": max(1, int(workers or _env_int("WEB_CONCURRENCY", 1))),
        "threads": max(1, int(threads or _env_int("WEB_THREADS", 8))),
        "worker_class": os.getenv("WEB_WORKER_CLASS", "gthread"),
        "timeout": _env_int("WEB_TIMEOUT", 180),
        "graceful_timeout": _env_int("WEB_GRACEFUL_TIMEOUT", 30),
        "keepalive": _env_int("WEB_KEEPALIVE", 65),
        "max_requests": _env_int("WEB_MAX_REQUESTS", 2000),
        "max_requests_jitter": _env_int("WEB_MAX_REQUESTS_JITTER", 200),
        "preload_app": bool(preload if preload is not None
                            else os.getenv("WEB_PRELOAD", "0") == "1"),
        "reload": bool(reload if reload is not None
                       else os.getenv("WEB_RELOAD", "0") == "1"),
        "accesslog": os.getenv("WEB_ACCESS_LOG", "-"),
        "errorlog": os.getenv("WEB_ERROR_LOG", "-"),
        "loglevel": os.getenv("WEB_LOG_LEVEL", "info"),
        "forwarded_allow_ips": os.getenv("WEB_FORWARDED_ALLOW_IPS", "*"),
        "proxy_allow_ips": os.getenv("WEB_PROXY_ALLOW_IPS", "*"),
        "worker_tmp_dir": tmp_dir,
    }


def banner(report: dict) -> str:
    """Человеческий баннер старта: сразу видно, что и где запущено."""
    checks = report.get("checks") or []
    critical = [item for item in checks if item["level"] == "critical"]
    warnings = [item for item in checks if item["level"] == "warning"]
    lines = [
        "",
        f"  {report['name']} {report['version']} · {report['codename']}"
        f" · сборка {report['build']}",
        "  " + "─" * 58,
        f"  окружение   {report['env']}"
        f"{'  (production)' if report['production'] else ''}",
        f"  python      {report['python']} · pid {report['pid']}",
        f"  база        {report['db_path']}",
    ]
    try:
        n_models = 0
        import config
        for name in (config.PROVIDERS or {}):
            if name in {"local_demo"}:
                continue
            if name == "openai_compatible":
                engine = getattr(config, "local_engine", {}) or {}
                if str(engine.get("base_url") or "").strip():
                    n_models += 1
                continue
            if config.has_credentials(name):
                n_models += 1
        lines.append(f"  модели      {'настроено провайдеров: ' + str(n_models) if n_models else 'нет ключей — офлайн-режим'}")
    except Exception:  # noqa: BLE001 — баннер не должен мешать старту
        pass
    try:
        import receipts
        stats = receipts.stats()
        lines.append(f"  квитанции   {stats['total']} шт · голова {stats['head_hash'][:12]}…")
    except Exception:  # noqa: BLE001
        pass
    lines.append(f"  фичи        {', '.join(report.get('features') or [])}")
    if critical:
        lines.append(f"  ✗ критических проблем: {len(critical)}")
        for item in critical:
            lines.append(f"      [{item['key']}] {item['message']}")
    elif warnings:
        lines.append(f"  ⚠ предупреждений: {len(warnings)} (подробно: python scripts/preflight.py)")
    else:
        lines.append("  ✓ проверки окружения чистые")
    lines.append("  " + "─" * 58)
    return "\n".join(lines)


def check() -> int:
    """Проверка перед запуском. 0 — можно стартовать, 1 — есть критические."""
    import production
    report = production.report()
    print(banner(report))
    critical = [item for item in report["checks"] if item["level"] == "critical"]
    if critical:
        print("\n  Запуск остановлен: исправьте проблемы выше.\n")
        return 1
    print("\n  Проверка пройдена. Запуск: python scripts/serve.py\n")
    return 0


def serve(options: dict) -> int:
    try:
        from gunicorn.app.base import BaseApplication
    except ImportError:
        print("gunicorn не установлен. Выполните: pip install -r requirements.txt",
              file=sys.stderr)
        return 2

    import production
    from wsgi import application

    class NovaServer(BaseApplication):
        def __init__(self, app, opts):
            self._app = app
            self._opts = opts
            super().__init__()

        def load_config(self):
            for key, value in self._opts.items():
                if value is None or key not in self.cfg.settings:
                    continue
                self.cfg.set(key, value)
            # Мягкая остановка: проба /readyz сразу показывает «не готов».
            self.cfg.set("on_exit", lambda server: setattr(production, "_SHUTTING_DOWN", True))

        def load(self):
            return self._app

    NovaServer(application, options).run()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Production-запуск NovaMind на Python")
    parser.add_argument("--host", help="адрес bind (по умолчанию 0.0.0.0)")
    parser.add_argument("--port", type=int, help="порт (по умолчанию PORT или 5000)")
    parser.add_argument("--workers", type=int, help="процессы (по умолчанию WEB_CONCURRENCY=1)")
    parser.add_argument("--threads", type=int, help="потоки на процесс (по умолчанию WEB_THREADS=8)")
    parser.add_argument("--reload", action="store_true", help="авто-перезагрузка (для разработки)")
    parser.add_argument("--preload", action="store_true", help="предзагрузка приложения в master")
    parser.add_argument("--check", action="store_true", help="только проверить окружение")
    args = parser.parse_args(argv)

    if args.check:
        return check()

    import production
    report = production.report()
    print(banner(report))
    if report["production"] and not report["ok"]:
        # В production критичные проблемы уже остановили импорт приложения,
        # но подстрахуемся, если сюда пришли с NOVA_ENV=production извне.
        print("  Запуск остановлен: критическая проблема окружения.", file=sys.stderr)
        return 1

    options = build_options(host=args.host, port=args.port, workers=args.workers,
                            threads=args.threads, reload=args.reload,
                            preload=args.preload)
    print(f"  gunicorn → http://{options['bind']} · "
          f"{options['workers']}×{options['threads']} "
          f"({options['worker_class']}) · timeout {options['timeout']}s\n")
    try:
        return serve(options)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
