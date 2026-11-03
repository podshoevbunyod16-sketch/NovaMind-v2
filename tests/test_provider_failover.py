"""Provider/model switching is deliberately disabled for chat requests."""
import ai_providers
import config


def test_sync_completion_uses_only_selected_provider_and_model(monkeypatch):
    monkeypatch.setattr(ai_providers, "resolve_target", lambda *a, **k: (
        "groq", "openai/gpt-oss-120b", config.PROVIDERS["groq"]))
    calls = []

    def fake_once(messages, provider=None, model=None, **kwargs):
        calls.append((provider, model))
        return None, "Groq HTTP 429: rate limit"

    monkeypatch.setattr(ai_providers, "_chat_completion_once", fake_once)
    text, error = ai_providers.chat_completion([{"role": "user", "content": "вопрос"}])

    assert text is None
    assert "429" in error
    assert calls == [("groq", "openai/gpt-oss-120b")]


def test_paid_selected_model_is_not_replaced(monkeypatch):
    monkeypatch.setattr(ai_providers, "resolve_target", lambda *a, **k: (
        "openrouter", "vendor/paid-model", config.PROVIDERS["openrouter"]))
    calls = []
    monkeypatch.setattr(ai_providers, "_chat_completion_once", lambda *a, **k: calls.append(k))

    text, error = ai_providers.chat_completion([{"role": "user", "content": "вопрос"}])

    assert text is None
    assert "Автоматическая замена модели отключена" in error
    assert calls == []


def test_stream_uses_only_selected_provider_even_before_first_token(monkeypatch):
    monkeypatch.setattr(ai_providers, "resolve_target", lambda *a, **k: (
        "groq", "openai/gpt-oss-120b", config.PROVIDERS["groq"]))
    calls = []

    def fake_stream(messages, provider=None, model=None, **kwargs):
        calls.append((provider, model))
        yield "error", "connection reset"
        yield "done", ""

    monkeypatch.setattr(ai_providers, "_chat_stream_once", fake_stream)
    events = list(ai_providers.chat_stream([{"role": "user", "content": "вопрос"}]))

    assert calls == [("groq", "openai/gpt-oss-120b")]
    assert events[0] == ("error", "connection reset")
    assert events[-1] == ("done", "")


def test_stream_does_not_switch_after_partial_answer(monkeypatch):
    monkeypatch.setattr(ai_providers, "resolve_target", lambda *a, **k: (
        "groq", "openai/gpt-oss-120b", config.PROVIDERS["groq"]))
    calls = []

    def fake_stream(messages, provider=None, model=None, **kwargs):
        calls.append(provider)
        yield "token", "часть ответа"
        yield "error", "stream interrupted"
        yield "done", ""

    monkeypatch.setattr(ai_providers, "_chat_stream_once", fake_stream)
    events = list(ai_providers.chat_stream([{"role": "user", "content": "вопрос"}]))

    assert calls == ["groq"]
    assert ("token", "часть ответа") in events
    assert any(kind == "error" for kind, _value in events)


def test_resolve_target_keeps_selected_provider_without_credentials(monkeypatch):
    monkeypatch.setattr(config, "has_credentials", lambda _provider: False)
    provider, model, _cfg = ai_providers.resolve_target("groq", "openai/gpt-oss-120b")
    assert (provider, model) == ("groq", "openai/gpt-oss-120b")


def test_free_failover_targets_returns_only_selected_free_target(monkeypatch):
    monkeypatch.setattr(ai_providers, "resolve_target", lambda *a, **k: (
        "groq", "openai/gpt-oss-120b", config.PROVIDERS["groq"]))
    assert ai_providers.free_failover_targets() == [("groq", "openai/gpt-oss-120b")]
