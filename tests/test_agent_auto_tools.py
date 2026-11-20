"""End-to-end checks for automatic tool/MCP choice in the ReAct route."""
from __future__ import annotations

import mcp_manager
import mcp_registry
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


def _mcp_catalog():
    return [
        {"server_id": "github", "server_name": "GitHub", "name": "list_issues",
         "description": "Перечислить issue и pull request в репозитории",
         "annotations": {"readOnlyHint": True},
         "inputSchema": {"type": "object", "properties": {}}},
        {"server_id": "github", "server_name": "GitHub", "name": "delete_repo",
         "description": "Удалить репозиторий", "inputSchema": {"type": "object", "properties": {}}},
        {"server_id": "docs", "server_name": "Docs", "name": "search_docs",
         "description": "Поиск по документации", "annotations": {"readOnlyHint": True},
         "inputSchema": {"type": "object", "properties": {}}},
    ]


def _stub_mcp(monkeypatch):
    monkeypatch.setattr(mcp_manager, "discover_tools", _mcp_catalog)
    monkeypatch.setattr(mcp_manager, "server_summaries", lambda: [
        {"server_id": "github", "name": "GitHub", "connected": True},
        {"server_id": "docs", "name": "Docs", "connected": True},
    ])
    monkeypatch.setattr(mcp_registry, "cached_tool_annotations",
                        lambda sid, tool: next((item.get("annotations") or {}
                                                for item in _mcp_catalog()
                                                if item["server_id"] == sid and item["name"] == tool), {}))
    monkeypatch.setattr(mcp_registry, "call_tool",
                        lambda sid, tool, args=None: {"content": [{"type": "text", "text": "ISSUE-42"}]})


def test_auto_mode_shows_selection_and_model_can_choose_discovered_mcp_tool(admin_client, monkeypatch):
    _stub_mcp(monkeypatch)
    seen = []
    script(monkeypatch, [
        '```mcp_call\n{"server_id": "github", "tool": "list_issues", "arguments": {}}\n```',
        "В репозитории 1 открытый issue.",
    ], seen)
    events = ndjson(admin_client.post("/api/agent/react", json={
        "message": "покажи issue в подключённом GitHub репозитории",
        "auto_tools": True, "tools": True,
    }))
    selections = events_of(events, "selection")
    assert len(selections) == 1
    assert "github" in selections[0]["mcp_servers"]
    assert "list_issues" in selections[0]["mcp_tools"]
    tools = events_of(events, "tool")
    assert len(tools) == 1 and tools[0]["name"] == "mcp_call"
    assert "ISSUE-42" in model_text(seen, 1)
    assert "issue" in answer_of(events).lower()


def test_auto_mode_is_advisory_and_exposes_all_permitted_mcp_tools(admin_client, monkeypatch):
    _stub_mcp(monkeypatch)
    seen = []
    script(monkeypatch, ["Готово."], seen)
    events = ndjson(admin_client.post("/api/agent/react", json={
        "message": "покажи issue", "auto_tools": True, "tools": True,
    }))
    assert events_of(events, "selection")[0]["mode"] == "advisory"
    exposed = {item["function"]["name"] for item in (seen[0]["tools"] or [])}
    assert {"list_issues", "delete_repo", "search_docs"} <= exposed


def test_strict_mode_limits_catalogue_but_keeps_recovery_tools(admin_client, monkeypatch):
    monkeypatch.setenv("AUTO_TOOLS_STRICT", "1")
    _stub_mcp(monkeypatch)
    seen = []
    script(monkeypatch, ["Готово."], seen)
    events = ndjson(admin_client.post("/api/agent/react", json={
        "message": "покажи issue в github", "auto_tools": True, "tools": True,
    }))
    selection = events_of(events, "selection")[0]
    assert selection["mode"] == "strict"
    assert "github" in selection["mcp_servers"]
    assert "docs" not in selection["mcp_servers"]
    exposed = {item["function"]["name"] for item in (seen[0]["tools"] or [])}
    assert "mcp_call" in exposed and "mcp_list" in exposed
    assert "search_docs" not in exposed
    prompt = model_text(seen, 0)
    assert "Selected MCP servers/tools" in prompt


def test_explicit_tools_opt_out_still_disables_auto_tools(admin_client, monkeypatch):
    _stub_mcp(monkeypatch)
    seen = []
    script(monkeypatch, ["Готово."], seen)
    events = ndjson(admin_client.post("/api/agent/react", json={
        "message": "покажи issue", "auto_tools": True, "tools": False,
    }))
    assert events_of(events, "selection") == []
    assert events_of(events, "capability")[0]["tools"] == []


def test_auto_mode_default_off_keeps_legacy_behaviour(admin_client, monkeypatch):
    _stub_mcp(monkeypatch)
    seen = []
    script(monkeypatch, ["Готово."], seen)
    events = ndjson(admin_client.post("/api/agent/react", json={
        "message": "покажи issue", "tools": True,
    }))
    assert events_of(events, "selection") == []
    assert "AUTO TOOL SELECTION" not in model_text(seen, 0)


def test_auto_mode_never_expands_role_permissions(google_client, monkeypatch):
    """MCP stays owner-only even when automatic selection is requested."""
    _stub_mcp(monkeypatch)
    seen = []
    script(monkeypatch, ["Готово."], seen)
    events = ndjson(google_client.post("/api/agent/react", json={
        "message": "покажи issue в github", "auto_tools": True, "tools": True,
    }))
    capability = events_of(events, "capability")[0]
    assert "mcp_call" not in capability["tools"]
    selection = events_of(events, "selection")[0]
    assert selection["mcp_servers"] == [] and selection["mcp_tools"] == []
    exposed = {item["function"]["name"] for item in (seen[0]["tools"] or [])}
    assert "list_issues" not in exposed
