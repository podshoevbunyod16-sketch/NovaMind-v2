"""Поиск нашёл данные — пользователь получает ответ, а не «ИИ не работает».

Регрессии на реальные причины жалобы «веб-поиск находит данные, а ИИ не отвечает»:

* статус ``degraded``: модель упала после успешного поиска, но ответ собирается
  из уже полученных наблюдений (а не «Не удалось получить ответ модели»);
* модель повторяет один и тот же вызов — вместо «Не удалось продолжить»
  приходит ответ по найденным данным;
* хвост «Источники:» в конце ответа больше не считается обрывом;
* наблюдение инструмента никогда не режется в невалидный JSON;
* транзиентные ошибки провайдера повторяются, а критические — нет;
* нативный tool calling отдаёт пару assistant.tool_calls → tool с тем же id.
"""
import json

import pytest

import agent.loop as loop_module
from agent.loop import AgentContext, run_react
from agent.output import clean_final_answer, is_truncated_answer
from tool_registry import ToolSpec

GOAL = "какой курс доллара сегодня в Худжанде"
FOUND = {"ok": True, "count": 2, "results": [
    {"title": "Курс НБТ", "url": "https://nbt.tj/usd", "snippet": "USD 10,9000"},
    {"title": "Курсы банков", "url": "https://bank.tj/usd", "snippet": "USD 11,0000"},
]}


def _search_spec():
    return ToolSpec("web_search", "Search the web",
                    {"type": "object", "properties": {"query": {"type": "string"},
                                                      "limit": {"type": "integer"}},
                     "required": ["query"]},
                    lambda **_kwargs: dict(FOUND), permissions=("network",), risk="safe")


def _json_call(query="курс доллара"):
    return ('```json\n{"tool":"web_search","arguments":{"query":"' + query + '"}}\n```', {})


def _native_call(query="курс доллара", call_id="call_1"):
    return "", {"tool_calls": [{"id": call_id, "type": "function",
                                "function": {"name": "web_search",
                                             "arguments": json.dumps({"query": query})}}]}


def _run(replies, goal=GOAL, max_steps=6):
    queue = iter(replies)

    def complete(*_args, **_kwargs):
        try:
            return next(queue)
        except StopIteration:
            return None, "fixture exhausted"

    spec = _search_spec()
    return run_react(AgentContext(goal, complete=complete, tools=[spec],
                                  call_tool=lambda name, **kwargs: spec.call(**kwargs),
                                  max_steps=max_steps, max_tokens=2048))


# ══════════════ ответ по найденным данным без модели ══════════════

