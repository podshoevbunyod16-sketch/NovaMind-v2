from __future__ import annotations

from agent.loop import AgentContext, run_react
from tool_registry import ToolSpec


def test_direct_dynamic_mcp_function_is_routed_through_mcp_call_and_returns_real_result():
    calls = []
    completion_inputs = []

    def complete(messages, **kwargs):
        completion_inputs.append((messages, kwargs))
        if len(completion_inputs) == 1:
            return "", {"tool_calls": [{"function": {
                "name": "search_mcp_servers",
                "arguments": {"query": "MCP server for internet search", "limit": 5},
            }}]}
        return "Найдено в каталоге RMCP.", {"model": "test"}

    def dispatch(name, **kwargs):
        calls.append((name, kwargs))
        return {"ok": True, "result": {"content": [{"type": "text", "text": "RMCP_REAL_RESULT_73"}]}}

    spec = ToolSpec(
        "mcp_call", "Universal MCP router",
        {"type": "object", "properties": {
            "server_id": {"type": "string"}, "tool": {"type": "string"},
            "arguments": {"type": "object"}}, "required": ["server_id", "tool"]},
        lambda **_: None, permissions=("network", "external_service"), risk="safe",
    )
    result = run_react(AgentContext(
        goal="Найди MCP серверы для интернет-поиска",
        complete=complete,
        tools=[spec],
        call_tool=dispatch,
        mcp_tools=[{"server_id": "rmcp", "name": "search_mcp_servers",
                    "description": "Search the RMCP server catalog",
                    "inputSchema": {"type": "object", "properties": {
                        "query": {"type": "string"}, "limit": {"type": "integer"}},
                        "required": ["query"]}}],
        max_steps=4,
        timeout=5,
    ))

    assert calls == [("mcp_call", {"server_id": "rmcp", "tool": "search_mcp_servers",
                                     "arguments": {"query": "MCP server for internet search", "limit": 5}})]
    exposed = completion_inputs[0][1]["tools"]
    assert any(item["function"]["name"] == "search_mcp_servers" for item in exposed)
    observation_messages = [item["content"] for item in completion_inputs[1][0]
                            if isinstance(item.get("content"), str)]
    assert any("RMCP_REAL_RESULT_73" in text for text in observation_messages)
    assert result.observations[0]["result"]["result"]["content"][0]["text"] == "RMCP_REAL_RESULT_73"


def test_duplicate_mcp_names_get_server_qualified_exposed_names():
    from agent.loop import _mcp_exposed_tools

    schemas, aliases = _mcp_exposed_tools([
        {"server_id": "rmcp-a", "name": "search", "inputSchema": {"type": "object"}},
        {"server_id": "rmcp-b", "name": "search", "inputSchema": {"type": "object"}},
    ], {"mcp_call", "mcp_list"})
    names = {item["function"]["name"] for item in schemas}
    assert names == {"mcp__rmcp-a__search", "mcp__rmcp-b__search"}
    assert set(aliases) == names


def test_direct_rmcp_tool_call_uses_registry_protocol_and_returns_server_payload(monkeypatch, tmp_path):
    import json
    import mcp_manager
    import mcp_registry

    store = tmp_path / "mcp-connections.json"
    store.write_text(json.dumps({"rmcp": {
        "name": "Rmcp", "url": "https://rmcp.test/mcp", "transport": "streamable_http",
        "auth": "none", "connected": True, "initialized": True,
    }}), encoding="utf-8")
    monkeypatch.setattr(mcp_registry, "STORE", store)
    mcp_registry._TOOL_CACHE.clear()
    mcp_registry._FAILURE_UNTIL.clear()

    real_payload = {"content": [{"type": "text", "text": "RMCP_SEARCH_RESULT_812"}]}

    def perform(_sid, _connection, payload, _schema=None):
        if payload["method"] == "tools/list":
            result = {"tools": [{"name": "search_mcp_servers",
                "description": "Search indexed MCP servers",
                "inputSchema": {"type": "object", "properties": {
                    "query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"]}}]}
        elif payload["method"] == "tools/call":
            assert payload["params"] == {"name": "search_mcp_servers",
                "arguments": {"query": "MCP tools", "limit": 5}}
            result = real_payload
        else:
            raise AssertionError(payload["method"])
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}

    monkeypatch.setattr(mcp_registry, "_perform", perform)
    discovered = mcp_manager.discover_tools()
    assert any(item["server_id"] == "rmcp" and item["name"] == "search_mcp_servers"
               for item in discovered)
    outcome = mcp_manager.call_tool("rmcp", "search_mcp_servers",
                                    {"query": "MCP tools", "limit": 5})
    assert outcome == {"ok": True, "result": real_payload}
