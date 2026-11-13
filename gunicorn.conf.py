"""gunicorn.conf.py — production-конфигурация процесса.

Подобрана под смешанную нагрузку NovaMind: долгие NDJSON-стримы агента
(нужны потоки, а не только процессы), локальный SQLite, память небольших
инстансов. Всё переопределяется переменными окружения.

    gunicorn -c gunicorn.conf.py wsgi:application
"""
import multiprocessing
import os


def _int(name, default):
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return int(default)


#: Воркеры: 1 по умолчанию. SQLite и in-memory лимиты — один писатель;
#: для нескольких воркеров задайте WEB_CONCURRENCY и внешнюю БД/лимитер.
workers = _int("WEB_CONCURRENCY", 1)
threads = _int("WEB_THREADS", 8)
worker_class = os.getenv("WEB_WORKER_CLASS", "gthread")

bind = os.getenv("BIND", f"0.0.0.0:{_int('PORT', 5000)}")
timeout = _int("WEB_TIMEOUT", 180)          # агент бывает долгим
graceful_timeout = _int("WEB_GRACEFUL_TIMEOUT", 30)
keepalive = _int("WEB_KEEPALIVE", 65)       # > прокси, чтобы не рвать стримы

#: Перезапуск воркера после N запросов — лекарство от утечек долгого аптайма.
max_requests = _int("WEB_MAX_REQUESTS", 2000)
max_requests_jitter = _int("WEB_MAX_REQUESTS_JITTER", 200)

#: Предзагрузка экономит память, но инициализация БД/потоков должна быть
#: безопасной до fork. У нас безопасна, поэтому по умолчанию включена.
preload_app = os.getenv("WEB_PRELOAD", "1") == "1"

accesslog = os.getenv("WEB_ACCESS_LOG", "-")
errorlog = os.getenv("WEB_ERROR_LOG", "-")
loglevel = os.getenv("WEB_LOG_LEVEL", "info")
forwarded_allow_ips = os.getenv("WEB_FORWARDED_ALLOW_IPS", "*")
proxy_allow_ips = os.getenv("WEB_PROXY_ALLOW_IPS", "*")
worker_tmp_dir = os.getenv("WEB_WORKER_TMP", "/dev/shm")

#: graceful-остановка: SIGTERM → ждём текущие запросы, потом выходим.
def on_exit(server):  # noqa: D401 — хук gunicorn
    try:
        import production
        production._SHUTTING_DOWN = True
    except Exception:
        pass


def post_fork(server, worker):  # noqa: D401 — хук gunicorn
    """Каждый воркер пишет свой pid и версию — видно в логах платформы."""
    try:
        import os as _os
        import version
        server.log.info("NovaMind %s (build %s) worker=%s pid=%s",
                        version.__version__, version.BUILD, worker.pid, _os.getpid())
    except Exception:
        pass
