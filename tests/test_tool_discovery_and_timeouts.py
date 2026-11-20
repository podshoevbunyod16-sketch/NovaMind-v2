"""Tests for two-meta-tool discovery (tool_search + use_tool) and provider timeouts."""
from __future__ import annotations

import json

import ai_providers
import local_providers
import mcp_chat
import mcp_registry
import media_generation
import routes.agent as agent_routes
import tool_registry
import tool_search
from agent.verification import verify_task
from tests.agent_helpers import answer_of, events_of, model_text, ndjson, script


def _sample_mcp_catalog() -> list[dict]:
    return [
        {
            "server_id": "github",
            "server_name": "GitHub",
            "name": "find_repo",
            "description": "Search and find repositories on GitHub",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        },
        {
            "server_id": "github",
            "server_name": "GitHub",
            "name": "open_repo",
            "description": "Open a GitHub repository and inspect README and structure",
            "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        },
        {
            "server_id": "github",
            "server_name": "GitHub",
            "name": "get_file_contents",
            "description": "Read a file or directory inside a GitHub repository",
            "inputSchema": {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        },
        {
            "server_id": "github",
            "server_name": "GitHub",
            "name": "create_issue",
            "description": "Create a new issue in a GitHub repository",
            "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
            "annotations": {"readOnlyHint": False},
        },
        {
            "server_id": "github",
            "server_name": "GitHub",
            "name": "delete_repo",
            "description": "Delete a repository permanently",
            "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}},
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
        {
            "server_id": "github",
            "server_name": "GitHub",
            "name": "list_notifications",
            "description": "List user notifications",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True},
        },
        {
            "server_id": "weather",
            "server_name": "Weather",
            "name": "get_weather",
            "description": "Get weather forecast for a city",
            "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        },
        {
            "server_id": "rmcp",
            "server_name": "RMCP Registry",
            "name": "search_mcp_servers",
            "description": "Search third-party MCP servers in the directory",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        },
    ]


def test_tool_search_web_query_returns_only_web_tools():
    tool_registry.bootstrap_default_tools()
    specs = tool_registry.list_tools()
    mcp_tools = _sample_mcp_catalog()

    for query in ("топ ии модели", "поиск в интернете", "web search and open page"):
        found = tool_search.search_tools(query, specs, mcp_tools, goal=query)
        names = [item["tool"] for item in found]
        assert set(names) == {"web_search", "web_open"}, (query, names)


def test_tool_search_repo_query_returns_only_relevant_read_repo_tools():
    tool_registry.bootstrap_default_tools()
    specs = tool_registry.list_tools()
    mcp_tools = _sample_mcp_catalog()

    found = tool_search.search_tools("посмотри на мой репозиторий", specs, mcp_tools,
                                     goal="посмотри на мой репозиторий")
    names = [item["tool"] for item in found]
    assert "find_repo" in names
    assert "open_repo" in names
    assert "create_issue" not in names
    assert "delete_repo" not in names
    assert "list_notifications" not in names
    assert "get_weather" not in names
    assert "search_mcp_servers" not in names
    assert "web_search" not in names
    assert "shell" not in names


def test_tool_search_repo_write_query_surfaces_create_issue_not_delete():
    tool_registry.bootstrap_default_tools()
    specs = tool_registry.list_tools()
    mcp_tools = _sample_mcp_catalog()

    found = tool_search.search_tools("создай issue в репозитории", specs, mcp_tools)
    names = [item["tool"] for item in found]
    assert "create_issue" in names
    assert "delete_repo" not in names
    assert "web_search" not in names


def test_tool_search_guest_never_leaks_workspace_tools_on_file_query():
    tool_registry.bootstrap_default_tools()
    specs = [s for s in tool_registry.list_tools()
             if "workspace" not in getattr(s, "permissions", ())]
    found = tool_search.search_tools("прочитай файл app.py", specs, [])
    names = [item["tool"] for item in found]
    assert "file_read" not in names
    assert "web_open" not in names


def test_react_sends_only_two_meta_tools_and_omits_mcp_catalog_bloat(
    admin_client, sandbox, monkeypatch
):
    monkeypatch.setattr(mcp_registry, "connected_tools", _sample_mcp_catalog)
    seen = []
    script(monkeypatch, ["Готово."], seen)
    ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))

    assert len(seen) == 1
    offered = [item["function"]["name"] for item in (seen[0]["tools"] or [])]
    assert offered == ["tool_search", "use_tool"]
    prompt = model_text(seen)
    assert "delete_repo" not in prompt
    assert "get_weather" not in prompt
    assert "search_mcp_servers" not in prompt


def test_react_end_to_end_tool_search_then_use_tool_mcp(admin_client, sandbox, monkeypatch):
    monkeypatch.setattr(mcp_registry, "connected_tools", _sample_mcp_catalog)
    monkeypatch.setattr(mcp_registry, "cached_tool_annotations",
                        lambda _sid, _tool: {"readOnlyHint": True})
    called = []

    def fake_call(server_id, tool_name, arguments=None):
        called.append((server_id, tool_name, dict(arguments or {})))
        return {"repos": [{"name": "NovaMind-v2", "stars": 42}]}

    monkeypatch.setattr(mcp_registry, "call_tool", fake_call)
    seen = []
    script(monkeypatch, [
        '{"tool":"tool_search","arguments":{"query":"посмотри на мой репозиторий"}}',
        '{"tool":"use_tool","arguments":{"tool":"find_repo","arguments":{"query":"NovaMind"}}}',
        "Ваш репозиторий NovaMind-v2 найден (42 звезды).",
    ], seen)

    events = ndjson(admin_client.post("/api/agent/stream",
                                      json={"message": "посмотри на мой репозиторий"}))
    assert called == [("github", "find_repo", {"query": "NovaMind"})]
    tools_ran = events_of(events, "tool")
    assert [t["name"] for t in tools_ran] == ["tool_search", "mcp_call"]
    discovered = {t["tool"] for t in tools_ran[0]["result"]["tools"]}
    assert {"find_repo", "open_repo"} <= discovered
    assert "get_weather" not in discovered
    assert "NovaMind-v2" in answer_of(events)


def test_react_end_to_end_tool_search_then_use_tool_web(admin_client, sandbox, monkeypatch):
    results = [{"title": "Top AI Models", "url": "https://example.test/models",
                "snippet": "Comparison of top AI models", "host": "example.test"}]
    monkeypatch.setattr(agent_routes, "nova_search",
                        lambda query, limit=8: (results, {"results": results, "count": 1, "query": query}))
    seen = []
    script(monkeypatch, [
        '{"tool":"tool_search","arguments":{"query":"топ ии модели"}}',
        '{"tool":"use_tool","arguments":{"tool":"web_search","arguments":{"query":"топ ии модели"}}}',
        "Вот актуальный рейтинг ИИ-моделей.",
    ], seen)

    events = ndjson(admin_client.post("/api/agent/stream", json={"message": "топ ии модели"}))
    tools_ran = events_of(events, "tool")
    assert [t["name"] for t in tools_ran] == ["tool_search", "web_search"]
    discovered = [t["tool"] for t in tools_ran[0]["result"]["tools"]]
    assert set(discovered) == {"web_search", "web_open"}
    assert events_of(events, "sources")[0]["sources"][0]["url"] == "https://example.test/models"
    assert "рейтинг" in answer_of(events)


def test_verify_task_does_not_treat_tool_search_alone_as_action_completion():
    verdict = verify_task("создай файл report.md", [
        {"tool": "tool_search", "arguments": {"query": "write file"}, "result": {"ok": True, "tools": []}},
    ])
    assert verdict["verified"] is False


def test_mcp_chat_filters_large_catalog_by_relevance(monkeypatch):
    monkeypatch.setattr(mcp_chat, "_mcp_allowed_for_user", lambda: True)
    import mcp_manager
    monkeypatch.setattr(mcp_manager, "discover_tools", _sample_mcp_catalog)

    prompts_seen = []

    def fake_complete(messages, **kwargs):
        prompts_seen.append(kwargs.get("system") or "")
        return "Готово.", {"model": "mock"}

    monkeypatch.setattr(mcp_chat, "chat_completion", fake_complete)

    # Unrelated casual message skips MCP catalog entirely when catalog > 5
    reply, _, _ = mcp_chat.complete_with_mcp(
        [{"role": "user", "content": "привет, как дела?"}],
        system="SYS", provider="groq", model="mock",
    )
    assert reply is None and not prompts_seen

    # Repository message sends only repo tools, not weather/rmcp
    reply, _, _ = mcp_chat.complete_with_mcp(
        [{"role": "user", "content": "покажи мой репозиторий на github"}],
        system="SYS", provider="groq", model="mock",
    )
    assert reply == "Готово."
    assert len(prompts_seen) == 1
    assert "find_repo" in prompts_seen[0]
    assert "get_weather" not in prompts_seen[0]


def test_provider_timeouts_are_extended_for_pollinations_and_local_models(
    admin_client, sandbox, monkeypatch
):
    assert ai_providers.default_timeout_for("pollinations") >= 240.0
    assert ai_providers.default_timeout_for("openai_compatible") >= 300.0
    assert ai_providers.default_timeout_for("groq") == 90.0
    assert local_providers._CATALOG_TIMEOUT >= 10.0

    import inspect
    sig = inspect.signature(media_generation._pollinations_catalog)
    assert sig.parameters["timeout"].default >= 12.0

    # Verify ai_providers.chat_completion passes extended default timeouts
    captured_timeouts = []

    def fake_openai_request(url, payload, headers, timeout=90, max_retries=2):
        captured_timeouts.append(timeout)
        return {"choices": [{"message": {"content": "ok"}}]}, None

    monkeypatch.setattr(ai_providers, "openai_compatible_request", fake_openai_request)
    monkeypatch.setattr(ai_providers, "is_free_ai_model", lambda *_a, **_kw: True)
    monkeypatch.setattr(ai_providers, "_pm_skip_provider", lambda _p: False)

    ai_providers.chat_completion([{"role": "user", "content": "hi"}],
                                 provider="openai_compatible", model="qwen2.5:7b")
    ai_providers.chat_completion([{"role": "user", "content": "hi"}],
                                 provider="pollinations", model="openai")
    assert captured_timeouts[0] >= 300.0
    assert captured_timeouts[1] >= 240.0

    # Verify ReAct loop does not cap pollinations / openai_compatible turns to 45s
    react_timeouts = []

    def fake_complete(messages, **kwargs):
        react_timeouts.append(kwargs.get("timeout"))
        return "Готово.", {"model": kwargs.get("model"), "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}}

    monkeypatch.setattr(ai_providers, "chat_completion", fake_complete)
    for prov, mod in (("openai_compatible", "qwen2.5:7b"), ("pollinations", "openai")):
        monkeypatch.setattr(ai_providers, "resolve_target",
                            lambda *_a, p=prov, m=mod, **_kw: (p, m, {"max_tokens": 4096}))
        events = ndjson(admin_client.post("/api/agent/stream", json={"message": "привет"}))
        res = next(e for e in events if e["type"] == "result")
        assert res["usage"]["prompt_tokens"] == 120
        assert res["usage"]["completion_tokens"] == 30
    assert react_timeouts[0] >= 300.0
    assert react_timeouts[1] >= 240.0
