"""Интеграционный тест: основной ReAct-агент реально обнаруживает и вызывает
MCP-инструмент через MCP Manager (а не обходя его прямыми вызовами реестра)."""
import json
import sys
import textwrap

import pytest

import mcp_registry

FAKE_MCP = textwrap.dedent("""
    import json, sys
    def read_frame():
        headers = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line or line in (b"\\r\\n", b"\\n"):
                break
            k, _, v = line.decode().partition(":")
            headers[k.strip().lower()] = v.strip()
        return json.loads(sys.stdin.buffer.read(int(headers.get("content-length", "0"))).decode())
    def send(obj):
        raw = json.dumps(obj).encode()
        sys.stdout.buffer.write(b"Content-Length: " + str(len(raw)).encode() + b"\\r\\n\\r\\n" + raw)
        sys.stdout.buffer.flush()
    while True:
        try:
            msg = read_frame()
        except Exception:
            break
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
                "name": "get_fact", "description": "Факт из MCP",
                "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}},
                "annotations": {"readOnlyHint": True}}]}})
        elif method == "tools/call":
            args = (msg.get("params") or {}).get("arguments") or {}
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "MCP-FACT:" + str(args.get("topic"))}],
                "isError": False}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
""")


@pytest.fixture
def mcp_server(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_CONNECTIONS_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setattr(mcp_registry, "STORE", tmp_path / "mcp.json")
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_MCP, encoding="utf-8")
    sid = mcp_registry.save_connection({
        "name": "Fake MCP", "transport": "stdio",
        "command": [sys.executable, str(script)],
    })
    yield sid
    mcp_registry.remove_connection(sid)


def test_react_agent_discovers_and_calls_mcp_tool_via_manager(mcp_server, monkeypatch):
    import identity
    monkeypatch.setattr(identity, "is_admin_owner", lambda *a, **k: True)
    monkeypatch.setattr(identity, "chat_owner", lambda *a, **k: "owner")

    import tool_registry
    from agent.loop import AgentContext, run_react

    # Каталог инструментов агента строится через MCP Manager.
    schemas = {s["name"] for s in tool_registry.tool_schemas()}
    assert "mcp_call" in schemas
    tool_registry._register_agent_tools()
    mcp_spec = tool_registry.get_tool("mcp_call")
    assert mcp_spec is not None

    transcript = iter([
        ('```json\n{"tool":"mcp_call","arguments":{'
         f'"server_id":"{mcp_server}","tool":"get_fact","arguments":{{"topic":"nova"}}}}}}\n```', {}),
        ("Вот факт из MCP: MCP-FACT:nova.", {"model": "fake"}),
    ])
    events = []
    result = run_react(AgentContext(
        "Получи факт из MCP", complete=lambda *a, **k: next(transcript),
        emit=events.append, max_steps=4, tools=[mcp_spec],
    ))
    assert result.status == "completed"
    assert result.steps >= 1
    mcp_observations = [o for o in result.observations if o["tool"] == "mcp_call"]
    assert mcp_observations, "агент не вызвал mcp_call"
    assert mcp_observations[0]["result"].get("ok") is True
    assert "MCP-FACT:nova" in json.dumps(mcp_observations[0]["result"])
    assert any(e.get("type") == "tool" and e.get("name") == "mcp_call" for e in events)


def test_mcp_manager_isolates_dead_server_during_agent_discovery(tmp_path, monkeypatch, mcp_server):
    import mcp_manager
    monkeypatch.setenv("MCP_CONNECTIONS_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setattr(mcp_registry, "STORE", tmp_path / "mcp.json")
    dead = mcp_registry.save_connection({
        "name": "Dead MCP", "transport": "stdio", "command": ["/nonexistent/xyz"],
    })
    try:
        tools = mcp_manager.discover_tools()
        # живой сервер обнаружен, мёртвый молча пропущен — без исключений
        assert any(t["name"] == "get_fact" and t["server_id"] == mcp_server for t in tools)
        assert all(t["server_id"] != dead for t in tools)
        health = {r["id"]: r for r in mcp_manager.health_check_all()}
        assert health[mcp_server]["healthy"] is True
        assert health[dead]["healthy"] is False
    finally:
        mcp_registry.remove_connection(dead)


# ── Современный STDIO-режим: JSON-сообщения построчно (без Content-Length) ──
MODERN_MCP = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if method == "server/discover":
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}},
                "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "modern-fake", "version": "1"}}}}), flush=True)
        elif method == "tools/list":
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
                "name": "modern_ping", "description": "modern line-based tool",
                "inputSchema": {"type": "object"}}]}}), flush=True)
        elif method == "tools/call":
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "modern-ok"}], "isError": False}}), flush=True)
        elif mid is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {}}), flush=True)
