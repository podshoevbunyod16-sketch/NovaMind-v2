import config
import routes.commands as commands
import routes.media as media


def test_code_command_uses_selected_provider_and_model(client, monkeypatch):
    captured = {}

    def fake_completion(messages, **kwargs):
        captured.update(kwargs)
        return "print(1)", {"provider": kwargs.get("provider"), "model": kwargs.get("model")}

    monkeypatch.setattr(commands, "chat_completion", fake_completion)
    provider, model = config.current_provider, config.current_model
    response = client.post("/command", json={"command": "/code print(1)"})

    assert response.status_code == 200
    assert response.get_json()["result"] == "print(1)"
    assert captured["provider"] == provider
    assert captured["model"] == model


def test_research_uses_selected_provider_and_bounded_token_budget(client, monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "search_web", lambda query: "Source result")

    def fake_completion(messages, **kwargs):
        calls.append(kwargs)
        return ("Analysis" if len(calls) == 1 else "Final answer"), {"model": kwargs.get("model")}

    monkeypatch.setattr(commands, "chat_completion", fake_completion)
    provider, model = config.current_provider, config.current_model
    response = client.post("/command", json={"command": "/research test question"})

    assert response.status_code == 200
    assert response.get_json()["result"] == "Final answer"
    assert len(calls) == 2
    assert all(call["provider"] == provider and call["model"] == model for call in calls)
    assert all(call["max_tokens"] <= 8192 for call in calls)


def test_media_text_analysis_uses_selected_provider_and_model(monkeypatch):
    captured = {}

    def fake_completion(messages, **kwargs):
        captured.update(kwargs)
        return "analysis", {"provider": kwargs.get("provider"), "model": kwargs.get("model")}

    monkeypatch.setattr(media, "chat_completion", fake_completion)
    provider, model = config.current_provider, config.current_model
    result, error = media._call_selected_text_ai("Summarize this document")

    assert error is None
    assert result == "analysis"
    assert captured["provider"] == provider
    assert captured["model"] == model
