"""server.py — единая точка запуска NovaMind.

Главный сценарий — ровно один и привычный:

    python app.py

Он поднимает production-сервер (gunicorn через его Python-API), печатает
баннер и прогоняет проверки окружения. Docker и отдельные shell-команды не
нужны.

Режимы:

    python app.py                 # production: gunicorn, 1×8 потоков
    python app.py --port 8080     # другой порт
    python app.py --workers 2     # несколько процессов (см. ограничения SQLite)
    python app.py --dev           # Flask dev-сервер: debug + автоперезагрузка
    python app.py --flask         # принудительно встроенный сервер Flask
    python app.py --check         # только проверка окружения, без запуска

Совместимость сохранена: `FLASK_DEBUG=true python app.py` тоже включает
dev-режим, как раньше. `scripts/serve.py` остался тонкой обёрткой над этим
модулем — старые команды и systemd-unit продолжают работать.

Почему gunicorn: встроенный сервер Flask однопоточный и без graceful-stop.
gunicorn — обычный Python-пакет из requirements.txt.

Отдельная тонкость — планировщик. Он живёт в фоновом потоке, а потоки не
переживают fork. Поэтому при запуске через gunicorn планировщик глушится в
master-процессе и поднимается заново в каждом воркере (post_fork).
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Планировщик поднимается уже в воркере (post_fork), а не в master:
# поток, созданный до fork, в дочернем процессе не живёт.
os.environ.setdefault("NOVA_DEFER_SCHEDULER", "1")


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return int(default)


def runtime_mode(dev: bool = False, flask: bool = False,
                 debug_env: str | None = None) -> str:
    """Какой сервер запускать: ``gunicorn`` (по умолчанию) или ``flask``.

    Вынесено в чистую функцию, чтобы поведение проверялось тестами:
    --dev/--flask или FLASK_DEBUG=true → Flask; иначе gunicorn.
    """
    if dev or flask:
        return "flask"
    value = (os.getenv("FLASK_DEBUG", "false") if debug_env is None else debug_env)
    if str(value).strip().lower() in {"1", "true", "yes", "on"}:
        return "flask"
    return "gunicorn"


def build_options(host: str | None = None, port: int | None = None,
                  workers: int | None = None, threads: int | None = None,
                  reload: bool | None = None, preload: bool | None = None) -> dict:
    """Опции gunicorn. Чистая функция — её проверяют тесты."""
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
    """Человеческий баннер старта: видно, что и где запущено."""
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
        import config
        providers = 0
        for name in (config.PROVIDERS or {}):
            if name == "local_demo":
                continue
            if name == "openai_compatible":
                engine = getattr(config, "local_engine", {}) or {}
                if str(engine.get("base_url") or "").strip():
                    providers += 1
                continue
            if config.has_credentials(name):
                providers += 1
        lines.append("  модели      " + (f"настроено провайдеров: {providers}"
                                         if providers else "нет ключей — офлайн-режим"))
    except Exception:  # noqa: BLE001 — баннер не должен мешать старту
        pass
    try:
        import receipts
        stats = receipts.stats()
        lines.append(f"  квитанции   {stats['total']} шт · голова "
                     f"{stats['head_hash'][:12]}…")
    except Exception:  # noqa: BLE001
        pass
    lines.append(f"  фичи        {', '.join(report.get('features') or [])}")
    if critical:
        lines.append(f"  ✗ критических проблем: {len(critical)}")
        for item in critical:
            lines.append(f"      [{item['key']}] {item['message']}")
    elif warnings:
        lines.append(f"  ⚠ предупреждений: {len(warnings)} "
                     "(подробно: python scripts/preflight.py)")
    else:
        lines.append("  ✓ проверки окружения чистые")
    lines.append("  " + "─" * 58)
    return "\n".join(lines)


def check() -> int:
    """Проверка окружения перед запуском. 0 — можно стартовать."""
    import production
    report = production.report()
    print(banner(report))
    if [item for item in report["checks"] if item["level"] == "critical"]:
        print("\n  Запуск остановлен: исправьте проблемы выше.\n")
        return 1
    print("\n  Проверка пройдена. Запуск: python app.py\n")
    sys.stdout.flush()
    return 0


def _start_scheduler(app, why: str) -> None:
    """Поднимает планировщик, если его ещё нет. Ошибки не роняют сервер."""
    try:
        import scheduler
        scheduler.reset_after_fork()
        started = scheduler.start(app)
        if started:
            print(f"  планировщик запущен ({why})")
    except Exception as error:  # noqa: BLE001 — планировщик не критичен для старта
        print(f"  ⚠ планировщик не запустился: {type(error).__name__}: {error}",
              file=sys.stderr)


def _stop_scheduler() -> None:
    """Глушит планировщик в master перед fork — иначе задачи идут дважды."""
    try:
        import scheduler
        scheduler.stop()
    except Exception:  # noqa: BLE001
        pass


def run_flask(app, host: str, port: int, debug: bool = False,
              use_reloader: bool = True) -> int:
    """Встроенный сервер Flask: только для разработки и отладки."""
    sys.stdout.flush()
    _start_scheduler(app, "Flask-режим")
    print(f"  Flask dev-сервер → http://{host}:{port} "
          f"(debug={'on' if debug else 'off'})\n")
    app.run(host=host, port=port, debug=debug, threaded=True,
            use_reloader=use_reloader and debug)
    return 0


def _worker_hook(app):
    """post_fork: в воркере поднимаем планировщик заново (поток не пережил fork)."""
    def post_fork(server, worker):
        try:
            import scheduler
            scheduler.reset_after_fork()
            if scheduler.start(app):
                server.log.info("Планировщик запущен в воркере pid=%s", worker.pid)
        except Exception:
            server.log.exception("Планировщик не запустился в воркере")
    return post_fork


def serve(app, options: dict) -> int:
    """Запуск gunicorn с Python-API. Без Docker, без внешних процессов."""
    # Баннер печатается в stdout, логи gunicorn идут в stderr: сбрасываем
    # буфер, чтобы в journald сначала был баннер, а потом строки сервера.
    sys.stdout.flush()
    host, _, port = options["bind"].rpartition(":")
    try:
        from gunicorn.app.base import BaseApplication
    except ImportError:
        print("  ⚠ gunicorn не установлен — запускаю встроенный сервер Flask "
              "(только для разработки).\n"
              "    Для production: pip install -r requirements.txt",
              file=sys.stderr)
        return run_flask(app, host or "0.0.0.0", int(port or 5000),
                         debug=False, use_reloader=False)

    _stop_scheduler()

    class NovaServer(BaseApplication):
        def __init__(self, application, opts):
            self._application = application
            self._options = opts
            super().__init__()

        def load_config(self):
            for key, value in self._options.items():
                if value is None or key not in self.cfg.settings:
                    continue
                self.cfg.set(key, value)
            self.cfg.set("post_fork", _worker_hook(app))
            self.cfg.set("on_exit",
                         lambda server: setattr(__import__("production"),
                                                "_SHUTTING_DOWN", True))

        def load(self):
            return self._application

    NovaServer(app, options).run()
    return 0


def main(app, argv=None) -> int:
    """`python app.py` — единая точка входа."""
    parser = argparse.ArgumentParser(
        description="NovaMind — запуск (по умолчанию production на gunicorn)")
    parser.add_argument("--host", help="адрес bind (по умолчанию 0.0.0.0)")
    parser.add_argument("--port", type=int, help="порт (по умолчанию PORT или 5000)")
    parser.add_argument("--workers", type=int,
                        help="процессы (по умолчанию WEB_CONCURRENCY=1)")
    parser.add_argument("--threads", type=int,
                        help="потоки на процесс (по умолчанию WEB_THREADS=8)")
    parser.add_argument("--dev", action="store_true",
                        help="Flask dev-сервер: debug и автоперезагрузка")
    parser.add_argument("--flask", action="store_true",
                        help="встроенный сервер Flask вместо gunicorn")
    parser.add_argument("--reload", action="store_true",
                        help="авто-перезагрузка gunicorn (для разработки)")
    parser.add_argument("--preload", action="store_true",
                        help="предзагрузка приложения в master")
    parser.add_argument("--check", action="store_true",
                        help="только проверить окружение и выйти")
    args = parser.parse_args(argv)

    if args.check:
        return check()

    import production
    report = production.report()
    print(banner(report))
    if report["production"] and not report["ok"]:
        print("  Запуск остановлен: критическая проблема окружения.",
              file=sys.stderr)
        return 1

    mode = runtime_mode(dev=args.dev, flask=args.flask)
    host = args.host or os.getenv("BIND_HOST", "0.0.0.0")
    port = int(args.port or _env_int("PORT", 5000))

    if mode == "flask":
        if report["production"] and args.dev:
            print("  ⚠ Flask dev-сервер в NOVA_ENV=production: так делать нельзя, "
                  "используйте python app.py без --dev.", file=sys.stderr)
        debug = bool(args.dev) or str(os.getenv("FLASK_DEBUG", "")).strip().lower() \
            in {"1", "true", "yes", "on"}
        return run_flask(app, host, port, debug=debug)

    options = build_options(host=args.host, port=args.port, workers=args.workers,
                            threads=args.threads, reload=args.reload,
                            preload=args.preload)
    print(f"  gunicorn → http://{options['bind']} · "
          f"{options['workers']}×{options['threads']} "
          f"({options['worker_class']}) · timeout {options['timeout']}s\n")
    try:
        return serve(app, options)
    except KeyboardInterrupt:
        return 0
