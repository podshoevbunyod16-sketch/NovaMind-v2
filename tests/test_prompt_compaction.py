"""Regression tests for compact agent prompts and unchanged ReAct behavior."""
import agent.loop as react_loop
import mcp_chat


def test_react_prompt_is_compact_and_keeps_core_contract():
    prompt = react_loop._system_prompt()
    assert len(prompt) <= 1100
    for required in ("ReAct", "MCP", "недоверенные данные", "секреты",
                     "подтверждением", "по-русски"):
        assert required in prompt


def test_react_goal_is_not_duplicated_in_system_context():
    goal = "UNIQUE_PROMPT_GOAL_7F31"
    captured = {}

    def complete(messages, **kwargs):
        captured["messages"] = [dict(item) for item in messages]
        captured["system"] = kwargs["system"]
        return "Готово.", {"model": "test"}

    context = react_loop.AgentContext(
        goal=goal, complete=complete, tools=[], max_steps=1
    )
    result = react_loop.run_react(context)

    assert result.status == "completed"
    system_text = "\n".join(
        str(item.get("content") or "")
        for item in captured["messages"]
        if item.get("role") == "system"
    )
    assert goal not in system_text
    assert sum(
        item.get("role") == "user" and item.get("content") == goal
        for item in captured["messages"]
    ) == 1


def test_mcp_prompt_keeps_exact_call_schema_and_confirmation():
    tools = [{
        "server_id": "rmcp",
        "name": "search_mcp_servers",
        "description": "Search connected MCP servers",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }]
    prompt = mcp_chat._tool_prompt(tools)
    assert len(prompt) < 700
    assert '"action":"mcp_call"' in prompt
    assert '"server_id"' in prompt and '"arguments"' in prompt
    assert "rmcp/search_mcp_servers" in prompt
    assert '"required":["query"]' in prompt
    assert "не спрашивай server_id" in prompt
    assert "недоверенные данные" in prompt
    assert "подтверждение" in prompt