def test_provider_failure_after_search_returns_sources_instead_of_error(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER_RETRY_DELAY", "0")
    result = _run([_json_call(), (None, "Groq HTTP 429: rate limit exceeded"),
                   (None, "Groq HTTP 429: rate limit exceeded"),
                   (None, "Groq HTTP 429: rate limit exceeded")])
    assert result.status == "degraded"
    assert "Не удалось получить ответ модели" not in result.answer
    assert "https://nbt.tj/usd" in result.answer
    assert "10,9000" in result.answer
    assert "429" in result.answer  # честно называем причину


def test_empty_search_does_not_pretend_to_have_data():
    empty = {"ok": True, "count": 0, "results": []}

    def complete(*_args, **_kwargs):
        return None, "Groq HTTP 429: rate limit"

    spec = ToolSpec("web_search", "Search", {"type": "object",
                                             "properties": {"query": {"type": "string"}},
                                             "required": ["query"]},
                    lambda **_kwargs: dict(empty), permissions=("network",))
    result = run_react(AgentContext(GOAL, complete=complete, tools=[spec],
                                    call_tool=lambda name, **kwargs: dict(empty)))
    assert result.status == "provider_error"
    assert "Не удалось получить ответ модели" in result.answer


def test_sources_are_emitted_once_per_url():
    events = []
    spec = _search_spec()
    replies = iter([_json_call("курс доллара"), _json_call("курс доллара сомони"),
                    ("Курс — 10,90 сомони [1].", {})])
    run_react(AgentContext(GOAL, complete=lambda *a, **k: next(replies), tools=[spec],
                           emit=events.append,
                           call_tool=lambda name, **kwargs: spec.call(**kwargs)))
    urls = [source["url"] for event in events if event.get("type") == "sources"
            for source in event["sources"]]
    assert urls == ["https://nbt.tj/usd", "https://bank.tj/usd"]


# ══════════════ модель зациклилась на одном вызове ══════════════

def test_repeated_identical_call_still_produces_an_answer_from_found_data():
    repeat = _native_call()
    result = _run([repeat, repeat, repeat, repeat, repeat])
    assert result.status == "completed"
    assert "Не удалось продолжить" not in result.answer
    assert "https://nbt.tj/usd" in result.answer


def test_model_that_starts_answering_is_not_interrupted_by_a_retry():
    """Успешный ответ не должен повторяться, даже если первый вызов упал."""
    calls = []

    def complete(*_args, **_kwargs):
        calls.append(1)
        return ("Курс — 10,90 сомони.", {"model": "gpt-oss"})

    result = run_react(AgentContext(GOAL, complete=complete, tools=[]))
    assert len(calls) == 1
    assert result.status == "completed"


def test_repeat_observation_tells_the_model_not_to_present_it_as_new():
    seen = []
    repeat = _json_call()

    def complete(messages, **_kwargs):
        seen.append("\n".join(str(item.get("content") or "") for item in messages))
        return repeat if len(seen) < 3 else ("Готово: курс 10,90 [1].", {})

    spec = _search_spec()
    run_react(AgentContext(GOAL, complete=complete, tools=[spec],
                           call_tool=lambda name, **kwargs: spec.call(**kwargs)))
    assert "NOT executed" in seen[2]
    assert "do not repeat it" in seen[2]


# ══════════════ хвост «Источники:» — не обрыв ══════════════

def test_answer_with_a_trailing_sources_label_is_kept():
    text = ("## Курс валют\n\n| Валюта | Курс |\n|---|---|\n| USD | 10,90 |\n\n**Источники:**")
    assert is_truncated_answer(text) is False
    cleaned = clean_final_answer(text, GOAL)
    assert "| USD | 10,90 |" in cleaned
    assert not cleaned.rstrip().endswith("Источники:")


def test_answer_with_a_real_cut_is_still_rejected():
    assert clean_final_answer("Курс доллара — 10,90 сомони, евро —", GOAL) == ""
    assert clean_final_answer("Первое поколение выпускали в 2008–2012 годах на", GOAL) == ""


def test_trailing_label_does_not_hide_a_cut_inside_the_body():
    text = ("Источники найдены, но подробности такие: курс доллара сегодня составляет "
            "около 10,90 сомони в банках, а в НБТ —")
    assert is_truncated_answer(text) is True


# ══════════════ наблюдение никогда не ломается о лимит ══════════════

def test_observation_truncation_keeps_valid_json():
    huge = {"ok": True, "count": 30, "results": [
        {"title": "T" * 300, "url": "https://example.tj/" + "u" * 200,
         "snippet": "текст " * 400} for _ in range(30)]}
    for limit in (6000, 3000, 1200, 400, 200):
        encoded = loop_module._observation_json(huge, limit)
        parsed = json.loads(encoded)  # не должно бросать
        assert isinstance(parsed, dict) and parsed["ok"] is True


def test_oversized_observation_tells_the_model_that_data_was_cut():
    encoded = loop_module._observation_json({"ok": True, "result": "x" * 50000}, 200)
    parsed = json.loads(encoded)
    assert len(encoded) <= 200
    assert "обрезан" in encoded or parsed.get("truncated") is True


def test_unshrinkable_observation_is_replaced_by_an_actionable_note():
    payload = {"ok": True, "results": [{"title": "T" * 5000, "url": "u" * 5000}] * 20}
    encoded = loop_module._observation_json(payload, 200)
    parsed = json.loads(encoded)
    assert parsed.get("truncated") is True
    assert "limit/path" in parsed["note"]


# ══════════════ повтор транзиентных ошибок ══════════════

def test_transient_rate_limit_is_retried(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER_RETRY_DELAY", "0")
    calls = []

    def complete(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return None, "Groq HTTP 429: rate limit"
        return "Курс — 10,90 сомони.", {"model": "gpt-oss"}

    result = run_react(AgentContext(GOAL, complete=complete, tools=[]))
    assert result.status == "completed"
    assert result.answer == "Курс — 10,90 сомони."
    assert len(calls) == 2


def test_critical_config_error_is_not_retried(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER_RETRY_DELAY", "0")
    calls = []

    def complete(*_args, **_kwargs):
        calls.append(1)
        return None, "Groq HTTP 401: API-ключ отклонён"

    result = run_react(AgentContext(GOAL, complete=complete, tools=[]))
    assert result.status == "provider_error"
    assert len(calls) == 1


def test_retry_attempts_are_bounded(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER_RETRY_DELAY", "0")
    calls = []

    def complete(*_args, **_kwargs):
        calls.append(1)
        return None, "Groq HTTP 503: upstream unavailable"

    run_react(AgentContext(GOAL, complete=complete, tools=[]))
    assert len(calls) == 3  # первая попытка + AGENT_PROVIDER_RETRIES=2


# ══════════════ нативный протокол tool_calls ══════════════

def test_native_tool_call_gets_a_matching_tool_message():
    seen = []
    replies = iter([_native_call(), ("Курс — 10,90 сомони [1].", {})])

    def complete(messages, **_kwargs):
        seen.append([dict(item) for item in messages])
        return next(replies)

    spec = _search_spec()
    result = run_react(AgentContext(GOAL, complete=complete, tools=[spec],
                                    call_tool=lambda name, **kwargs: spec.call(**kwargs)))
    assert result.answer.startswith("Курс")
    assistant = next(item for item in seen[1] if item.get("role") == "assistant")
    tool_message = next(item for item in seen[1] if item.get("role") == "tool")
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert tool_message["tool_call_id"] == "call_1"
    assert tool_message["name"] == "web_search"
    assert "nbt.tj" not in tool_message["content"] or "untrusted" in tool_message["content"]


def test_json_protocol_keeps_the_user_observation_turn():
    """Без нативного tool calling второй ход остаётся обычным user-сообщением."""
    seen = []
    replies = iter([_json_call(), ("Готово.", {})])

    def complete(messages, **_kwargs):
        seen.append([dict(item) for item in messages])
        return next(replies)

    spec = _search_spec()
    run_react(AgentContext(GOAL, complete=complete, tools=[spec],
                           call_tool=lambda name, **kwargs: spec.call(**kwargs)))
    assert all(item.get("role") != "tool" for item in seen[1])
    assert any("Observation from tool web_search" in str(item.get("content"))
               for item in seen[1])


# ══════════════ «Источники» в разных видах ══════════════

@pytest.mark.parametrize("tail", [
    "**Источники:**", "Источники: [1], [2], [3]", "Источники (1–3):",
    "_Sources:_", "## Источники [1][2]", "**Итог:**", "См. также:",
])
def test_any_bare_announcement_tail_is_removed(tail):
    text = "Курс доллара — 10,90 сомони.\n\n" + tail
    cleaned = clean_final_answer(text, GOAL)
    assert cleaned == "Курс доллара — 10,90 сомони."


def test_ordinary_prose_ending_with_a_colon_is_not_mistaken_for_a_label():
    text = "Какие бывают источники энергии:\n\n- гидроэнергия\n- солнечная энергия"
    assert clean_final_answer(text, "какие бывают источники энергии") == text
