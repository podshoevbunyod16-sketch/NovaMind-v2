"""Тесты независимого MCP Manager: STDIO-сервер, health check, отключение
одного сервера не ломает остальные, восстановление соединения."""
import json
import sys
import textwrap
import time

import pytest

import mcp_manager
import mcp_registry

STDIO_SERVER = textwrap.dedent("""
    import json, sys
    def send(obj):
        raw = json.dumps(obj).encode()
        sys.stdout.buffer.write(b"Content-Length: " + str(len(raw)).encode() + b"\\r\\n\\r\\n" + raw)
        sys.stdout.buffer.flush()
    for line in sys.stdin.buffer:
        pass
    # frame-based loop
""")


def _write_fake_stdio_server(tmp_path, name="fake_mcp.py"):
    code = textwrap.dedent("""
        import json, sys
        def read_frame():
            headers = {}
            while True:
                line = sys.stdin.buffer.readline()
                if not line or line in (b"\\r\\n", b"\\n"):
                    break
                k, _, v = line.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", "0"))
            return json.loads(sys.stdin.buffer.read(length).decode())
        def send(obj):
            raw = json.dumps(obj).encode()
            sys.stdout.buffer.write(b"Content-Length: " + str(len(raw)).encode() + b"\\r\\n\\r\\n" + raw)
            sys.stdout.buffer.flush()
        while True:
            try:
                msg = read_frame()
            except Exception:
                break
            mid = msg.get("id")
            method = msg.get("method")
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}})
            elif method == "notifications/initialized":
                pass
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                    {"name": "echo", "description": "echo tool", "inputSchema": {"type": "object"}}]}})
            elif method == "tools/call":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "pong"}], "isError": False}})
            elif mid is not None:
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
    """)
    path = tmp_path / name
    path.write_text(code, encoding="utf-8")
    return path


@pytest.fixture
def stdio_server(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_CONNECTIONS_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setattr(mcp_registry, "STORE", tmp_path / "mcp.json")
    script = _write_fake_stdio_server(tmp_path)
    sid = mcp_registry.save_connection({
        "name": "Fake Local", "transport": "stdio",
        "command": [sys.executable, str(script)],
    })
    yield sid
    mcp_registry.remove_connection(sid)


def test_stdio_connect_discover_and_call(stdio_server):
    result = mcp_manager.connect(stdio_server)
    assert result["connected"] is True
    tools = mcp_manager.discover_tools()
    assert any(t["name"] == "echo" and t["server_id"] == stdio_server for t in tools)
    call = mcp_manager.call_tool(stdio_server, "echo", {})
    assert call["ok"] is True


def test_health_check_and_reconnect(stdio_server):
    health = mcp_manager.health_check(stdio_server)
    assert health["healthy"] is True
    mcp_manager.disconnect(stdio_server)
    assert mcp_manager.list_servers()[0]["connected"] is False
    recovered = mcp_manager.reconnect(stdio_server)
    assert recovered["connected"] is True


def test_health_check_all_isolates_dead_server(tmp_path, monkeypatch, stdio_server):
    monkeypatch.setenv("MCP_CONNECTIONS_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setattr(mcp_registry, "STORE", tmp_path / "mcp.json")
    dead = mcp_registry.save_connection({
        "name": "Dead Server", "transport": "stdio",
        "command": ["/nonexistent/binary-xyz"],
    })
    try:
        results = {r["id"]: r for r in mcp_manager.health_check_all()}
        assert results[stdio_server]["healthy"] is True
        assert results[dead]["healthy"] is False
        assert results[dead]["error"]
    finally:
        mcp_registry.remove_connection(dead)


def test_call_tool_error_isolated(stdio_server):
    result = mcp_manager.call_tool(stdio_server, "no_such_tool", {})
    assert result["ok"] is False
    assert "no_such_tool" in result["error"]



def test_discovery_keeps_fresh_cached_tools_for_degraded_server(stdio_server, monkeypatch):
    cached_tool = {
        "name": "echo", "description": "cached echo",
        "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True},
    }
    mcp_registry._TOOL_CACHE[stdio_server] = (time.monotonic() + 60, [cached_tool])
    monkeypatch.setattr(mcp_registry, "connected_tools", lambda: [])

    tools = mcp_manager.discover_tools()

    assert len(tools) == 1
    assert tools[0]["server_id"] == stdio_server
    assert tools[0]["name"] == "echo"
    assert tools[0]["degraded"] is True
