"""observability.py — request IDs, structured logs, redaction and metrics.

The module is deliberately dependency-free: it can be imported before any AI
provider is configured.  Nothing here logs credentials, cookies or document
contents; :func:`redact` is the last line of defence for third-party messages.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any

from flask import g, jsonify, request

log = logging.getLogger("novamind.observability")

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

# Patterns match assignments and common token shapes.  The replacement never
# contains the captured value.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|"
    r"passwd|authorization|cookie|session[_-]?id)\b\s*[:=]\s*[\"']?([^\s,;\"'}]+)")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}")
_KEY_LITERAL = re.compile(r"\b(?:sk|pk|ghp|gho|ghu|ghs|ghr|xox[baprs]|AIza)[\w-]{12,}")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----.*?-----END (?:[A-Z ]+)?PRIVATE KEY-----",
    re.DOTALL,
)
_SENSITIVE_HEADERS = frozenset({
    "authorization", "cookie", "set-cookie", "x-api-key", "x-agent-token",
    "x-internal-token", "proxy-authorization",
})


def redact(value: Any) -> str:
    """Return a log-safe representation of ``value`` (never raises)."""
    text = str(value)
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _KEY_LITERAL.sub("[REDACTED TOKEN]", text)
    return text


def new_request_id(candidate: str | None = None) -> str:
    """Reuse a well-formed client-supplied ID, otherwise generate one."""
    if candidate and _REQUEST_ID_RE.match(candidate.strip()):
        return candidate.strip()[:64]
    return uuid.uuid4().hex


def request_id() -> str:
    return str(getattr(g, "request_id", "")) or "no-request"


class RedactingFilter(logging.Filter):
    """Scrubs messages and arguments before any handler writes them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class JSONFormatter(logging.Formatter):
    """Structured JSON lines with request/correlation context when available."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for attr in ("request_id", "agent_run_id", "provider", "event"):
            value = getattr(record, attr, None)
            if value:
                payload[attr] = redact(value)
        if record.exc_info and record.exc_info[0]:
            # Type only: no traceback with local variables.
            payload["exception"] = redact(record.exc_info[0].__name__)
        return json.dumps(payload, ensure_ascii=False)


def configure_json_logging() -> bool:
    """Enable redacting JSON logs for every handler (opt-in via LOG_JSON=1)."""
    if os.getenv("LOG_JSON", "0") != "1":
        return False
    formatter = JSONFormatter()
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RedactingFilter())
    return True


class Metrics:
    """Small thread-safe counter registry; Prometheus exposition without deps."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._lock = threading.Lock()

    def inc(self, name: str, value: float = 1, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        self.inc(f"{name}_sum", seconds, **labels)
        self.inc(f"{name}_count", 1, **labels)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {
                name + ("{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}" if labels else ""): value
                for (name, labels), value in self._values.items()
            }

    def prometheus(self) -> str:
        lines = ["# TYPE novamind_counter counter"]
        for key, value in sorted(self.snapshot().items()):
            lines.append(f"novamind_{key.replace('{', '{').replace('}', '}')} {value:g}")
        return "\n".join(lines) + "\n"


metrics = Metrics()


def init_app(app) -> None:
    """Attach request IDs, correlation headers, safe errors and metrics."""
    configure_json_logging()

    @app.before_request
    def _assign_request_id():
        g.request_id = new_request_id(request.headers.get(REQUEST_ID_HEADER))
        g.request_started = time.monotonic()
        return None

    @app.after_request
    def _request_id_header(response):
        response.headers[REQUEST_ID_HEADER] = request_id()
        if hasattr(g, "request_started"):
            metrics.observe("http_request_duration_seconds",
                            time.monotonic() - g.request_started,
                            method=request.method,
                            status=str(response.status_code))
        metrics.inc("http_requests_total", 1, method=request.method,
                    status=str(response.status_code))
        return response

    @app.errorhandler(404)
    @app.errorhandler(405)
    def _not_found(error):
        return _error_response("not_found", "Ресурс не найден", 404 if request.path else 405)

    @app.errorhandler(413)
    def _too_large(error):
        return _error_response("payload_too_large", "Слишком большой запрос", 413)

    @app.errorhandler(429)
    def _too_many(error):
        retry_after = getattr(error, "description", "")
        response = _error_response("rate_limited", "Слишком много запросов", 429)
        if isinstance(retry_after, str) and retry_after.isdigit():
            response.headers["Retry-After"] = retry_after
        return response

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def _server_error(error):
        from werkzeug.exceptions import HTTPException

        status = error.code if isinstance(error, HTTPException) and error.code else 500
        if status >= 500:
            log.exception("Unhandled request error: %s", redact(error))
        metrics.inc("server_errors_total", 1, status=str(status))
        production = os.getenv("APP_ENV", "").lower() in ("production", "prod")
        message = "Внутренняя ошибка сервера" if production or status >= 500 \
            else redact(getattr(error, "description", "Ошибка запроса"))
        return _error_response("internal_error" if status >= 500 else "request_error",
                               message, status)


def _error_response(code: str, message: str, status: int):
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({"error": message, "code": code, "request_id": request_id()}), status
    return message, status


def health_response() -> tuple[Any, int]:
    """Liveness: no DB or provider calls, so it stays up during incidents."""
    return jsonify({"status": "ok", "service": "novamind-v2"}), 200


def ready_response() -> tuple[Any, int]:
    """Readiness: the chat database must be reachable before accepting traffic."""
    try:
        import sqlite3

        from database import DB_PATH
        conn = sqlite3.connect(DB_PATH, timeout=2)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — safe status, no details to client
        log.error("Readiness check failed: %s", redact(exc))
        return jsonify({"status": "not_ready"}), 503
    return jsonify({"status": "ready"}), 200


def metrics_response() -> tuple[Any, int]:
    """Prometheus-ready text exposition, no third-party dependencies."""
    from flask import Response

    return Response(metrics.prometheus(), mimetype="text/plain; version=0.0.4"), 200
