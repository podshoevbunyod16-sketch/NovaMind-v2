"""web_hardening.py — browser-facing protections for the Flask app.

Compatibility notes
-------------------
* Existing endpoints keep working without a CSRF token when the request has no
  browser ``Origin`` header (curl, server-to-server, unit tests).  Real browsers
  send ``Origin`` on POST/PUT/PATCH/DELETE; a cross-site origin is rejected.
  Set ``CSRF_REQUIRE_HEADER=1`` to additionally require the double-submit
  header on every cookie-authenticated mutation.
* The default Content-Security-Policy keeps ``'unsafe-inline'`` because the
  current templates use inline handlers.  ``CSP_STRICT=1`` removes it after the
  templates have been migrated; frame/object/base sources are locked down in
  both modes.
"""
from __future__ import annotations

import logging
import os
import secrets
from urllib.parse import urlparse

from flask import jsonify, request

log = logging.getLogger("novamind.hardening")

CSRF_COOKIE = "nova_csrf"
CSRF_HEADER = "X-CSRF-Token"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_production() -> bool:
    return os.getenv("APP_ENV", os.getenv("FLASK_ENV", "")).lower() in ("production", "prod")


def cookie_secure() -> bool:
    configured = os.getenv("COOKIE_SECURE", "").strip().lower()
    if configured in ("1", "true", "yes"):
        return True
    if configured in ("0", "false", "no"):
        return False
    return urlparse(os.getenv("SITE_URL", "")).scheme == "https" or is_production()


def _csp() -> str:
    inline = " 'unsafe-inline'" if os.getenv("CSP_STRICT", "0") != "1" else ""
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self'{inline} https://accounts.google.com",
        f"style-src 'self'{inline}",
        "img-src 'self' data: blob: https:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "media-src 'self' blob: data:",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ])


def trusted_origins():
    configured = os.getenv("CSRF_TRUSTED_ORIGINS", "")
    values = {item.strip().rstrip("/") for item in configured.split(",") if item.strip()}
    values.add(request.host_url.rstrip("/"))
    site = os.getenv("SITE_URL", "").strip().rstrip("/")
    if site:
        values.add(site)
    return values


def csrf_failure(reason: str):
    log.warning("CSRF rejected: %s", reason)
    return jsonify({"error": "CSRF-проверка не пройдена", "code": "csrf_failed"}), 403


def _check_csrf() -> tuple[bool, str]:
    if request.method not in _UNSAFE_METHODS:
        return True, ""
    # Endpoints called by external automation and server-side jobs do not have
    # browser cookies and therefore cannot be crossed by a browser Origin.
    has_cookies = bool(request.cookies)
    if not has_cookies:
        return True, ""

    origin = (request.headers.get("Origin") or "").strip().rstrip("/")
    referer = (request.headers.get("Referer") or "").strip()
    if origin:
        if origin not in trusted_origins():
            return False, "untrusted origin"
    elif referer:
        parsed = urlparse(referer)
        if f"{parsed.scheme}://{parsed.netloc}" not in trusted_origins():
            return False, "untrusted referer"

    if os.getenv("CSRF_REQUIRE_HEADER", "0") == "1":
        cookie_value = request.cookies.get(CSRF_COOKIE, "")
        header_value = request.headers.get(CSRF_HEADER, "")
        if not cookie_value or not header_value or not secrets.compare_digest(cookie_value, header_value):
            return False, "missing or mismatched double-submit token"
    return True, ""


def init_app(app) -> None:
    """Install cookie policy, security headers and CSRF checks on ``app``."""
    app.config.setdefault(
        "MAX_CONTENT_LENGTH",
        int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024))),
    )
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=os.getenv("SESSION_SAMESITE", "Lax"),
        SESSION_COOKIE_SECURE=cookie_secure(),
        SESSION_REFRESH_EACH_REQUEST=True,
    )

    @app.before_request
    def _csrf_guard():
        ok, reason = _check_csrf()
        if not ok:
            return csrf_failure(reason)
        return None

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("Content-Security-Policy", _csp())
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy",
                                    "camera=(), microphone=(), geolocation=(), payment=()")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
        response.headers.setdefault("Cache-Control", "no-store")
        if request.is_secure or (is_production() and cookie_secure()):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.get("/api/csrf-token")
    def csrf_token():
        """Double-submit cookie for the opt-in strict CSRF mode."""
        token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)
        response = jsonify({"token": token})
        response.set_cookie(CSRF_COOKIE, token, httponly=False, secure=cookie_secure(),
                            samesite="Lax", max_age=86400)
        return response


def validate_production_config() -> list[str]:
    """Fail-fast checklist for production deployments (not run under pytest)."""
    problems: list[str] = []
    if not os.getenv("ADMIN_CODE") or len(os.getenv("ADMIN_CODE", "")) < 12:
        problems.append("ADMIN_CODE must be set to a strong value (>=12 chars)")
    if not os.getenv("SESSION_SECRET") or len(os.getenv("SESSION_SECRET", "")) < 32:
        problems.append("SESSION_SECRET must be set to >=32 chars")
    if not cookie_secure():
        problems.append("COOKIE_SECURE=1 (or an https SITE_URL) is required in production")
    if os.getenv("FLASK_DEBUG", "false").lower() == "true":
        problems.append("FLASK_DEBUG must be false in production")
    if os.getenv("TERMINAL_ENABLED", "0") == "1" and not os.getenv("SANDBOX_CONTAINER"):
        problems.append("TERMINAL_ENABLED requires an isolated container (SANDBOX_CONTAINER=1)")
    return problems
