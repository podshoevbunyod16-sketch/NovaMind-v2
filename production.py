"""production.py — production-обвязка NovaMind.

Здесь всё, что превращает «работает у меня» в «работает под нагрузкой и
объясняет своё состояние»:

* ``validate()`` — проверка окружения до старта. В ``NOVA_ENV=production``
  критичные проблемы (дефолтный админ-код, debug, невзламываемые секреты)
  останавливают запуск, а не тихо живут в проде;
* ``install(app)`` — доверенный прокси, security-заголовки, request id,
  access-лог, обработчики ошибок;
* ``/healthz`` — liveness (процесс жив, ничего не трогает);
* ``/readyz`` — readiness (БД, диск, квитанции), 503 при неготовности;
* ``/version`` — версия, сборка, фичи, аптайм;
* ``/metrics`` — Prometheus-текст без зависимостей.

Всё в одном модуле, потому что это одна ответственность: эксплуатация.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
import time
import uuid

from flask import Response, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import version

log = logging.getLogger("novamind.production")

STARTED_AT = time.time()
REQUESTS = {
    "total": 0,
    "by_status": {},      # "2xx": 123
    "by_path": {},        # "/send_stream": 12
    "latency_ms": 0.0,
    "inflight": 0,
}
_LOCK = threading.Lock()
_SHUTTING_DOWN = False


# ── окружение ────────────────────────────────────────────────────────────
def env_name() -> str:
    value = (os.getenv("NOVA_ENV") or os.getenv("FLASK_ENV") or "development").strip().lower()
    return value or "development"


def is_production() -> bool:
    return env_name() in {"production", "prod"}


def uptime_seconds() -> float:
    return round(time.time() - STARTED_AT, 1)


def _python_version() -> str:
    return "%d.%d.%d" % sys.version_info[:3]


def _db_path() -> str:
    try:
        import database
        return str(database.DB_PATH)
    except Exception:  # noqa: BLE001 — проверка не должна падать
        return ""


def _issue(level, key, message, fix=""):
    return {"level": level, "key": key, "message": message, "fix": fix}


def validate() -> list[dict]:
    """Проверяет окружение и возвращает список замечаний.

    ``critical`` — в production останавливает запуск, ``warning`` — печатается
    в лог, ``info`` — подсказка для полного счастья.
    """
    issues: list[dict] = []
    prod = is_production()

    admin_code = (os.getenv("ADMIN_CODE") or "").strip()
    if not admin_code:
        issues.append(_issue(
            "critical" if prod else "warning", "admin_code",
            "ADMIN_CODE не задан: код администратора генерируется заново при каждом старте.",
            "Задайте ADMIN_CODE в .env (минимум 12 символов)."))
    elif len(admin_code) < 10:
        issues.append(_issue(
            "warning", "admin_code_weak",
            "ADMIN_CODE короче 10 символов — его легко подобрать.",
            "Используйте код от 12 символов или `openssl rand -base64 24`."))

    if not (os.getenv("SESSION_SECRET") or "").strip():
        issues.append(_issue(
            "warning", "session_secret",
            "SESSION_SECRET не задан — ключ подписи читается из .session_secret.",
            "В кластере задайте общий SESSION_SECRET, иначе сессии не переживут узел."))

    if str(os.getenv("FLASK_DEBUG", "")).lower() in {"1", "true", "yes"}:
        issues.append(_issue(
            "critical" if prod else "warning", "debug",
            "FLASK_DEBUG включён: отладочный сервер недопустим в production.",
            "FLASK_DEBUG=false и запуск через gunicorn/wsgi."))

    site = (os.getenv("SITE_URL") or "").strip()
    if prod and site.startswith("http://"):
        issues.append(_issue(
            "warning", "https",
            "SITE_URL использует http:// — cookies и OAuth без TLS небезопасны.",
            "Настройте HTTPS на прокси и SITE_URL=https://…"))

    if prod and os.getenv("TRUST_PROXY", "0") != "1":
        issues.append(_issue(
            "warning", "trust_proxy",
            "TRUST_PROXY выключен: IP клиента и схема из X-Forwarded-* игнорируются.",
            "TRUST_PROXY=1, если приложение стоит за nginx/Render/Cloudflare."))

    try:
        import config
        configured = [name for name in (config.PROVIDERS or {})
                      if name not in {"local_demo", "openai_compatible"}
                      and config.has_credentials(name)]
        if not configured:
            issues.append(_issue(
                "warning", "providers",
                "Ни одного облачного ключа не настроено — чат работает офлайн-моделью.",
                "Добавьте бесплатный GROQ_API_KEY/GEMINI_API_KEY или локальный сервер."))
    except Exception:  # noqa: BLE001 — проверка не должна падать
        configured = []

    if not (os.getenv("RECEIPT_SECRET") or "").strip():
        issues.append(_issue(
            "info", "receipt_secret",
            "RECEIPT_SECRET не задан — подпись квитанций выводится из секрета сессии.",
            "Для аудита нескольких узлов задайте общий RECEIPT_SECRET."))

    if os.getenv("RECEIPTS_ENABLED", "1") == "0":
        issues.append(_issue(
            "warning", "receipts_off",
            "Nova Receipts выключены: ответы больше не подтверждаются квитанциями.",
            "RECEIPTS_ENABLED=1 — это фирменная функция продукта."))

    db_path = _db_path()
    if db_path:
        directory = os.path.dirname(os.path.abspath(db_path)) or "."
        if not os.path.isdir(directory):
            issues.append(_issue("critical", "db_dir",
                                 f"Папка базы данных не существует: {directory}",
                                 "Создайте папку или задайте NOVA_DB_PATH."))
        elif not os.access(directory, os.W_OK):
            issues.append(_issue("critical", "db_write",
                                 f"Нет прав на запись в папку базы: {directory}",
                                 "Исправьте права или NOVA_DB_PATH."))
        else:
            try:
                free_mb = shutil.disk_usage(directory).free / (1024 * 1024)
                if free_mb < 200:
                    issues.append(_issue(
                        "warning", "disk",
                        f"Свободно {free_mb:.0f} МБ — меньше 200 МБ.",
                        "Очистите логи/медиа или увеличьте том."))
            except OSError:
                pass

    if prod and os.getenv("TERMINAL_ENABLED", "0") == "1":
        issues.append(_issue(
            "info", "sandbox",
            "Терминал включён в production: песочница должна быть изолирована "
            "(отдельный пользователь, контейнер, без секретов в окружении).",
            "Проверьте TERMINAL_COMMANDS/лимиты и не давайте гостям доступ к файлам."))

    try:
        if sys.version_info < (3, 10):
            issues.append(_issue("warning", "python",
                                 f"Python {_python_version()} старше 3.10.",
                                 "Обновите интерпретатор до 3.11+."))
    except Exception:  # noqa: BLE001
        pass

    return issues


def critical_issues() -> list[dict]:
    return [item for item in validate() if item["level"] == "critical"]


def enforce_startup() -> list[dict]:
    """Печатает замечания; в production критические останавливают запуск."""
    issues = validate()
    for item in issues:
        message = f"[{item['level']}] {item['key']}: {item['message']}"
        if item["level"] == "critical":
            log.error(message)
        elif item["level"] == "warning":
            log.warning(message)
        else:
            log.info(message)
    critical = [item for item in issues if item["level"] == "critical"]
    if critical and is_production():
        details = "; ".join(f"{item['key']}: {item['message']}" for item in critical)
        raise RuntimeError(
            "Запуск остановлен: production-проверка нашла критические проблемы. " + details)
    return issues


def report() -> dict:
    """Полный отчёт о среде — для /version, preflight и логов старта."""
    issues = validate()
    return {
        "name": version.NAME,
        "codename": version.CODENAME,
        "version": version.__version__,
        "build": version.BUILD,
        "features": list(version.FEATURES),
        "env": env_name(),
        "production": is_production(),
        "python": _python_version(),
        "pid": os.getpid(),
        "started_at": STARTED_AT,
        "uptime_s": uptime_seconds(),
        "db_path": _db_path(),
        "checks": issues,
        "ok": not [item for item in issues if item["level"] == "critical"],
    }


def _headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    response.headers.setdefault("X-Request-Id", getattr(request, "nova_request_id", ""))
    if is_production():
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Content-Security-Policy", "object-src 'none'; base-uri 'self'; frame-ancestors 'self'")
        if (request.headers.get("X-Forwarded-Proto") or request.scheme) == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def _record(status_code: int, duration_ms: float, path: str) -> None:
    with _LOCK:
        REQUESTS["total"] += 1
        REQUESTS["latency_ms"] += duration_ms
        bucket = f"{status_code // 100}xx"
        REQUESTS["by_status"][bucket] = REQUESTS["by_status"].get(bucket, 0) + 1
        if path:
            REQUESTS["by_path"][path] = REQUESTS["by_path"].get(path, 0) + 1


def _wants_json() -> bool:
    if request.path.startswith("/api/") or request.path in {"/send", "/send_stream", "/run"}:
        return True
    best = request.accept_mimetypes.best
    return best == "application/json"


def _error_payload(code: int, message: str):
    payload = {
        "ok": False,
        "error": message,
        "status": code,
        "request_id": getattr(request, "nova_request_id", ""),
    }
    if _wants_json():
        return jsonify(payload), code
    return Response(
        "<!doctype html><meta charset='utf-8'><title>NovaMind · %(code)s</title>"
        "<style>body{font:16px/1.5 system-ui;background:#070713;color:#e8e8f2;"
        "display:grid;place-items:center;height:100vh;margin:0}"
        "a{color:#8ab4ff}</style><div style='text-align:center'>"
        "<h1 style='font-size:64px;margin:0'>%(code)s</h1>"
        "<p>%(message)s</p><p><a href='/chat'>Вернуться в чат</a></p>"
        "<p style='opacity:.45;font-size:13px'>request %(rid)s</p></div>"
        % {"code": code, "message": message, "rid": payload["request_id"]},
        status=code,
        mimetype="text/html",
    )


# ── пробы ────────────────────────────────────────────────────────────────
def readiness() -> tuple[dict, int]:
    checks: dict[str, dict] = {}
    ready = True

    db_ok, db_detail = False, ""
    try:
        import database
        with database.get_db() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception as error:  # noqa: BLE001
        db_detail = f"{type(error).__name__}: {error}"[:200]
        ready = False
    checks["database"] = {"ok": db_ok, "detail": db_detail or _db_path()}

    disk_ok, disk_detail = False, ""
    directory = os.path.dirname(os.path.abspath(_db_path() or ".")) or "."
    try:
        probe = os.path.join(directory, f".nova-ready-{uuid.uuid4().hex[:8]}")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        disk_ok = True
        disk_detail = directory
    except Exception as error:  # noqa: BLE001
        disk_detail = f"{type(error).__name__}: {error}"[:200]
        ready = False
    checks["storage"] = {"ok": disk_ok, "detail": disk_detail}

    try:
        import receipts
        stats = receipts.stats()
        checks["receipts"] = {"ok": True, "total": stats["total"],
                              "head": stats["head_hash"][:16]}
    except Exception as error:  # noqa: BLE001 — квитанции не блокируют готовность
        checks["receipts"] = {"ok": False, "detail": f"{type(error).__name__}"}

    payload = {
        "ok": ready,
        "ready": ready,
        "shutting_down": _SHUTTING_DOWN,
        "uptime_s": uptime_seconds(),
        "version": version.__version__,
        "checks": checks,
    }
    return payload, (200 if ready else 503)


def metrics_text() -> str:
    """Prometheus text-format без клиентской библиотеки."""
    with _LOCK:
        total = REQUESTS["total"]
        latency = REQUESTS["latency_ms"]
        by_status = dict(REQUESTS["by_status"])
        by_path = dict(REQUESTS["by_path"])

    lines = [
        "# HELP novamind_info Build information.",
        "# TYPE novamind_info gauge",
        f'novamind_info{{version="{version.__version__}",env="{env_name()}",'
        f'build="{version.BUILD}"}} 1',
        "# HELP novamind_uptime_seconds Seconds since process start.",
        "# TYPE novamind_uptime_seconds gauge",
        f"novamind_uptime_seconds {uptime_seconds()}",
        "# HELP novamind_requests_total HTTP requests handled.",
        "# TYPE novamind_requests_total counter",
        f'novamind_requests_total{{class="all"}} {total}',
    ]
    for bucket, count in sorted(by_status.items()):
        lines.append(f'novamind_requests_total{{class="{bucket}"}} {count}')
    lines += [
        "# HELP novamind_request_duration_ms_sum Total request time.",
        "# TYPE novamind_request_duration_ms_sum counter",
        f"novamind_request_duration_ms_sum {round(latency, 2)}",
        "# HELP novamind_request_duration_ms_count Requests measured.",
        "# TYPE novamind_request_duration_ms_count counter",
        f"novamind_request_duration_ms_count {total}",
    ]
    for path, count in sorted(by_path.items())[:40]:
        safe = path.replace('"', "")
        lines.append(f'novamind_path_requests_total{{path="{safe}"}} {count}')

    try:
        import receipts
        stats = receipts.stats()
        lines += [
            "# HELP novamind_receipts_total Verifiable receipts in the chain.",
            "# TYPE novamind_receipts_total counter",
            f"novamind_receipts_total {stats['total']}",
            "novamind_receipts_tokens_total %d" % stats["tokens"],
            "novamind_receipts_cost_usd_total %.6f" % stats["cost_usd"],
        ]
    except Exception:  # noqa: BLE001
        pass

    try:
        import council
        _init = getattr(council, "_init_db", None)
        if callable(_init):
            _init()
        import database
        with database.get_db() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM council_runs").fetchone()
        lines += [
            "# HELP novamind_council_runs_total Councils run.",
            "# TYPE novamind_council_runs_total counter",
            f"novamind_council_runs_total {int(row['n'] or 0)}",
        ]
    except Exception:  # noqa: BLE001
        pass

    try:
        path = _db_path()
        if path and os.path.exists(path):
            lines += [
                "# HELP novamind_db_size_bytes SQLite database size.",
                "# TYPE novamind_db_size_bytes gauge",
                f"novamind_db_size_bytes {os.path.getsize(path)}",
            ]
    except OSError:
        pass

    ready, _ = readiness()
    lines += [
        "# HELP novamind_ready Readiness (1 = ready).",
        "# TYPE novamind_ready gauge",
        f"novamind_ready {1 if ready.get('ready') else 0}",
    ]
    return "\n".join(lines) + "\n"


# ── установка в приложение ───────────────────────────────────────────────
def install(app) -> None:
    """Подключает всю production-обвязку к Flask-приложению (идемпотентно)."""
    if app.config.get("NOVA_PRODUCTION_INSTALLED"):
        return
    app.config["NOVA_PRODUCTION_INSTALLED"] = True

    if os.getenv("TRUST_PROXY", "0") == "1":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    @app.before_request
    def _nova_before():
        request.nova_request_id = (request.headers.get("X-Request-Id")
                                   or uuid.uuid4().hex[:16])
        request.nova_started = time.perf_counter()
        with _LOCK:
            REQUESTS["inflight"] += 1

    @app.after_request
    def _nova_after(response):
        duration = (time.perf_counter() - getattr(request, "nova_started", time.perf_counter())) * 1000
        _record(response.status_code, duration, request.path)
        with _LOCK:
            REQUESTS["inflight"] = max(0, REQUESTS["inflight"] - 1)
        response.headers.setdefault("X-Response-Time-Ms", f"{duration:.1f}")
        _headers(response)
        if os.getenv("NOVA_ACCESS_LOG", "1") == "1" and not request.path.startswith("/static/"):
            if os.getenv("NOVA_LOG_JSON", "0") == "1":
                log.info(json.dumps({
                    "event": "request", "method": request.method, "path": request.path,
                    "status": response.status_code, "duration_ms": round(duration, 1),
                    "request_id": getattr(request, "nova_request_id", ""),
                    "ip": request.remote_addr,
                }, ensure_ascii=False))
        return response

    @app.errorhandler(HTTPException)
    def _http_error(error):
        return _error_payload(error.code or 500, error.description or "Ошибка")

    @app.errorhandler(Exception)
    def _unhandled(error):
        log.exception("Необработанная ошибка", extra={"request_id":
                                                      getattr(request, "nova_request_id", "")})
        return _error_payload(500, "Внутренняя ошибка сервера")

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "status": "alive", "uptime_s": uptime_seconds(),
                        "version": version.__version__})

    @app.get("/readyz")
    def readyz():
        payload, status = readiness()
        return jsonify(payload), status

    @app.get("/version")
    def version_view():
        return jsonify(report())

    @app.get("/metrics")
    def metrics_view():
        token = (os.getenv("METRICS_TOKEN") or "").strip()
        if token:
            supplied = (request.headers.get("X-Metrics-Token")
                        or request.args.get("token") or "")
            if supplied != token:
                return jsonify({"ok": False, "error": "forbidden"}), 403
        return Response(metrics_text(), mimetype="text/plain; version=0.0.4")
