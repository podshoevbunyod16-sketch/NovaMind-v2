"""Regression tests for the Gemini 3.8 API contract and retired-model migration."""
import json

import ai_providers
import config


def test_gemini_38_is_the_configured_free_ai_studio_model():
    models = config.PROVIDERS["google_ai_studio"]["models"]
    assert [item["id"] for item in models] == ["gemini-3.8-flash"]
    assert ai_providers.is_free_ai_model("google_ai_studio", "gemini-3.8-flash")
    assert not ai_providers.is_free_ai_model("google_ai_studio", "gemini-2.0-flash")


def test_retired_gemini_runtime_selection_is_migrated(monkeypatch, tmp_path):
    settings = tmp_path / "runtime_settings.json"
    settings.write_text(json.dumps({
        "provider": "google_ai_studio",
        "model": "gemini-2.0-flash",
    }), encoding="utf-8")
    monkeypatch.setattr(config, "RUNTIME_SETTINGS_FILE", str(settings))
    monkeypatch.setattr(config, "current_provider", "groq")
    monkeypatch.setattr(config, "current_model", "openai/gpt-oss-120b")

    config.load_runtime_settings()

    assert config.current_provider == "google_ai_studio"
    assert config.current_model == "gemini-3.8-flash"


def test_gemini_3x_generation_config_omits_legacy_temperature():
    generation = ai_providers._gemini_generation_config(
        "gemini-3.8-flash", {"temperature": 0.2, "max_tokens": 2048}
    )
    assert generation == {"maxOutputTokens": 2048}


def test_gemini_older_generation_config_keeps_temperature():
    generation = ai_providers._gemini_generation_config(
        "gemini-2.0-flash", {"temperature": 0.2, "max_tokens": 1024}
    )
    assert generation == {"temperature": 0.2, "maxOutputTokens": 1024}


def test_gemini_function_response_preserves_required_call_id():
    _system, contents = ai_providers.gemini_messages_from_openai([
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_lookup_1",
            "type": "function",
            "function": {"name": "search", "arguments": "{\"query\": \"test\"}"},
        }]},
        {"role": "tool", "tool_call_id": "call_lookup_1", "content": "found"},
    ])

    function_call = contents[0]["parts"][0]["functionCall"]
    function_response = contents[1]["parts"][0]["functionResponse"]
    assert function_call["id"] == "call_lookup_1"
    assert function_response["call_id"] == "call_lookup_1"
    assert function_response["name"] == "search"
