"""The ReAct run has one orchestrator and sequences observations in one context."""
import threading

from agent.loop import AgentContext, run_react
from tool_registry import ToolSpec


def _tool(name):
    return ToolSpec(name, f"Fixture tool {name}",
                    {"type": "object", "properties": {"query": {"type": "string"}},
                     "required": ["query"]},
                    lambda **kwargs: {"ok": True, "result": name + " result"})


def test_one_orchestrator_sequences_tools_and_reuses_one_history():
    tools = [_tool("lookup_a"), _tool("lookup_b")]
    replies = iter([
        ('```json\n{"tool":"lookup_a","arguments":{"query":"a"}}\n```', {}),
        ('```json\n{"tool":"lookup_b","arguments":{"query":"b"}}\n```', {}),
        ("Сводный ответ основан на обоих результатах.", {"model": "fake"}),
    ])
    provider_threads = []
    tool_calls = []

    def complete(messages, **kwargs):
        provider_threads.append(threading.get_ident())
        if len(provider_threads) > 1:
            assert "lookup_a result" in str(messages)
        return next(replies)

    result = run_react(AgentContext(
        "Собери сведения A и B", complete=complete, tools=tools,
        call_tool=lambda name, **kwargs: tool_calls.append(name)
        or {"ok": True, "result": name + " result"}))
    assert result.status == "completed"
    assert result.steps == 2
    assert tool_calls == ["lookup_a", "lookup_b"]
    assert len(set(provider_threads)) == 1
    assert result.answer.startswith("Сводный ответ")


def test_tool_failure_is_an_observation_not_a_specialist_fallback():
    replies = iter([
        ('```json\n{"tool":"lookup_a","arguments":{"query":"a"}}\n```', {}),
        ("Первый поиск завершился ошибкой; дальнейших действий не предпринимал.", {}),
    ])
    calls = []
    result = run_react(AgentContext(
        "Проверь данные", complete=lambda *a, **k: next(replies), tools=[_tool("lookup_a")],
        call_tool=lambda *a, **k: calls.append(a) or {"ok": False, "error": "offline"}))
    assert calls == [("lookup_a",)]
    assert result.steps == 1
    assert result.observations[0]["result"]["ok"] is False
    assert "ошибкой" in result.answer


def test_no_specialist_roles_or_parallel_branches_are_created():
    seen = []
    result = run_react(AgentContext(
        "Сравни два варианта", complete=lambda messages, **kwargs:
            seen.append(kwargs.get("system", "")) or ("Сравнение готово.", {}), tools=[]))
    assert result.status == "completed"
    assert len(seen) == 1
    assert not any(role in seen[0].lower() for role in ("researcher", "analyst", "critic", "planner"))


def test_model_reconsiders_a_batch_after_the_first_observation():
    actions = []
    def complete(messages, **kwargs):
        if not actions:
            return "", {"tool_calls": [
                {"name": "lookup_a", "arguments": {"query": "first"}},
                {"name": "lookup_b", "arguments": {"query": "speculative"}},
            ]}
        if len(actions) == 1:
            # The second query must come from A, not the model's initial plan.
            assert "key_from_A" in messages[-1]["content"]
            return "", {"tool_calls": [{"name": "lookup_b", "arguments": {"query": "key_from_A"}}]}
        assert "value_from_B" in messages[-1]["content"]
        return "Нашёл значение B.", {}

    def execute(name, query):
        actions.append((name, query))
        return {"ok": True, "result": "key_from_A" if name == "lookup_a" else "value_from_B"}

    result = run_react(AgentContext("Собери факты", complete=complete,
                       tools=[_tool("lookup_a"), _tool("lookup_b")], call_tool=execute))
    assert actions == [("lookup_a", "first"), ("lookup_b", "key_from_A")]
    assert result.steps == 2 and result.answer == "Нашёл значение B."
