"""
mcp_manager.py — независимый MCP Manager NovaMind.

Единая точка управления MCP-серверами обоих транспортов (локальный STDIO и
удалённый HTTP/SSE): обнаружение инструментов, подключение, проверка
работоспособности (health check), отключение и восстановление соединения.

Сбой одного сервера не влияет на остальные: каждый health check изолирован,
а результаты уже обнаруженных инструментов не теряются при падении одного
из серверов. Логи не содержат токенов и секретов (фильтрует mcp_registry).
"""
from __future__ import annotations

import logging
import time

import mcp_registry as registry

log = logging.getLogger("novamind.mcp.manager")


def list_servers():
    """Все настроенные серверы со статусом (без секретов)."""
    return registry.public_connections()


def connect(server_id):
    """Подключение + проверка: возвращает {connected, tools?, error?}."""
    started = time.time()
    result = registry.test_connection(server_id)
    log.info("mcp_manager connect server=%s ok=%s ms=%s error=%s", server_id,
             result.get("connected"), int((time.time() - started) * 1000),
             result.get("error") or "")
    return result


def disconnect(server_id):
    """Отключение сервера: закрывает STDIO-сессию и снимает флаги."""
    registry._close_stdio_session(server_id)
    registry._set_status(server_id, connected=False, initialized=False)
    registry._TOOL_CACHE.pop(server_id, None)
    log.info("mcp_manager disconnect server=%s", server_id)
    return {"id": server_id, "connected": False}


def reconnect(server_id):
    """Восстановление соединения после сбоя (close + connect + tools)."""
    disconnect(server_id)
    return connect(server_id)


def health_check(server_id):
    """Лёгкая проверка работоспособности: ping либо повторный tools/list."""
    started = time.time()
    try:
        registry.request(server_id, "ping")
        ok, error = True, ""
    except Exception as exc:
        ok, error = False, registry._safe_error(exc)
    if not ok:
        # Попытка восстановления: реинициализация сессии.
        try:
            result = reconnect(server_id)
            ok = bool(result.get("connected"))
            error = "" if ok else str(result.get("error") or error)
        except Exception as exc:
            ok, error = False, registry._safe_error(exc)
    log.info("mcp_manager health server=%s ok=%s ms=%s error=%s", server_id, ok,
             int((time.time() - started) * 1000), error or "")
    return {"id": server_id, "healthy": ok, "error": error,
            "ms": int((time.time() - started) * 1000)}


def health_check_all():
    """Health check всех серверов; сбой одного не роняет остальные."""
    results = []
    for item in registry.public_connections():
        try:
            results.append(health_check(item["id"]))
        except Exception as exc:
            results.append({"id": item.get("id"), "healthy": False,
                            "error": registry._safe_error(exc), "ms": 0})
    return results


def discover_tools():
    """Discover tools independently; retain fresh cached tools for degraded servers."""
    tools = registry.connected_tools()
    alive = {tool.get("server_id") for tool in tools}
    for sid, item in registry._read().items():
        if not isinstance(item, dict) or sid in alive:
            continue
        cached = registry._TOOL_CACHE.get(sid)
        if cached and cached[0] > time.monotonic():
            for tool in cached[1]:
                entry = dict(tool)
                entry["server_id"] = sid
                entry["server_name"] = item.get("name", sid)
                entry["transport"] = item.get("transport", "streamable_http")
                entry["degraded"] = True
                tools.append(entry)
    tools.sort(key=lambda item: (str(item.get("server_id", "")), str(item.get("name", ""))))
    return tools


def server_summaries():
    return registry.server_summaries()


def call_tool(server_id, tool_name, arguments=None):
    """Вызов инструмента с возвратом результата либо изолированной ошибки."""
    try:
        return {"ok": True, "result": registry.call_tool(server_id, tool_name, arguments)}
    except Exception as exc:
        log.warning("mcp_manager call failed server=%s tool=%s error=%s",
                    server_id, tool_name, registry._safe_error(exc))
        return {"ok": False, "error": registry._safe_error(exc)}
