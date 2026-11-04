import pytest

import mcp_manager
import mcp_registry
import routes.agent as agent


def test_non_admin_agent_only_discovers_readonly_mcp_tools(monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_USER_READ", "1")
    monkeypatch.setattr(agent._identity, "is_admin_owner", lambda owner: False)
    monkeypatch.setattr(mcp_manager, "discover_tools", lambda: [
        {"server_id": "srv", "name": "lookup", "annotations": {"readOnlyHint": True}},
        {"server_id": "srv", "name": "send", "annotations": {"readOnlyHint": False}},
        {"server_id": "srv", "name": "unknown", "annotations": {}},
    ])

    tools = agent.NovaToolsContext._mcp_tools("anon:browser")

    assert [tool["name"] for tool in tools] == ["lookup"]


def test_non_admin_agent_cannot_call_write_mcp_tool(monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_USER_READ", "1")
    monkeypatch.setattr(agent._identity, "is_admin_owner", lambda owner: False)
    monkeypatch.setattr(mcp_registry, "cached_tool_annotations", lambda *args: {"readOnlyHint": False})
    called = []
    monkeypatch.setattr(mcp_manager, "call_tool", lambda *args: called.append(args))
    context = agent.NovaToolsContext.__new__(agent.NovaToolsContext)
    context.owner = "anon:browser"

    with pytest.raises(PermissionError, match="readOnlyHint=true"):
        context.call_mcp("srv", "send", {})

    assert called == []


def test_non_admin_agent_can_call_annotated_readonly_mcp_tool(monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_USER_READ", "1")
    monkeypatch.setattr(agent._identity, "is_admin_owner", lambda owner: False)
    monkeypatch.setattr(mcp_registry, "cached_tool_annotations", lambda *args: {"readOnlyHint": True})
    monkeypatch.setattr(mcp_manager, "call_tool", lambda *args: {"ok": True, "result": {"content": []}})
    context = agent.NovaToolsContext.__new__(agent.NovaToolsContext)
    context.owner = "anon:browser"

    assert context.call_mcp("srv", "lookup", {}) == {"content": []}
