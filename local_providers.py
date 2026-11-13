"""
local_providers.py — first-class support for local model runtimes.

NovaMind talks to local models over the **same OpenAI-compatible protocol**
that Groq, OpenRouter and Cerebras speak. That makes parity possible, but a
local server still needs a few things hosted providers give you for free:

* **presets and endpoint discovery** — Ollama, LM Studio, llama.cpp
  (``llama-server``), vLLM, LocalAI, Jan, KoboldCpp and text-generation-webui
  all listen on a different port and expose a different catalogue endpoint;
* **a real catalogue** — context window, family, parameter count, quantization
  and the loaded/unloaded state, not just a bare model id;
* **capabilities** — tool calling, vision, thinking/reasoning, JSON mode and
  embeddings, read from the server when it advertises them and inferred from
  the model name otherwise;
* **hosted-provider parameters** — streaming, ``tools``/``tool_choice``,
  ``response_format``, ``top_p``, ``seed``, ``stop``, penalties and reasoning
  controls, all opt-in and validated against the model;
* **one stream parser** — SSE (``data: {...}``), newline-delimited JSON and
  Ollama's native chat chunks are accepted, including reasoning deltas and
  streamed tool calls;
* **graceful degradation** — when a server rejects ``tools`` or an optional
  parameter, the request is retried once without the offending field instead
  of failing the whole chat.

The module never mutates global state except its discovery cache; wiring into
requests happens in ``ai_providers.py``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

import requests

logger = logging.getLogger("novamind.local")

# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------

#: Local runtimes NovaMind knows out of the box. ``catalog`` lists the paths
#: that may contain the model list, in the order worth trying.
PRESETS: dict[str, dict] = {
    "ollama": {
        "name": "Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "ports": (11434,),
        "catalog": ("/api/tags", "/v1/models"),
        "hint": "Ollama keeps models in ~/.ollama and can pull them with `ollama pull`.",
        "native_tools": True,
        "reasoning_param": "think",
    },
    "lmstudio": {
        "name": "LM Studio",
        "base_url": "http://127.0.0.1:1234/v1",
        "ports": (1234,),
        "catalog": ("/api/v0/models", "/v1/models"),
        "hint": "Start the LM Studio local server (Developer tab) and load a model.",
        "native_tools": True,
        "reasoning_param": "chat_template_kwargs",
    },
    "llamacpp": {
        "name": "llama.cpp (llama-server)",
        "base_url": "http://127.0.0.1:8080/v1",
        "ports": (8080, 8001),
        "catalog": ("/v1/models", "/props"),
        "hint": "Run `llama-server -m model.gguf --port 8080 -c 8192`.",
        "native_tools": True,
        "reasoning_param": "reasoning_format",
    },
    "vllm": {
        "name": "vLLM",
        "base_url": "http://127.0.0.1:8000/v1",
        "ports": (8000,),
        "catalog": ("/v1/models",),
        "hint": "Run `vllm serve <model> --port 8000 --enable-auto-tool-choice`.",
        "native_tools": True,
        "reasoning_param": "chat_template_kwargs",
    },
    "localai": {
        "name": "LocalAI",
        "base_url": "http://127.0.0.1:8080/v1",
        "ports": (8080,),
        "catalog": ("/v1/models",),
        "hint": "LocalAI serves every model in its models directory on the same port.",
        "native_tools": True,
        "reasoning_param": "",
    },
    "jan": {
        "name": "Jan",
        "base_url": "http://127.0.0.1:1337/v1",
        "ports": (1337,),
        "catalog": ("/v1/models",),
        "hint": "Enable the local API server in Jan → Settings → Local API Server.",
        "native_tools": True,
        "reasoning_param": "",
    },
    "koboldcpp": {
        "name": "KoboldCpp",
        "base_url": "http://127.0.0.1:5001/v1",
        "ports": (5001,),
        "catalog": ("/v1/models",),
        "hint": "Launch KoboldCpp with the OpenAI-compatible API enabled.",
        "native_tools": False,
        "reasoning_param": "",
    },
    "textgenwebui": {
        "name": "text-generation-webui",
        "base_url": "http://127.0.0.1:5000/v1",
        "ports": (5000,),
        "catalog": ("/v1/models",),
        "hint": "Start text-generation-webui with `--api --api-port 5000`.",
        "native_tools": False,
        "reasoning_param": "",
    },
    "openai": {
        "name": "Generic OpenAI-compatible server",
        "base_url": "http://127.0.0.1:8080/v1",
        "ports": (),
        "catalog": ("/v1/models",),
        "hint": "Any server that implements /v1/models and /v1/chat/completions.",
        "native_tools": True,
        "reasoning_param": "",
    },
}

_PRESET_ALIASES = {
    "llama.cpp": "llamacpp", "llama_cpp": "llamacpp", "llama-server": "llamacpp",
    "lms": "lmstudio", "lm-studio": "lmstudio", "lm_studio": "lmstudio",
    "lmstudio": "lmstudio", "ollama": "ollama", "oobabooga": "textgenwebui",
    "textgen": "textgenwebui", "text-generation-webui": "textgenwebui",
    "kobold": "koboldcpp", "openai_compatible": "openai", "auto": "openai",
}

_CATALOG_TTL = float(os.getenv("LOCAL_LLM_CATALOG_TTL", "45") or 45)
_CATALOG_TIMEOUT = float(os.getenv("LOCAL_LLM_CATALOG_TIMEOUT", "4") or 4)
_SHOW_CACHE: dict = {}
_CATALOG_CACHE: dict = {}
_LOCK = threading.Lock()

_SENTENCE_KEYS = ("stop", "seed", "top_p", "presence_penalty", "frequency_penalty",
                  "max_tokens", "n")


# --------------------------------------------------------------------------
# URLs and presets
# --------------------------------------------------------------------------

def normalize_base(url: str | None = None) -> str:
    """Return a clean ``http://host:port/v1`` base URL for a runtime."""
    base = str(url or configured_base()).strip().rstrip("/")
    if not base:
        base = PRESETS["openai"]["base_url"]
    for suffix in ("/chat/completions", "/completions", "/models"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    if base.lower().endswith("/api/tags"):
        base = base[: -len("/api/tags")].rstrip("/") + "/v1"
    if base.lower().endswith("/api"):
        base = base[: -len("/api")].rstrip("/") + "/v1"
    if not re.search(r"/v\d+$", base):
        base += "/v1"
    return base


def root_of(url: str | None = None) -> str:
    """Server root without the ``/v1`` suffix (used by native endpoints)."""
    base = normalize_base(url)
    return re.sub(r"/v\d+$", "", base)


def chat_url(url: str | None = None) -> str:
    return normalize_base(url) + "/chat/completions"


def preset_id(value: str | None = None) -> str:
    key = str(value or "").strip().lower()
    if not key:
        return ""
    key = _PRESET_ALIASES.get(key, key)
    return key if key in PRESETS else ""


def preset_for(url: str | None = None, hint: str = "") -> str:
    """Guess which runtime is behind ``url`` (explicit hint wins)."""
    explicit = preset_id(hint)
    if explicit:
        return explicit
    base = normalize_base(url)
    lowered = base.lower()
    port_match = re.search(r":(\d+)(?:/|$)", lowered)
    port = int(port_match.group(1)) if port_match else 0
    if "11434" in lowered or "ollama" in lowered:
        return "ollama"
    if "1234" in lowered or "lmstudio" in lowered:
        return "lmstudio"
    if "1337" in lowered:
        return "jan"
    if "5001" in lowered:
        return "koboldcpp"
    if "8000" in lowered:
        return "vllm"
    if "8080" in lowered or "8001" in lowered:
        return "llamacpp"
    for name, item in PRESETS.items():
        if port and port in item.get("ports", ()):
            return name
    return "openai"


def preset_info(value: str | None = None) -> dict:
    key = preset_id(value) or "openai"
    item = dict(PRESETS[key])
    item["id"] = key
    item["base_url"] = normalize_base(item.get("base_url"))
    return item


def preset_catalog() -> list[dict]:
    """Every supported runtime, for the settings screen."""
    return [preset_info(key) for key in PRESETS]


def configured_base() -> str:
    """Base URL configured by settings, ``.env`` or the built-in default."""
    try:
        import config
        engine = getattr(config, "local_engine", {}) or {}
        value = str(engine.get("base_url") or "").strip()
        if value:
            return value
    except Exception:
        pass
    for name in ("LOCAL_LLM_URL", "OPENAI_COMPATIBLE_URL", "OPENAI_BASE_URL"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    try:
        import config
        value = str((config.PROVIDERS.get("openai_compatible") or {}).get("url") or "")
        if value:
            return value
    except Exception:
        pass
    return PRESETS["openai"]["base_url"]


def headers(url: str | None = None) -> dict:
    """Auth headers for a local server (most runtimes need none)."""
    try:
        import config
        engine = getattr(config, "local_engine", {}) or {}
        key = str(engine.get("api_key") or "").strip()
    except Exception:
        key = ""
    key = key or os.getenv("LOCAL_LLM_API_KEY", "").strip() \
        or os.getenv("OPENAI_COMPATIBLE_KEY", "").strip() \
        or os.getenv("OPENAI_API_KEY", "").strip() or "ollama"
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def catalog_urls(url: str | None = None) -> list[str]:
    """Catalogue endpoints worth trying for the runtime behind ``url``."""
    base = normalize_base(url)
    preset = preset_for(base)
    root = root_of(base)
    candidates: list[str] = []
    for path in PRESETS.get(preset, {}).get("catalog", ()):
        if path.startswith("/api/") or path == "/props":
            candidates.append(root + path)
        elif path.startswith("/v1/"):
            candidates.append(base + path[len("/v1"):])   # base already ends with /v1
        else:
            candidates.append(base + path)
    # A runtime may listen on a non-standard port: keep the native endpoints as
    # fallbacks so Ollama/LM Studio behind a proxy are still discovered.
    candidates.append(root + "/api/tags")
    candidates.append(root + "/api/v0/models")
    candidates.append(base + "/models")
    candidates.append(root + "/v1/models")
    return list(dict.fromkeys(candidates))


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------

_VISION_HINTS = (
    "llava", "bakllava", "moondream", "vision", "qwen2-vl", "qwen2.5-vl",
    "qwen3-vl", "minicpm-v", "pixtral", "internvl", "molmo", "gemma3",
    "granite-vision", "mistral-small3.1", "llama3.2-vision", "phi-3.5-vision",
    "idefics", "cogvlm", "florence",
)
_REASONING_HINTS = (
    "deepseek-r1", "deepseek-reasoner", "qwq", "qwen3", "gpt-oss", "magistral",
    "reasoning", "reasoner", "thinking", "glm-4.5", "glm-5", "nemotron",
    "phi-4-reasoning", "phi-4-mini-reasoning", "exaone-deep", "granite3.3",
)
_EMBEDDING_HINTS = ("embed", "bge", "e5-", "gte-", "nomic-embed", "voyage", "minilm")


def _has_hint(text: str, hints) -> bool:
    return any(hint in text for hint in hints)


def model_capabilities(model: str, raw: dict | None = None,
                       server: dict | None = None) -> dict:
    """Capabilities of one local model.

    ``server`` may carry values reported by the runtime (Ollama's
    ``capabilities`` array, LM Studio's ``type`` field). When the server is
    silent, capabilities are inferred from the model name; tool calling is
    assumed available because every supported runtime implements it, and the
    chat layer still falls back gracefully if a specific model rejects tools.
    """
    model_id = str(model or "")
    lowered = model_id.lower()
    raw = raw if isinstance(raw, dict) else {}
    server = server if isinstance(server, dict) else {}
    reported = {str(item).lower() for item in (server.get("capabilities") or [])
                if isinstance(item, str)}
    kind = str(server.get("type") or raw.get("type") or "").lower()

    embeddings = bool(_has_hint(lowered, _EMBEDDING_HINTS)) or kind == "embeddings"
    vision = bool(reported & {"vision", "image", "images"}) \
        or _has_hint(lowered, _VISION_HINTS) or kind == "vlm"
    reasoning = bool(reported & {"thinking", "reasoning", "completion+thinking"}) \
        or _has_hint(lowered, _REASONING_HINTS)
    if "tools" in reported:
        tools = True
    elif reported and not embeddings:
        tools = False                      # server listed capabilities and tools were absent
    else:
        # No authoritative list: local servers usually accept `tools`, and the
        # chat layer retries without them if this model does not.
        tools = True
    if embeddings:
        tools, vision = False, False
    return {
        "streaming": True,
        "tools": tools,
        "tool_calling": "native" if tools else "prompt-json",
        "vision": vision,
        "reasoning": reasoning,
        "json_mode": not embeddings,
        "embeddings": embeddings,
        "source": "server" if reported else "model-name",
    }


def _ollama_show(root: str, model: str, timeout: float = 2.5) -> dict:
    """Ask Ollama for the real capability list of one model (cached)."""
    key = (root, model)
    now = time.time()
    with _LOCK:
        cached = _SHOW_CACHE.get(key)
        if cached and now - cached[0] < 300:
            return cached[1]
    data: dict = {}
    try:
        response = requests.post(f"{root}/api/show", json={"model": model},
                                 headers=headers(), timeout=timeout)
        if response.status_code < 400:
            payload = response.json()
            details = payload.get("details") if isinstance(payload, dict) else {}
            info = payload.get("model_info") if isinstance(payload, dict) else {}
            data = {
                "capabilities": payload.get("capabilities") or [],
                "details": details or {},
                "model_info": info or {},
            }
    except (requests.RequestException, ValueError, TypeError):
        data = {}
    with _LOCK:
        _SHOW_CACHE[key] = (now, data)
    return data


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------

def _clean_model_entry(item: dict, preset: str, base: str) -> dict | None:
    if not isinstance(item, dict):
        return None
    model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
    if not model_id:
        return None
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    family = str(item.get("arch") or details.get("family") or "").strip()
    parameter_size = str(details.get("parameter_size") or item.get("parameter_size") or "")
    quantization = str(item.get("quantization") or details.get("quantization_level") or "")
    size_bytes = item.get("size") or item.get("size_bytes") or 0
    context = (item.get("max_context_length") or item.get("context_length")
               or item.get("loaded_context_length") or item.get("context_window") or 0)
    try:
        context = int(context or 0)
    except (TypeError, ValueError):
        context = 0
    state = str(item.get("state") or "").lower()
    server = {
        "capabilities": item.get("capabilities") or [],
        "type": item.get("type") or "",
    }
    caps = model_capabilities(model_id, item, server)
    return {
        "id": model_id,
        "name": item.get("name") or model_id,
        "family": family,
        "parameter_size": parameter_size,
        "quantization": quantization,
        "size_bytes": int(size_bytes or 0),
        "context_length": context,
        "loaded": state == "loaded" or bool(item.get("loaded")),
        "preset": preset,
        "server": base,
        "capabilities": caps,
        "description": _describe(model_id, family, parameter_size, quantization, caps),
    }


def _describe(model_id: str, family: str, params: str, quant: str, caps: dict) -> str:
    parts = [family or "local model"]
    if params:
        parts.append(params)
    if quant:
        parts.append(quant)
    badges = []
    if caps.get("tools"):
        badges.append("tools")
    if caps.get("vision"):
        badges.append("vision")
    if caps.get("reasoning"):
        badges.append("reasoning")
    if caps.get("embeddings"):
        badges.append("embeddings")
    text = " · ".join(parts)
    return f"{text} ({', '.join(badges)})" if badges else text


def _parse_catalog(payload, preset: str, base: str) -> list[dict]:
    """Parse any catalogue dialect into normalised entries."""
    items: list = []
    if isinstance(payload, dict):
        for key in ("data", "models"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    elif isinstance(payload, list):
        items = payload
    entries = []
    for item in items:
        entry = _clean_model_entry(item, preset, base)
        if entry:
            entries.append(entry)
    return entries


def discover(url: str | None = None, *, force: bool = False,
             timeout: float | None = None, deep: bool | None = None) -> dict:
    """Return ``{"base", "preset", "models", "errors", "ok"}`` for a runtime."""
    base = normalize_base(url)
    preset = preset_for(base)
    now = time.time()
    if not force:
        with _LOCK:
            cached = _CATALOG_CACHE.get(base)
        if cached and now - cached["at"] < _CATALOG_TTL:
            return cached["value"]

    timeout = _CATALOG_TIMEOUT if timeout is None else timeout
    if deep is None:
        deep = os.getenv("LOCAL_LLM_DEEP_SCAN", "0") == "1"
    errors: list[str] = []
    models: list[dict] = []
    source_url = ""
    for candidate in catalog_urls(base):
        try:
            response = requests.get(candidate, headers=headers(base), timeout=timeout)
            if response.status_code >= 400:
                errors.append(f"{candidate}: HTTP {response.status_code}")
                continue
            entries = _parse_catalog(response.json(), preset, base)
            if not entries:
                errors.append(f"{candidate}: empty catalogue")
                continue
            models = entries
            source_url = candidate
            if candidate.endswith("/api/tags"):
                preset = "ollama"
            elif candidate.endswith("/api/v0/models"):
                preset = "lmstudio"
            break
        except (requests.RequestException, ValueError, TypeError) as exc:
            errors.append(f"{candidate}: {exc.__class__.__name__}")
            continue

    # A generic /v1/models answer carries no capabilities. When the runtime is
    # Ollama on a non-standard port, its native /api/tags catalogue is richer
    # (family, quantisation, size, and capabilities via /api/show), so prefer it.
    if models and preset != "ollama":
        native = root_of(base) + "/api/tags"
        try:
            response = requests.get(native, headers=headers(base), timeout=timeout)
            if response.status_code < 400:
                native_entries = _parse_catalog(response.json(), "ollama", base)
                if native_entries:
                    models = native_entries
                    preset = "ollama"
                    source_url = native
        except (requests.RequestException, ValueError, TypeError):
            pass

    if models and preset == "ollama":
        root = root_of(base)
        for entry in (models if deep else []):
            info = _ollama_show(root, entry["id"])
            if info:
                entry["capabilities"] = model_capabilities(entry["id"], entry, info)
                details = info.get("details") or {}
                entry["family"] = entry["family"] or str(details.get("family") or "")
                entry["parameter_size"] = entry["parameter_size"] or str(
                    details.get("parameter_size") or "")
                entry["quantization"] = entry["quantization"] or str(
                    details.get("quantization_level") or "")
                entry["description"] = _describe(
                    entry["id"], entry["family"], entry["parameter_size"],
                    entry["quantization"], entry["capabilities"])
    value = {"base": base, "preset": preset, "models": models,
             "errors": errors, "ok": bool(models)}
    with _LOCK:
        _CATALOG_CACHE[base] = {"at": now, "value": value}
    if not models:
        logger.info("local catalogue empty (%s): %s", base, "; ".join(errors[-2:]))
    return value


def capabilities_for(model: str, url: str | None = None) -> dict:
    """Capabilities of ``model`` according to the last successful discovery."""
    base = normalize_base(url)
    with _LOCK:
        cached = _CATALOG_CACHE.get(base)
    if cached:
        for entry in cached["value"].get("models") or []:
            if entry.get("id") == model:
                return dict(entry.get("capabilities") or {})
    if preset_for(base) == "ollama" and os.getenv("LOCAL_LLM_DEEP_SCAN", "0") == "1":
        info = _ollama_show(root_of(base), model)
        if info:
            return model_capabilities(model, {}, info)
    return model_capabilities(model)


def health(url: str | None = None, *, force: bool = False, timeout: float = 2.5) -> dict:
    """Is the local server alive and which models does it serve?"""
    base = normalize_base(url)
    started = time.time()
    result = discover(base, force=force, timeout=timeout)
    return {
        "reachable": bool(result["ok"]),
        "base_url": base,
        "preset": result["preset"],
        "preset_name": preset_info(result["preset"])["name"],
        "models": len(result["models"]),
        "loaded": sum(1 for item in result["models"] if item.get("loaded")),
        "errors": result["errors"][-3:],
        "latency_ms": int((time.time() - started) * 1000),
    }


# --------------------------------------------------------------------------
# Request options
# --------------------------------------------------------------------------

def _env_number(name: str):
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    for cast in (int, float):
        try:
            return cast(raw)
        except (TypeError, ValueError):
            continue
    return None


def json_mode_payload(name: str | None = None, schema: dict | None = None) -> dict:
    """``response_format`` for local servers that support structured output."""
    if schema:
        return {"response_format": {
            "type": "json_schema",
            "json_schema": {"name": name or "novamind_response", "schema": schema,
                            "strict": False},
        }}
    return {"response_format": {"type": "json_object"}}


def reasoning_options(model: str, enabled: bool, url: str | None = None) -> dict:
    """Reasoning knobs the current runtime understands (empty when unknown)."""
    if not enabled:
        return {}
    preset = preset_for(url)
    mode = os.getenv("LOCAL_LLM_REASONING", "auto").strip().lower()
    if mode in ("0", "off", "none", "false"):
        return {}
    caps = capabilities_for(model, url)
    if mode == "auto" and not caps.get("reasoning"):
        return {}
    param = PRESETS.get(preset, {}).get("reasoning_param") or ""
    if param == "reasoning_format":
        # llama-server understands this and ignores it for plain models.
        return {"reasoning_format": "auto"}
    if param == "chat_template_kwargs":
        return {"chat_template_kwargs": {"enable_thinking": True}}
    if param == "think":
        return {"think": True}
    return {}


def request_options(model: str, *, url: str | None = None, extra: dict | None = None,
                    reasoning: bool = False, json_schema: dict | None = None,
                    json_mode: bool = False) -> dict:
    """Optional OpenAI-compatible parameters for a local request.

    Everything is opt-in or derived from the model name, so a server that
    supports only the basics keeps working.
    """
    options: dict = {}
    top_p = _env_number("LOCAL_LLM_TOP_P")
    if top_p is not None:
        options["top_p"] = max(0.0, min(1.0, float(top_p)))
    seed = _env_number("LOCAL_LLM_SEED")
    if seed is not None:
        options["seed"] = int(seed)
    for env_name, field in (("LOCAL_LLM_PRESENCE_PENALTY", "presence_penalty"),
                            ("LOCAL_LLM_FREQUENCY_PENALTY", "frequency_penalty")):
        value = _env_number(env_name)
        if value is not None:
            options[field] = float(value)
    stops = os.getenv("LOCAL_LLM_STOP", "").strip()
    if stops:
        # Stop tokens such as <|im_end|> contain "|", so "||" separates them.
        separator = "||" if "||" in stops else ("\n" if "\n" in stops else ",")
        options["stop"] = [item.strip() for item in stops.split(separator) if item.strip()]
    if json_schema:
        options.update(json_mode_payload(model, json_schema))
    elif json_mode:
        options.update(json_mode_payload(model))
    if reasoning:
        options.update(reasoning_options(model, True, url))
    caps = capabilities_for(model, url)
    if caps.get("embeddings"):
        options.pop("tools", None)
    for key, value in (extra or {}).items():
        if value is not None:
            options[key] = value
    return options


def tools_supported(model: str, url: str | None = None) -> bool:
    """Should ``tools`` be sent to this local model?"""
    mode = os.getenv("LOCAL_LLM_TOOLS", "auto").strip().lower()
    if mode in ("0", "off", "none", "false"):
        return False
    if mode in ("1", "on", "true", "force"):
        return True
    preset = preset_for(url)
    if not PRESETS.get(preset, {}).get("native_tools", True):
        return False
    caps = capabilities_for(model, url)
    return bool(caps.get("tools", True))


def rejected_fields(error_text: str) -> list[str]:
    """Fields a 400 response complained about (for one retry without them)."""
    text = str(error_text or "").lower()
    if not text:
        return []
    fields = []
    for key in ("tools", "tool_choice", "response_format", "reasoning_format",
                "reasoning_effort", "chat_template_kwargs", "think", "seed",
                "top_p", "presence_penalty", "frequency_penalty", "stop",
                "json_schema", "json_object", "stream_options"):
        if key in text:
            fields.append(key)
    if ("tool" in text and "support" in text) and "tools" not in fields:
        fields.append("tools")
    return fields


def strip_fields(payload: dict, fields: list[str]) -> dict:
    cleaned = dict(payload)
    for field in fields or []:
        cleaned.pop(field, None)
        if field == "json_schema":
            cleaned.pop("response_format", None)
        if field == "tools":
            cleaned.pop("tool_choice", None)
    return cleaned


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------

_DONE = object()


def _absorb_tool_call(store: dict, raw: dict) -> None:
    if not isinstance(raw, dict):
        return
    function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    index = raw.get("index")
    if not isinstance(index, int):
        index = len(store)
    entry = store.setdefault(index, {
        "id": "", "type": "function",
        "function": {"name": "", "arguments": ""},
    })
    if raw.get("id"):
        entry["id"] = str(raw["id"])
    if function.get("name"):
        entry["function"]["name"] = str(function["name"])
    arguments = function.get("arguments")
    if isinstance(arguments, dict):
        entry["function"]["arguments"] = json.dumps(arguments, ensure_ascii=False)
    elif arguments:
        entry["function"]["arguments"] += str(arguments)


def _event_from_payload(chunk: dict, tools: dict) -> dict:
    """Translate one server chunk into NovaMind stream events."""
    event = {"content": "", "reasoning": "", "done": False, "tools": []}
    if not isinstance(chunk, dict):
        return event

    # Ollama native /api/chat
    message = chunk.get("message")
    if isinstance(message, dict):
        event["content"] = message.get("content") or ""
        event["reasoning"] = message.get("thinking") or message.get("reasoning") or ""
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            calls = {index: {"id": str(call.get("id") or f"call_{index}"),
                             "type": "function",
                             "function": {
                                 "name": str((call.get("function") or {}).get("name") or ""),
                                 "arguments": json.dumps(
                                     (call.get("function") or {}).get("arguments") or {},
                                     ensure_ascii=False),
                             }}
                     for index, call in enumerate(raw_calls) if isinstance(call, dict)}
            event["tools"] = list(calls.values())
        event["done"] = bool(chunk.get("done"))
        return event

    # llama.cpp native completion chunks
    if "response" in chunk and not chunk.get("choices"):
        event["content"] = chunk.get("response") or ""
        event["done"] = bool(chunk.get("stop"))
        return event
    if "content" in chunk and not chunk.get("choices"):
        event["content"] = chunk.get("content") or ""
        event["done"] = bool(chunk.get("stop"))
        return event

    choices = chunk.get("choices") or []
    if not choices:
        return event
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
    if not delta and isinstance(choice.get("message"), dict):
        delta = choice["message"]
    event["content"] = delta.get("content") or choice.get("text") or ""
    event["reasoning"] = (delta.get("reasoning") or delta.get("reasoning_content")
                          or delta.get("thinking") or choice.get("thinking") or "")
    if isinstance(event["content"], list):
        event["content"] = "".join(str(part.get("text", "")) for part in event["content"]
                                   if isinstance(part, dict))
    if isinstance(event["reasoning"], list):
        event["reasoning"] = "".join(str(part.get("text", "")) for part in event["reasoning"]
                                     if isinstance(part, dict))
    for raw in delta.get("tool_calls") or []:
        _absorb_tool_call(tools, raw)
    if choice.get("finish_reason") == "tool_calls" and tools:
        event["tools"] = [tools[key] for key in sorted(tools)]
        tools.clear()
    return event


def iter_stream(response):
    """Yield ``(kind, payload)`` from an SSE or NDJSON response.

    kinds: ``token``, ``reasoning``, ``tool_calls``, ``done``.
    Accepts ``data:``-prefixed SSE, bare JSON lines, Ollama chat chunks,
    llama.cpp completion chunks, comments and keep-alives.
    """
    tools: dict = {}
    try:
        for raw in response.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.strip()
            if not line or line.startswith(":") or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line:
                continue
            if line in ("[DONE]", "data: [DONE]"):
                break
            try:
                chunk = json.loads(line)
            except (TypeError, ValueError):
                # Some runtimes emit plain text chunks.
                yield "token", line
                continue
            if isinstance(chunk, dict) and chunk.get("error"):
                message = chunk["error"]
                if isinstance(message, dict):
                    message = message.get("message") or json.dumps(message, ensure_ascii=False)
                yield "error", str(message)[:600]
                return
            event = _event_from_payload(chunk, tools)
            if event["reasoning"]:
                yield "reasoning", str(event["reasoning"])
            if event["tools"]:
                yield "tool_calls", event["tools"]
            if event["content"]:
                yield "token", str(event["content"])
            if event["done"]:
                break
        if tools:
            yield "tool_calls", [tools[key] for key in sorted(tools)]
    finally:
        try:
            response.close()
        except Exception:
            pass
    yield "done", ""


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------

def feature_matrix() -> dict:
    """Which hosted-provider features are available locally, and how."""
    return {
        "streaming": {"supported": True, "note": "SSE and NDJSON dialects are parsed."},
        "tools": {"supported": os.getenv("LOCAL_LLM_TOOLS", "auto"),
                  "note": "Native tool calling with automatic fallback to the JSON protocol."},
        "json_mode": {"supported": True, "note": "response_format json_object / json_schema."},
        "reasoning": {"supported": os.getenv("LOCAL_LLM_REASONING", "auto"),
                      "note": "reasoning deltas are streamed when the runtime provides them."},
        "vision": {"supported": True, "note": "Image parts are passed through to vision models."},
        "catalog": {"supported": True, "note": "Context size, family and capabilities."},
        "usage": {"supported": True, "note": "Token usage is forwarded when the server sends it."},
    }


def status(url: str | None = None, *, force: bool = False) -> dict:
    """Everything the settings screen needs to talk about local models."""
    base = normalize_base(url)
    catalog = discover(base, force=force)
    preset = catalog.get("preset") or preset_for(base)
    return {
        "base_url": base,
        "chat_url": chat_url(base),
        "preset": preset,
        "preset_name": preset_info(preset)["name"],
        "presets": preset_catalog(),
        "reachable": catalog["ok"],
        "models": catalog["models"],
        "errors": catalog["errors"],
        "features": feature_matrix(),
        "options": {
            "top_p": os.getenv("LOCAL_LLM_TOP_P", ""),
            "seed": os.getenv("LOCAL_LLM_SEED", ""),
            "stop": os.getenv("LOCAL_LLM_STOP", ""),
            "tools": os.getenv("LOCAL_LLM_TOOLS", "auto"),
            "reasoning": os.getenv("LOCAL_LLM_REASONING", "auto"),
        },
    }