""")


def test_modern_line_based_stdio_server_via_manager(tmp_path, monkeypatch):
    import mcp_manager
    monkeypatch.setenv("MCP_CONNECTIONS_FILE", str(tmp_path / "mcp_modern.json"))
    monkeypatch.setattr(mcp_registry, "STORE", tmp_path / "mcp_modern.json")
    script = tmp_path / "modern_mcp.py"
    script.write_text(MODERN_MCP, encoding="utf-8")
    sid = mcp_registry.save_connection({
        "name": "Modern MCP", "transport": "stdio",
        "command": [sys.executable, str(script)],
    })
    try:
        result = mcp_manager.connect(sid)
        assert result["connected"] is True, result
        tools = mcp_manager.discover_tools()
        assert any(t["name"] == "modern_ping" and t["server_id"] == sid for t in tools)
        call = mcp_manager.call_tool(sid, "modern_ping", {})
        assert call["ok"] is True
        assert "modern-ok" in json.dumps(call["result"])
        health = mcp_manager.health_check(sid)
        assert health["healthy"] is True
    finally:
        mcp_registry.remove_connection(sid)


def test_discover_tools_keeps_cached_tools_of_dead_server(tmp_path, monkeypatch):
    """После падения сервера его ранее обнаруженные инструменты не теряются."""
    import mcp_manager
    monkeypatch.setenv("MCP_CONNECTIONS_FILE", str(tmp_path / "mcp_cache.json"))
    monkeypatch.setattr(mcp_registry, "STORE", tmp_path / "mcp_cache.json")
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_MCP, encoding="utf-8")
    sid = mcp_registry.save_connection({
        "name": "Cache MCP", "transport": "stdio",
        "command": [sys.executable, str(script)],
    })
    try:
        assert mcp_manager.connect(sid)["connected"] is True
        tools = mcp_manager.discover_tools()
        assert any(t["name"] == "get_fact" for t in tools)
        # сервер "умер": подменяем команду на несуществующую и сбрасываем сессию
        import time as _time
        mcp_registry._close_stdio_session(sid)
        mcp_registry._TOOL_CACHE[sid] = (_time.monotonic() + 60, [{
            "name": "get_fact", "description": "cached", "inputSchema": {"type": "object"}}])
        mcp_registry._set_status(sid, command="/nonexistent/xyz")
        mcp_registry._FAILURE_UNTIL[sid] = _time.monotonic() + 60
        tools = mcp_manager.discover_tools()
        cached = [t for t in tools if t.get("server_id") == sid]
        assert cached and cached[0].get("degraded") is True
    finally:
        mcp_registry.remove_connection(sid)


def test_command_code_uses_chat_completion_meta_correctly(client, monkeypatch):
    """Регрессия: chat_completion возвращает (text, meta-dict) при успехе —
    meta не должен попадать в поле error (реальный вызов /command падал)."""
    import routes.commands as commands_mod
    captured = {}

    def fake_completion(messages, **kwargs):
        captured["messages"] = messages
        return "print('hello')", {"provider": "local_demo", "model": "nova-local-1",
                                   "failover": [], "free_model": True}

    monkeypatch.setattr(commands_mod, "chat_completion", fake_completion)
    resp = client.post("/command", json={"command": "/code hello world"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "error" not in data, data
    assert data["result"] == "print('hello')"
