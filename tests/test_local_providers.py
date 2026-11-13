"""Локальные модели: каталог, возможности, параметры и разбор потока.

NovaMind общается с локальными рантаймами по OpenAI-совместимому протоколу.
Тесты проверяют, что локальная модель получает те же возможности, что и
облачная: поток, инструменты, JSON-режим, рассуждения, каталог с контекстом.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import ai_providers
import config
import language_policy
import local_providers


# ─────────────────────────── пресеты и адреса ───────────────────────────

def test_normalize_base_accepts_every_documented_form():
    assert local_providers.normalize_base("http://127.0.0.1:11434") == \
        "http://127.0.0.1:11434/v1"
    assert local_providers.normalize_base("http://127.0.0.1:8080/v1/chat/completions") == \
        "http://127.0.0.1:8080/v1"
    assert local_providers.normalize_base("http://127.0.0.1:11434/api/tags") == \
        "http://127.0.0.1:11434/v1"
    assert local_providers.chat_url("http://127.0.0.1:1234/v1") == \
        "http://127.0.0.1:1234/v1/chat/completions"


def test_preset_detection_by_port_and_hint():
    assert local_providers.preset_for("http://127.0.0.1:11434/v1") == "ollama"
    assert local_providers.preset_for("http://127.0.0.1:1234/v1") == "lmstudio"
    assert local_providers.preset_for("http://127.0.0.1:8000/v1") == "vllm"
    assert local_providers.preset_for("http://127.0.0.1:9000/v1", hint="llamacpp") == "llamacpp"
    assert local_providers.preset_for("http://127.0.0.1:9000/v1") == "openai"


def test_catalog_urls_prefer_native_endpoints():
    ollama = local_providers.catalog_urls("http://127.0.0.1:11434/v1")
    assert ollama[0] == "http://127.0.0.1:11434/api/tags"
    assert "http://127.0.0.1:11434/v1/models" in ollama

    lmstudio = local_providers.catalog_urls("http://127.0.0.1:1234/v1")
    assert lmstudio[0] == "http://127.0.0.1:1234/api/v0/models"

    generic = local_providers.catalog_urls("http://127.0.0.1:4321/v1")
    assert generic[0] == "http://127.0.0.1:4321/v1/models"


def test_preset_catalog_is_exposed_for_settings():
    catalog = local_providers.preset_catalog()
    ids = {item["id"] for item in catalog}
    assert {"ollama", "lmstudio", "llamacpp", "vllm", "localai"} <= ids
    assert all(item["base_url"].startswith("http") for item in catalog)


# ─────────────────────────── возможности моделей ───────────────────────────

def test_capabilities_inferred_from_model_name():
    vision = local_providers.model_capabilities("llava:13b")
    assert vision["vision"] is True

    reasoning = local_providers.model_capabilities("deepseek-r1:8b")
    assert reasoning["reasoning"] is True

    embedding = local_providers.model_capabilities("nomic-embed-text")
    assert embedding["embeddings"] is True and embedding["tools"] is False

    plain = local_providers.model_capabilities("llama3.2:3b")
    assert plain["streaming"] is True and plain["tools"] is True
    assert plain["vision"] is False


def test_capabilities_reported_by_server_win():
    caps = local_providers.model_capabilities(
        "some-model", {}, {"capabilities": ["completion"], "type": ""})
    assert caps["tools"] is False               # сервер не объявил tools
    caps = local_providers.model_capabilities(
        "some-model", {}, {"capabilities": ["completion", "tools", "vision", "thinking"]})
    assert caps["tools"] and caps["vision"] and caps["reasoning"]
    assert caps["source"] == "server"


# ─────────────────────────── параметры запроса ───────────────────────────

def test_request_options_are_opt_in(monkeypatch):
    for name in ("LOCAL_LLM_TOP_P", "LOCAL_LLM_SEED", "LOCAL_LLM_STOP",
                 "LOCAL_LLM_PRESENCE_PENALTY", "LOCAL_LLM_FREQUENCY_PENALTY"):
        monkeypatch.delenv(name, raising=False)
    assert local_providers.request_options("llama3.2") == {}

    monkeypatch.setenv("LOCAL_LLM_TOP_P", "0.9")
    monkeypatch.setenv("LOCAL_LLM_SEED", "7")
    monkeypatch.setenv("LOCAL_LLM_STOP", "</s>||<|im_end|>")
    options = local_providers.request_options("llama3.2")
    assert options["top_p"] == 0.9 and options["seed"] == 7
    assert options["stop"] == ["</s>", "<|im_end|>"]


def test_json_mode_payload_shapes():
    assert local_providers.json_mode_payload() == {
        "response_format": {"type": "json_object"}}
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    payload = local_providers.json_mode_payload("answer", schema)
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"] == schema


def test_reject_helpers_strip_only_the_named_field():
    error = "HTTP 400: tools are not supported by this model"
    fields = local_providers.rejected_fields(error)
    assert "tools" in fields
    payload = {"model": "m", "tools": [{"x": 1}], "tool_choice": "auto",
               "response_format": {"type": "json_object"}}
    stripped = local_providers.strip_fields(payload, fields)
    assert "tools" not in stripped and "tool_choice" not in stripped
    assert stripped["response_format"] == {"type": "json_object"}


def test_tools_follow_env_switch(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_TOOLS", "off")
    assert local_providers.tools_supported("qwen3:8b") is False
    assert ai_providers.supports_native_tools("openai_compatible", "qwen3:8b") is False
    monkeypatch.setenv("LOCAL_LLM_TOOLS", "on")
    assert local_providers.tools_supported("llama3.2") is True


# ─────────────────────────── разбор потока ───────────────────────────

class FakeResponse:
    def __init__(self, lines):
        self.lines = lines
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        for line in self.lines:
            yield line

    def close(self):
        self.closed = True


def test_iter_stream_parses_sse_reasoning_and_content():
    response = FakeResponse([
        'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}',
        'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ])
    events = list(local_providers.iter_stream(response))
    assert events[0] == ("reasoning", "thinking")
    assert ("token", "Hello") in events and ("token", " world") in events
    assert events[-1] == ("done", "")
    assert response.closed is True


def test_iter_stream_parses_ollama_native_chunks():
    response = FakeResponse([
        json.dumps({"message": {"role": "assistant", "content": "Привет",
                                "thinking": "думаю"}, "done": False}),
        json.dumps({"message": {"content": "!"}, "done": True}),
    ])
    events = list(local_providers.iter_stream(response))
    assert ("reasoning", "думаю") in events
    assert ("token", "Привет") in events and ("token", "!") in events


def test_iter_stream_aggregates_tool_call_fragments():
    response = FakeResponse([
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"web_search","arguments":"{\\"query\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":" \\"asyncio\\"}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ])
    events = list(local_providers.iter_stream(response))
    calls = [payload for kind, payload in events if kind == "tool_calls"]
    assert calls and calls[0][0]["function"]["name"] == "web_search"
    assert json.loads(calls[0][0]["function"]["arguments"]) == {"query": "asyncio"}


def test_iter_stream_reports_server_error():
    response = FakeResponse(['data: {"error":{"message":"model not loaded"}}'])
    events = list(local_providers.iter_stream(response))
    assert events[0][0] == "error" and "model not loaded" in events[0][1]


# ─────────────────────────── каталог через HTTP ───────────────────────────

class CatalogHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen_show = []

    def log_message(self, *args):
        pass

    def _send(self, body, status=200):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/tags":
            self._send(json.dumps({"models": [
                {"name": "qwen3:8b", "model": "qwen3:8b", "size": 5_000_000_000,
                 "details": {"family": "qwen3", "parameter_size": "8B",
                             "quantization_level": "Q4_K_M"}},
                {"name": "llava:7b", "model": "llava:7b",
                 "details": {"family": "llama", "parameter_size": "7B"}},
            ]}))
            return
        if path == "/v1/models":
            self._send(json.dumps({"data": [{"id": "qwen3:8b"}]}))
            return
        self._send("{}", status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/show":
            CatalogHandler.seen_show.append(payload)
            self._send(json.dumps({
                "capabilities": ["completion", "tools", "thinking"],
                "details": {"family": "qwen3", "parameter_size": "8B"},
            }))
            return
        self._send("{}", status=404)


@pytest.fixture()
def ollama_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def test_discover_reads_ollama_catalogue(ollama_server, monkeypatch):
    local_providers._CATALOG_CACHE.clear()
    monkeypatch.setenv("LOCAL_LLM_DEEP_SCAN", "1")
    result = local_providers.discover(ollama_server, force=True)
    assert result["ok"] is True and result["preset"] == "ollama"
    models = {item["id"]: item for item in result["models"]}
    assert set(models) == {"qwen3:8b", "llava:7b"}
    assert models["qwen3:8b"]["context_length"] == 0
    assert models["qwen3:8b"]["family"] == "qwen3"
    assert models["qwen3:8b"]["parameter_size"] == "8B"
    # Возможности из /api/show имеют приоритет над догадкой по имени.
    assert models["qwen3:8b"]["capabilities"]["tools"] is True
    assert models["llava:7b"]["capabilities"]["vision"] is True
    assert CatalogHandler.seen_show


def test_fetch_available_models_uses_local_catalogue(ollama_server, monkeypatch):
    local_providers._CATALOG_CACHE.clear()
    monkeypatch.setattr(local_providers, "configured_base", lambda: ollama_server)
    monkeypatch.setitem(config.PROVIDERS, "openai_compatible", {
        **config.PROVIDERS["openai_compatible"], "url": ollama_server + "/chat/completions"})
    config.MODEL_CACHE.pop("openai_compatible", None)
    models = ai_providers.fetch_available_models("openai_compatible", force=True)
    ids = [item["id"] for item in models]
    assert ids == ["qwen3:8b", "llava:7b"]
    first = models[0]
    assert first["local"] is True
    assert first["capabilities"]["tools"] is True
    assert first["family"] == "qwen3"


# ─────────────────────────── запросы через общий слой ───────────────────────────

def test_local_request_carries_language_and_options(monkeypatch):
    captured = {}

    def fake_request(url, payload, headers, timeout=90, max_retries=2):
        captured["payload"] = payload
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}, None

    monkeypatch.setattr(ai_providers, "openai_compatible_request", fake_request)
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda *a, **k: ("openai_compatible", "qwen3:8b",
                                         {"url": "http://local/v1/chat/completions",
                                          "max_tokens": 256}))
    monkeypatch.setenv("LOCAL_LLM_TOP_P", "0.8")
    with language_policy.use_language("ru"):
        text, meta = ai_providers.chat_completion(
            [{"role": "user", "content": "привет"}], system="You are helpful.")
    assert text == "ok"
    payload = captured["payload"]
    assert payload["top_p"] == 0.8
    assert "Russian" in payload["messages"][0]["content"]
    assert meta["answer_language"] == "ru"
    assert meta["local"] is True


def test_tools_are_dropped_when_the_local_server_rejects_them(monkeypatch):
    calls = []

    def fake_request(url, payload, headers, timeout=90, max_retries=2):
        calls.append(dict(payload))
        if "tools" in payload:
            return None, 'OpenAI-compatible сервер: HTTP 400: "tools" is not supported'
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}, None

    monkeypatch.setattr(ai_providers, "openai_compatible_request", fake_request)
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda *a, **k: ("openai_compatible", "plain-llm",
                                         {"url": "http://local/v1/chat/completions",
                                          "max_tokens": 128}))
    text, meta = ai_providers.chat_completion(
        [{"role": "user", "content": "search"}],
        tools=[{"type": "function", "function": {"name": "web_search",
                                                 "parameters": {"type": "object"}}}])
    assert text == "ok"
    assert len(calls) == 2
    assert "tools" in calls[0] and "tools" not in calls[1]
    assert meta["native_tools"] is False
    assert "tools" in meta["capabilities"]["degraded"]


def test_stream_uses_one_parser_for_ollama_dialect(monkeypatch):
    class StreamResponse:
        status_code = 200
        encoding = "utf-8"
        headers = {}

        def iter_lines(self, decode_unicode=True):
            yield json.dumps({"message": {"content": "Привет",
                                          "thinking": "размышляю"}, "done": False})
            yield json.dumps({"message": {"content": "!"}, "done": True})

        def close(self):
            pass

    monkeypatch.setattr(ai_providers, "_managed_stream_request",
                        lambda *a, **k: (StreamResponse(), None))
    monkeypatch.setattr(ai_providers, "resolve_target",
                        lambda *a, **k: ("openai_compatible", "qwen3:8b",
                                         {"url": "http://local/v1/chat/completions",
                                          "max_tokens": 128}))
    events = list(ai_providers._raw_stream(
        "openai_compatible", "qwen3:8b",
        {"model": "qwen3:8b", "messages": []}, {"url": "http://local/v1/chat/completions"}, 10))
    assert ("reasoning", "размышляю") in events
    assert ("token", "Привет") in events and ("token", "!") in events


def test_local_status_endpoint_lists_presets(admin_client, monkeypatch):
    monkeypatch.setattr(local_providers, "discover",
                        lambda *a, **k: {"base": "http://127.0.0.1:11434/v1",
                                         "preset": "ollama", "models": [], "errors": [],
                                         "ok": False})
    data = admin_client.get("/api/settings/local").get_json()
    assert data["preset"] == "ollama"
    assert any(item["id"] == "ollama" for item in data["presets"])
    assert data["features"]["streaming"]["supported"] is True
    assert "engine" in data and "has_key" in data["engine"]


def test_local_settings_save_updates_provider_url(admin_client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RUNTIME_SETTINGS_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setattr(local_providers, "discover",
                        lambda *a, **k: {"base": "http://127.0.0.1:11434/v1",
                                         "preset": "ollama", "models": [], "errors": [],
                                         "ok": False})
    response = admin_client.post("/api/settings/local", json={
        "preset": "ollama", "base_url": "http://127.0.0.1:11434/v1"})
    data = response.get_json()
    assert response.status_code == 200 and data["success"] is True
    assert config.local_engine["base_url"] == "http://127.0.0.1:11434/v1"
    assert config.PROVIDERS["openai_compatible"]["url"] == \
        "http://127.0.0.1:11434/v1/chat/completions"
    # Секрет наружу не отдаётся.
    assert "api_key" not in data["engine"]


def test_local_settings_reject_unknown_preset(admin_client):
    response = admin_client.post("/api/settings/local", json={"preset": "definitely-not-a-runtime"})
    assert response.status_code == 400
    body = response.get_json()
    assert "definitely-not-a-runtime" in body["error"]
    assert "ollama" in body["presets"]


def test_local_settings_accept_preset_alias(admin_client, monkeypatch):
    monkeypatch.setattr(local_providers, "discover",
                        lambda *a, **k: {"base": "http://127.0.0.1:1234/v1", "preset": "lmstudio",
                                         "models": [], "errors": [], "ok": False})
    response = admin_client.post("/api/settings/local", json={"preset": "lm-studio"})
    assert response.status_code == 200
    assert response.get_json()["engine"]["base_url"] == "http://127.0.0.1:1234/v1"
