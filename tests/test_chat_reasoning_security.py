import json
from typing import ClassVar

import ai_providers
import routes.chat as chat_routes

REASONING_MARKER = "TEST_RAW_REASONING_MARKER"
ANSWER = "Обычный безопасный ответ"


def test_completion_uses_content_only_and_does_not_log_reasoning(monkeypatch, caplog):
    monkeypatch.setattr(
        ai_providers,
        "resolve_target",
        lambda provider=None, model=None: (
            "openai_compatible", "test-model", {"url": "unused", "max_tokens": 32}
        ),
    )
    monkeypatch.setattr(
        ai_providers,
        "_request_local_with_fallback",
        lambda *args, **kwargs: (
            {"choices": [{"message": {
                "content": ANSWER,
                "reasoning": REASONING_MARKER,
                "reasoning_content": REASONING_MARKER,
                "thinking": REASONING_MARKER,
            }}]},
            None,
            None,
        ),
    )

    reply, meta = ai_providers._chat_completion_once(
        [{"role": "user", "content": "Ответь кратко"}],
        provider="openai_compatible",
        model="test-model",
    )

    assert reply == ANSWER
    assert REASONING_MARKER not in reply
    assert REASONING_MARKER not in repr(meta)
    assert REASONING_MARKER not in caplog.text


def test_provider_stream_drops_reasoning_but_keeps_answer(monkeypatch):
    monkeypatch.setattr(
        ai_providers,
        "free_failover_targets",
        lambda provider=None, model=None: [("openai_compatible", "test-model")],
    )
    monkeypatch.setattr(
        ai_providers,
        "resolve_target",
        lambda provider=None, model=None: (
            "openai_compatible", "test-model", {"url": "unused", "max_tokens": 32}
        ),
    )

    def raw_stream(*args, **kwargs):
        yield "reasoning", REASONING_MARKER
        yield "token", ANSWER

    monkeypatch.setattr(ai_providers, "_raw_stream", raw_stream)

    events = list(ai_providers.chat_stream(
        [{"role": "user", "content": "Ответь кратко"}],
        provider="openai_compatible",
        model="test-model",
        reasoning=True,
    ))

    assert ("token", ANSWER) in events
    assert not any(kind == "reasoning" for kind, _chunk in events)
    assert REASONING_MARKER not in repr(events)


def test_gemini_result_drops_thought_parts_but_keeps_answer(monkeypatch):
    class Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {}
        text = ""

        def json(self):
            return {"candidates": [{"content": {"parts": [
                {"thought": True, "text": REASONING_MARKER},
                {"text": ANSWER},
            ]}}]}

    monkeypatch.setattr(ai_providers.requests, "post", lambda *args, **kwargs: Response())

    result = ai_providers._gemini_request_with_key(
        "gemini-test",
        {"messages": [{"role": "user", "content": "Ответь кратко"}]},
        "test-key",
    )

    content = result["choices"][0]["message"]["content"]
    assert content == ANSWER
    assert REASONING_MARKER not in repr(result)


def test_send_stream_has_no_reasoning_field_or_marker(monkeypatch, client):
    monkeypatch.setattr(chat_routes, "_chat_tools_allowed", lambda _data: False)
    monkeypatch.setattr(
        chat_routes,
        "resolve_target",
        lambda: ("openai_compatible", "test-model", {"max_tokens": 32}),
    )
    monkeypatch.setattr(
        chat_routes,
        "chat_stream",
        lambda *args, **kwargs: iter([
            ("reasoning", REASONING_MARKER),
            ("token", ANSWER),
            ("done", ""),
        ]),
    )

    response = client.post(
        "/send_stream",
        json={"message": "Ответь кратко", "reasoning": True},
    )
    body = response.get_data(as_text=True)
    events = [json.loads(line) for line in body.splitlines() if line.strip()]

    assert response.status_code == 200
    assert ANSWER in body
    assert REASONING_MARKER not in body
    assert not any("reasoning" in event for event in events)
    assert events[-1]["done"] is True
