"""
ai_providers.py — Запросы к AI провайдерам (Groq, Gemini, OpenAI-compatible)
"""
import os
import json
import time
import re
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests
from groq_rotation import get_groq_key, mark_groq_key_exhausted, GROQ_KEY_COOLDOWN, mark_groq_key_success
import provider_manager as pm
import language_policy
import local_providers

logger = logging.getLogger(__name__)

def openai_compatible_request(url, payload, headers, timeout=90, max_retries=2):
    """Запрос к OpenAI-compatible серверу; HTTP-ошибки разбираются по status_code.

    Разные классы ошибок — разная стратегия: 401/403/404/billing не повторяются,
    429 ждёт Retry-After, 5xx/timeout — backoff с jitter, число попыток конечно.
    """
    for attempt in range(max(1, int(max_retries))):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.status_code >= 400:
                kind, retry_after = pm.classify_response(resp.status_code, resp.headers, resp.text)
                detail = pm.redact((resp.text or '')[:500])
                if kind == pm.ErrorKind.RATE_LIMIT and attempt < max_retries - 1:
                    time.sleep(pm.backoff_delay(attempt, retry_after))
                    continue
                if kind in (pm.ErrorKind.SERVER, pm.ErrorKind.TIMEOUT, pm.ErrorKind.NETWORK) \
                        and attempt < max_retries - 1:
                    time.sleep(pm.backoff_delay(attempt, retry_after))
                    continue
                labels = {
                    pm.ErrorKind.AUTH: "API ключ отклонён (401)",
                    pm.ErrorKind.FORBIDDEN: "доступ запрещён (403)",
                    pm.ErrorKind.NOT_FOUND: "ресурс или модель не найдены (404)",
                    pm.ErrorKind.RATE_LIMIT: "ограничение частоты запросов (429)",
                    pm.ErrorKind.QUOTA: "исчерпана квота",
                    pm.ErrorKind.BILLING: "проблема с оплатой / тарифом",
                    pm.ErrorKind.SERVER: f"ошибка сервера (HTTP {resp.status_code})",
                }
                label = labels.get(kind, f"HTTP {resp.status_code}")
                pm.log_event("warning", "provider_http_error", kind=kind.value,
                             reason=f"{label}: {detail}", attempt=attempt + 1)
                return None, f"OpenAI-compatible сервер: {label}: {detail}"
            return resp.json(), None
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(pm.backoff_delay(attempt))
                continue
            return None, "Таймаут OpenAI-compatible сервера"
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            if status == 429 and attempt < max_retries - 1:
                time.sleep(pm.backoff_delay(attempt))
                continue
            return None, f"OpenAI-compatible сервер вернул HTTP {status}: {pm.redact(str(exc)[:500])}"
        except requests.exceptions.RequestException as exc:
            if attempt < max_retries - 1:
                time.sleep(pm.backoff_delay(attempt))
                continue
            return None, f"OpenAI-compatible сервер недоступен: {exc.__class__.__name__}"
        except ValueError:
            return None, "OpenAI-compatible сервер вернул некорректный JSON"
    return None, "OpenAI-compatible сервер недоступен"


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([hms])", re.IGNORECASE)
_MAX_COOLDOWN_SECONDS = 25 * 3600


def _parse_duration_seconds(value):
    """Parse compound durations such as 2m15.4s, 1h2m or 0.5s."""
    raw = str(value or "").strip().lower()
    parts = _DURATION_PART.findall(raw)
    if not parts:
        return None
    residue = _DURATION_PART.sub("", raw).strip(" ,;")
    if residue:
        return None
    factors = {"h": 3600, "m": 60, "s": 1}
    seconds = sum(float(amount) * factors[unit.lower()] for amount, unit in parts)
    if seconds <= 0:
        return None
    return max(1, min(int(seconds + 0.999), _MAX_COOLDOWN_SECONDS))


def _header_value(headers, name):
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return value
    return None


def _reset_value_seconds(value):
    duration = _parse_duration_seconds(value)
    if duration is not None:
        return duration
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if number > 1_000_000_000:
        number -= time.time()  # Unix timestamp
    if number <= 0:
        return None
    return max(1, min(int(number + 0.999), _MAX_COOLDOWN_SECONDS))


def _retry_after_seconds(headers, default=60):
    """Return the longest valid Retry-After/rate-limit reset window."""
    headers = headers or {}
    candidates = []
    value = _header_value(headers, "Retry-After")
    if value:
        duration = _parse_duration_seconds(value)
        if duration is not None:
            candidates.append(duration)
        else:
            try:
                candidates.append(max(1, min(int(float(value) + 0.999), _MAX_COOLDOWN_SECONDS)))
            except (TypeError, ValueError, OverflowError):
                try:
                    when = parsedate_to_datetime(value)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    seconds = int((when - datetime.now(timezone.utc)).total_seconds())
                    if seconds > 0:
                        candidates.append(min(seconds, _MAX_COOLDOWN_SECONDS))
                except (TypeError, ValueError, OverflowError):
                    pass

    for name, value in headers.items():
        if "ratelimit-reset" in str(name).lower() or str(name).lower() == "x-ratelimit-reset":
            seconds = _reset_value_seconds(value)
            if seconds is not None:
                candidates.append(seconds)
    return max(candidates) if candidates else max(1, min(int(default), _MAX_COOLDOWN_SECONDS))


def _groq_key_index(key):
    """Номер ключа по его значению (для логов — без раскрытия самого ключа)."""
    try:
        from groq_rotation import GROQ_KEYS
        for item in GROQ_KEYS:
            if item.get("key") == key:
                return item.get("index")
    except Exception:
        pass
    return None


def _groq_429_cooldown(response, detail):
    headers = getattr(response, "headers", {}) or {}
    text = str(detail or "").lower()
    reset_headers = " ".join(str(v).lower() for k, v in headers.items() if "reset" in str(k).lower())
    daily = ("per day", "per-day", "daily limit", "daily quota", "tokens per day", "requests per day", "day limit")
    is_daily = any(term in text for term in daily) or "daily" in reset_headers
    header_seconds = _retry_after_seconds(headers, GROQ_KEY_COOLDOWN if is_daily else 60)
    # Groq's message commonly says "Please try again in 2m15.4s".
    body_seconds = None
    match = re.search(r"\b(?:in|after)\s+((?:\d+(?:\.\d+)?\s*[hms]\s*)+)", text)
    if match:
        body_seconds = _parse_duration_seconds(match.group(1))
    if is_daily and body_seconds is None and not any(
        _reset_value_seconds(v) for k, v in headers.items() if "ratelimit-reset" in str(k).lower()
    ) and not _header_value(headers, "Retry-After"):
        return GROQ_KEY_COOLDOWN
    return max(header_seconds, body_seconds or 0)


def _format_gemini_error(status_code, response):
    """Извлекает код, статус и причину ошибки из Gemini JSON."""
    try:
        body = response.json()
    except (ValueError, AttributeError):
        body = {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    message = str(error.get("message") or getattr(response, "text", "") or "нет подробностей").strip()
    api_status = str(error.get("status") or "").upper()
    lowered = (message + " " + api_status).lower()
    if status_code == 429 or any(x in lowered for x in ("quota", "rate limit", "resource_exhausted")):
        reason = "превышена квота или частотный лимит"
    elif status_code == 404 or any(x in lowered for x in ("model not found", "unknown model", "not found for api version")):
        reason = "модель не найдена или недоступна"
    elif status_code == 400:
        reason = "ошибка запроса или параметров"
    elif status_code in (401, 403):
        reason = "ключ недействителен или доступ запрещён"
    else:
        reason = "ошибка API"
    return f"Google AI Studio HTTP {status_code} ({reason}): {message[:900]}"


def gemini_messages_from_openai(messages):
    """OpenAI-сообщения → (systemInstruction, contents) для Gemini.

    Поддерживает нативный tool calling: assistant.tool_calls превращается
    в functionCall, а role="tool" — в functionResponse (иначе Gemini
    отверг бы второй запрос цикла агента).
    """
    contents_out=[]; system_text=""; call_names={}
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role=message.get("role","user"); content=message.get("content","")
        if isinstance(content,list):
            content="\n".join(str(p.get("text","")) for p in content if isinstance(p,dict) and p.get("text"))
        content=str(content or "")
        calls = message.get("tool_calls") if role == "assistant" else None
        if not content and not calls: continue
        if role=="system": system_text=(system_text+"\n\n"+content).strip()
        elif role=="assistant":
            parts=[]
            if content: parts.append({"text":content})
            for call in calls or []:
                if not isinstance(call, dict): continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(function.get("name") or "")
                if not name: continue
                args, _err = _parse_tool_arguments(function.get("arguments"))
                call_id = str(call.get("id") or f"call_{len(call_names)}")
                call_names[call_id] = name
                parts.append({"functionCall": {"name": name, "args": args, "id": call_id}})
            if parts: contents_out.append({"role":"model","parts":parts})
        elif role=="tool":
            tool_call_id = str(message.get("tool_call_id") or "")
            name = call_names.get(tool_call_id) or str(message.get("name") or "tool")
            function_response = {"name": name, "response": {"result": content},
                                 "call_id": tool_call_id or name}
            contents_out.append({"role": "user", "parts": [{"functionResponse": function_response}]})
        else: contents_out.append({"role":"user","parts":[{"text":content}]})
    return system_text,contents_out


def gemini_tools_payload(tools):
    """OpenAI-совместимые tools → Gemini functionDeclarations."""
    declarations = []
    for tool in openai_tools_payload(tools):
        function = tool["function"]
        parameters = function.get("parameters") or {"type": "object", "properties": {}}
        if isinstance(parameters, dict):
            parameters = {k: v for k, v in parameters.items()
                          if k not in ("additionalProperties", "$schema")}
        declarations.append({"name": function["name"],
                             "description": function.get("description", ""),
                             "parameters": parameters})
    return [{"functionDeclarations": declarations}] if declarations else []

def _gemini_generation_config(model, payload):
    """Build generation settings compatible with Gemini 3+ model contracts."""
    model_id = str(model or "").strip().removeprefix("models/").lower()
    generation_config = {
        "maxOutputTokens": min(int(payload.get("max_tokens", 65536)), 65536),
    }
    # Gemini 3.x/4.x no longer accepts legacy sampling controls such as
    # temperature. Keep it only for older model families.
    if not model_id.startswith(("gemini-3", "gemini-4")):
        generation_config["temperature"] = float(payload.get("temperature", 0.7))
    return generation_config


def _gemini_request_with_key(model, payload, key, timeout=90):
    model_id = str(model or "").strip().removeprefix("models/")
    system_text, contents_out = gemini_messages_from_openai(payload.get("messages", []))
    if not contents_out:
        raise pm.ProviderError(pm.ErrorKind.BAD_RESPONSE,
                               "Google AI Studio: отсутствует сообщение",
                               provider="google_ai_studio", model=model)
    body = {"contents": contents_out,
            "generationConfig": _gemini_generation_config(model_id, payload)}
    if system_text:
        body["systemInstruction"] = {"parts": [{"text": system_text}]}
    gemini_tools = gemini_tools_payload(payload.get("tools"))
    if gemini_tools:
        body["tools"] = gemini_tools
        body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    response = requests.post(url, json=body, headers=headers, timeout=timeout)
    if response.status_code >= 400:
        kind, retry_after = pm.classify_response(
            response.status_code, response.headers, response.text or "")
        raise pm.ProviderError(kind, _format_gemini_error(response.status_code, response),
                               status_code=response.status_code, retry_after=retry_after,
                               provider="google_ai_studio", model=model)
    data = response.json()
    if not isinstance(data, dict):
        raise pm.ProviderError(pm.ErrorKind.BAD_RESPONSE,
                               "Google AI Studio вернул ответ неожиданной структуры",
                               provider="google_ai_studio", model=model)
    parts, tool_calls = [], []
    for candidate in data.get("candidates", []):
        for part in (candidate.get("content") or {}).get("parts", []):
            if part.get("text"):
                parts.append(part["text"])
            call = part.get("functionCall") if isinstance(part, dict) else None
            if isinstance(call, dict) and call.get("name"):
                tool_calls.append({"id": str(call.get("id") or f"call_{len(tool_calls)}"),
                                   "type": "function",
                                   "function": {"name": call["name"],
                                                "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False)}})
    if not parts and not tool_calls:
        raise pm.ProviderError(pm.ErrorKind.BAD_RESPONSE,
                               "Google AI Studio не вернул текст",
                               provider="google_ai_studio", model=model)
    message = {"role": "assistant", "content": "".join(parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def gemini_request(model, payload, timeout=90, max_retries=2):
    manager = provider_manager()
    if "google_ai_studio" in manager.provider_names():
        data, error = manager.execute(
            "google_ai_studio", model,
            lambda key_state: _gemini_request_with_key(model, payload, key_state.key, timeout),
            max_attempts=max_retries)
        return data, str(error) if error else None
    key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_KEY") or "").strip()
    if not key:
        return None, "Google AI Studio: GEMINI_API_KEY не найден в .env"
    try:
        return _gemini_request_with_key(model, payload, key, timeout), None
    except Exception as exc:
        return None, str(exc)


def _gemini_stream_with_key(model, payload, key, timeout=90):
    model_id = str(model or "").strip().removeprefix("models/")
    system_text, contents_out = gemini_messages_from_openai(payload.get("messages", []))
    if not contents_out:
        raise pm.ProviderError(pm.ErrorKind.BAD_RESPONSE,
                               "Google AI Studio: отсутствует сообщение",
                               provider="google_ai_studio", model=model)
    body = {"contents": contents_out,
            "generationConfig": _gemini_generation_config(model_id, payload)}
    if system_text:
        body["systemInstruction"] = {"parts": [{"text": system_text}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:streamGenerateContent"
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
               "x-goog-api-key": key}
    response = requests.post(url, params={"alt": "sse"}, json=body, headers=headers,
                             timeout=(15, timeout), stream=True)
    if response.status_code >= 400:
        kind, retry_after = pm.classify_response(
            response.status_code, response.headers, response.text or "")
        status = response.status_code
        message = _format_gemini_error(status, response)
        response.close()
        raise pm.ProviderError(kind, message, status_code=status, retry_after=retry_after,
                               provider="google_ai_studio", model=model)
    response.encoding = "utf-8"
    return response


def gemini_stream_request(model, payload, timeout=90):
    manager = provider_manager()
    if "google_ai_studio" in manager.provider_names():
        def open_with_key(key_state):
            response = _gemini_stream_with_key(model, payload, key_state.key, timeout)
            response._novamind_key_index = key_state.index
            return response
        response, error = manager.execute("google_ai_studio", model, open_with_key)
        return response, str(error) if error else None
    key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_KEY") or "").strip()
    if not key:
        return None, "Google AI Studio: GEMINI_API_KEY не найден в .env"
    try:
        return _gemini_stream_with_key(model, payload, key, timeout), None
    except Exception as exc:
        return None, str(exc)


def groq_request_with_rotation(url, payload, headers, timeout=90, max_retries=3):
    """
    Запрос к Groq. Ротация выполняется только при 401/429:
    ошибки модели/параметров (400/403/404) не должны портить состояние ключей.
    """
    if "generativelanguage.googleapis.com" in url:
        return gemini_request(payload.get("model"), payload, timeout=timeout, max_retries=max_retries)
    if "api.groq.com" not in url:
        return openai_compatible_request(url, payload, headers, timeout=timeout, max_retries=max_retries)

    last_error = None
    max_attempts = max(1, min(max_retries, len(GROQ_KEYS_LIST())))
    if not GROQ_KEYS_LIST():
        return None, "Groq API ключ не найден. Добавьте GROQ_API_KEY в .env."

    for attempt in range(max_attempts):
        current_key = get_groq_key()
        if not current_key:
            return None, last_error or "Все Groq API ключи временно недоступны или помечены как неисправные."

        request_headers = dict(headers or {})
        request_headers["Authorization"] = f"Bearer {current_key}"
        request_headers.setdefault("Content-Type", "application/json")

        try:
            resp = requests.post(url, json=payload, headers=request_headers, timeout=timeout)
        except requests.exceptions.Timeout:
            return None, "Таймаут запроса к Groq; API-ключ не менялся."
        except requests.exceptions.RequestException as exc:
            return None, f"Ошибка соединения с Groq: {exc}; API-ключ не менялся."

        if resp.status_code == 401:
            last_error = "Groq HTTP 401: API-ключ отклонён; ключ временно отложен. После 3 последовательных 401 он будет отключён до перезапуска."
            mark_groq_key_exhausted(permanent=False, cooldown_seconds=60, key=current_key, auth_failure=True)
            pm.log_event("warning", "key_auth_failed", provider="groq", kind="auth",
                         key_index=_groq_key_index(current_key), reason=last_error,
                         attempt=attempt + 1)
            continue

        if resp.status_code == 429:
            detail = pm.redact((resp.text or "")[:400], [current_key])
            last_error = f"Groq HTTP 429: превышен лимит запросов/токенов. {detail}".strip()
            cooldown = _groq_429_cooldown(resp, detail)
            mark_groq_key_exhausted(cooldown_seconds=cooldown, key=current_key)
            resp_headers = getattr(resp, "headers", {}) or {}
            pm.log_event("warning", "key_rate_limited", provider="groq", kind="rate_limit",
                         key_index=_groq_key_index(current_key), reason=detail,
                         cooldown_sec=cooldown, retry_after_sec=pm.parse_retry_after(resp_headers),
                         attempt=attempt + 1)
            if attempt < max_attempts - 1:
                continue  # next Groq key; the exhausted key remains in its own cooldown
            return None, last_error

        if resp.status_code >= 400:
            detail = pm.redact((resp.text or "")[:600], [current_key])
            kind, _ = pm.classify_response(resp.status_code, getattr(resp, "headers", {}) or {}, detail)
            pm.log_event("warning", "provider_http_error", provider="groq",
                         kind=kind.value, reason=detail, attempt=attempt + 1)
            return None, f"Groq HTTP {resp.status_code}: {detail}"

        try:
            data = resp.json()
            mark_groq_key_success(current_key)
            # Остаток лимитов приходит в заголовках — запоминаем, чтобы
            # предупредить пользователя заранее (Б3).
            try:
                from groq_rotation import record_limits
                record_limits(getattr(resp, "headers", None), current_key)
            except Exception:
                pass
            return data, None
        except ValueError:
            return None, "Groq вернул некорректный JSON"

    return None, last_error or "Не удалось выполнить запрос к Groq"


# ══════════════════════════════════════════════════════════════════
# ЕДИНЫЙ СЛОЙ ДОСТУПА К МОДЕЛЯМ
# Один вход для всех провайдеров: cloud, локальный llama-server и
# встроенная офлайн-модель. Раньше каждый маршрут дёргал transport
# вручную и ломался на Gemini (в URL провайдера плейсхолдер {model}).
# ══════════════════════════════════════════════════════════════════

REASONING_MODELS_HINT = ("gpt-oss", "deepseek-r1", "qwen-3", "glm-4.5", "o1", "o3", "nemotron")

# ── Нативный tool calling ────────────────────────────────────────────
# Провайдеры, которые принимают OpenAI-совместимый массив `tools` и
# возвращают `message.tool_calls`. Для Gemini (google_ai_studio) аргументы
# конвертируются в functionDeclarations внутри gemini_request.
NATIVE_TOOL_PROVIDERS = {"groq", "cerebras", "openrouter", "pollinations",
                         "openai_compatible", "google_ai_studio"}


def supports_native_tools(provider: str | None = None, model: str | None = None) -> bool:
    """Умеет ли провайдер/модель нативный tool calling.

    Отключается переменной окружения AI_NATIVE_TOOLS=0 (аварийный откат
    на старый JSON-протокол без правки кода). Для локального сервера решение
    принимает local_providers: каталог Ollama/LM Studio сообщает реальные
    возможности модели, а LOCAL_LLM_TOOLS=on|off|auto ими управляет.
    """
    if os.getenv("AI_NATIVE_TOOLS", "1") != "1":
        return False
    if not provider:
        provider = resolve_target()[0]
    if provider == "local_demo":
        return False
    if provider == "openai_compatible":
        try:
            return local_providers.tools_supported(str(model or ""))
        except Exception:                                  # noqa: BLE001
            return True
    return provider in NATIVE_TOOL_PROVIDERS


def _parse_tool_arguments(raw):
    """Аргументы tool_call → (dict, ошибка). Битый JSON не роняет запрос."""
    if raw is None or raw == "":
        return {}, ""
    if isinstance(raw, dict):
        return raw, ""
    text = str(raw).strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        return {}, f"аргументы инструмента не являются корректным JSON: {exc}"
    if isinstance(data, dict):
        return data, ""
    return {"value": data}, ""


def normalize_tool_calls(raw_calls):
    """Приводит tool_calls провайдера к единому виду.

    Возвращает список словарей:
        {"id", "name", "arguments", "raw_arguments", "arguments_error"}
    Ошибка разбора аргументов не выбрасывается наружу: агент сообщает
    модели, что аргументы битые, и просит повторить вызов.
    """
    calls = []
    for index, call in enumerate(raw_calls or []):
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or call.get("name") or "").strip()
        if not name:
            continue
        raw_args = function.get("arguments", call.get("arguments", {}))
        arguments, arguments_error = _parse_tool_arguments(raw_args)
        calls.append({
            "id": str(call.get("id") or f"call_{index}"),
            "name": name,
            "arguments": arguments,
            "raw_arguments": raw_args if isinstance(raw_args, str)
            else json.dumps(raw_args, ensure_ascii=False),
            "arguments_error": arguments_error,
        })
    return calls


def openai_tools_payload(tools):
    """Оставляет только корректные OpenAI-совместимые описания инструментов."""
    payload = []
    for tool in tools or []:
        if isinstance(tool, dict) and tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            payload.append(tool)
        elif isinstance(tool, dict) and tool.get("name"):
            # Разрешаем «плоский» формат: {name, description, parameters}.
            payload.append({"type": "function", "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            }})
    return payload


def _clean_reasoning_tail(value):
    """Удаляет распространённые служебные маркеры, сохраняя обычный текст."""
    text = str(value or "")
    # Удаляем парные теги вида <thought>...</thought>, [THINK]...[/THINK] и т.п.
    text = re.sub(r"(?is)<\s*(?:thought|think|reasoning|analysis|internal)\b[^>]*>.*?<\s*/\s*(?:thought|think|reasoning|analysis|internal)\s*>", "", text)
    # Удаляем одиночные открывающие/закрывающие теги
    text = re.sub(r"(?is)</?\s*(?:thought|think|reasoning|analysis|internal)\b[^>]*>", "", text)
    # Удаляем маркеры вида [THINK] или [/THINK]
    text = re.sub(r"(?is)\[/?(?:THINK|REASONING|ANALYSIS|INTERNAL)\]", "", text)
    # Удаляем маркеры <|think|>, <|analysis|> и т.п.
    text = re.sub(r"(?is)<\|(?:think|analysis|reasoning)[^|]*\|>", "", text)
    return text.strip()

def provider_headers(provider: str) -> dict:
    """Заголовки для провайдера (ключ подставляется на сервере, не в браузере)."""
    import config
    headers = dict(PROVIDERS_HEADERS.get(provider) or config.PROVIDERS.get(provider, {}).get("headers") or {})
    if provider == "groq":
        key = get_groq_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
    headers.setdefault("Content-Type", "application/json")
    return headers


# Заголовки из config.PROVIDERS собираются один раз, но ключ Groq — всегда свежий.
PROVIDERS_HEADERS: dict = {}


def supports_reasoning(model: str) -> bool:
    model_id = str(model or "").lower()
    return any(hint in model_id for hint in REASONING_MODELS_HINT)


def resolve_target(provider: str = None, model: str = None):
    """Resolve only the explicitly selected provider/model; never switch silently."""
    import config
    provider = provider or config.current_provider
    model = model or config.current_model
    cfg = config.PROVIDERS.get(provider)
    if cfg is None:
        raise ValueError(f"Неизвестный AI-провайдер: {provider}")
    return provider, model, cfg


FREE_TIER_PROVIDERS = frozenset({
    "groq", "cerebras", "google_ai_studio", "openai_compatible", "local_demo",
})
def is_free_ai_model(provider: str, model: str, entry: dict | None = None) -> bool:
    """Whether a chat target is guaranteed not to select a paid model.

    Groq/Cerebras/Google AI Studio are used only through their free developer
    tiers in NovaMind. OpenRouter/Pollinations must explicitly mark a model as
    free (or use the conventional ``:free`` suffix). Local targets are free.
    """
    provider = str(provider or "").strip()
    model_id = str(model or "").strip().lower()
    item = entry if isinstance(entry, dict) else {}
    if provider == "openrouter" and model_id == "openrouter/free":
        # OpenRouter's dedicated router selects only currently available free models.
        return True
    if provider == "google_ai_studio":
        # Google catalog includes paid-only Pro/preview models. Keep only the
        # explicit free-tier allowlist maintained in config.py.
        try:
            import config
            allowed = {str(item.get("id") or "").lower()
                       for item in config.PROVIDERS[provider].get("models", [])
                       if item.get("free") is True}
        except Exception:
            allowed = set()
        return model_id in allowed
    if provider in FREE_TIER_PROVIDERS:
        return True
    if model_id.endswith(":free"):
        return True
    return item.get("free") is True or str(item.get("pricing_status") or "").lower() == "free"


def _free_models(provider: str, selected: str = "") -> list[str]:
    import config
    configured = list((config.PROVIDERS.get(provider) or {}).get("models") or [])
    found: list[str] = []
    if selected and is_free_ai_model(provider, selected):
        found.append(str(selected))
    for item in configured:
        model_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
        if model_id and is_free_ai_model(provider, model_id, item) and model_id not in found:
            found.append(model_id)
    # Dynamic providers may have a selected :free model although config.models
    # is populated from the network only in the settings screen.
    if selected and str(selected).endswith(":free") and selected not in found:
        found.insert(0, str(selected))
    if provider == "local_demo" and not found:
        found.append("nova-local-1")
    return found


_PM = None
_PM_LOCK = __import__("threading").Lock()


def provider_manager():
    """Ленивый единый Provider Manager со всеми провайдерами из окружения."""
    global _PM
    if _PM is None:
        with _PM_LOCK:
            if _PM is None:
                _PM = pm.default_manager()
    return _PM


def _pm_mark_failure(provider, model, error_text):
    """Отмечает ошибку в Provider Manager; Groq keys are tracked per-key separately."""
    if provider == "groq":
        return  # groq_rotation knows the exact key used by the failed request
    try:
        manager = provider_manager()
        if provider not in manager.provider_names():
            return
        kind = pm.classify_error_text(error_text)
        key = manager.available_key(provider)
        if key is None:
            return  # все ключи уже в cooldown — не используем их повторно
        manager.mark_key_result(provider, key.index,
                                pm.ProviderError(kind, pm.redact(str(error_text)[:300]),
                                                 provider=provider, model=model))
    except Exception:
        logger.debug("pm mark failure failed", exc_info=True)


def _pm_skip_provider(name):
    """True, если у провайдера есть ключи, но все они сейчас в cooldown/отключены."""
    try:
        manager = provider_manager()
        if name in manager.provider_names():
            return manager.all_keys_unavailable(name)
    except Exception:
        pass
    return False


def free_failover_targets(provider: str | None = None, model: str | None = None) -> list[tuple[str, str]]:
    """Return only the selected free target; provider/model failover is disabled."""
    primary_provider, primary_model, _cfg = resolve_target(provider, model)
    if not primary_model or not is_free_ai_model(primary_provider, primary_model):
        return []
    return [(primary_provider, primary_model)]


def _managed_provider_request(provider, model, url, payload, headers, timeout=90,
                              max_attempts=None):
    """Rotate keys only inside the selected provider; never change provider/model."""
    manager = provider_manager()
    if provider not in manager.provider_names():
        retries = max(1, int(max_attempts)) if max_attempts is not None else 2
        return openai_compatible_request(url, payload, headers, timeout=timeout,
                                        max_retries=retries)

    def request_with_key(key_state):
        request_headers = dict(headers or {})
        request_headers["Authorization"] = f"Bearer {key_state.key}"
        data, error = openai_compatible_request(
            url, payload, request_headers, timeout=timeout, max_retries=1)
        if error:
            kind = pm.classify_error_text(error)
            raise pm.ProviderError(kind, str(error), provider=provider, model=model)
        return data

    data, error = manager.execute(provider, model, request_with_key,
                                   max_attempts=max_attempts)
    return data, str(error) if error else None


def _managed_stream_request(provider, model, url, payload, headers, timeout=90):
    """Open an SSE response with same-provider key rotation before streaming starts."""
    manager = provider_manager()
    if provider not in manager.provider_names():
        try:
            response = requests.post(url, json=payload, headers=headers,
                                     timeout=(min(30, timeout), timeout), stream=True)
        except requests.exceptions.RequestException as exc:
            return None, f"Ошибка соединения с {provider}: {exc.__class__.__name__}"
        if response.status_code >= 400:
            detail = pm.redact((response.text or "")[:600])
            response.close()
            return None, f"{provider} HTTP {response.status_code}: {detail}"
        response.encoding = "utf-8"
        return response, None

    def open_with_key(key_state):
        request_headers = dict(headers or {})
        request_headers["Authorization"] = f"Bearer {key_state.key}"
        response = requests.post(url, json=payload, headers=request_headers,
                                 timeout=(min(30, timeout), timeout), stream=True)
        if response.status_code >= 400:
            detail = pm.redact((response.text or "")[:600], [key_state.key])
            kind, retry_after = pm.classify_response(
                response.status_code, response.headers, response.text or "")
            status = response.status_code
            response.close()
            raise pm.ProviderError(kind, f"{provider} HTTP {status}: {detail}",
                                   status_code=status, retry_after=retry_after,
                                   provider=provider, model=model)
        response.encoding = "utf-8"
        response._novamind_key_index = key_state.index
        return response

    response, error = manager.execute(provider, model, open_with_key)
    return response, str(error) if error else None


def _mark_stream_key_failure(provider, model, response, error_text):
    key_index = getattr(response, "_novamind_key_index", None)
    if key_index is None:
        return
    try:
        kind = pm.classify_error_text(error_text)
        provider_manager().mark_key_result(
            provider, key_index,
            pm.ProviderError(kind, pm.redact(str(error_text)[:300]),
                             provider=provider, model=model, key_index=key_index))
    except Exception:
        logger.debug("stream key failure update failed", exc_info=True)


def _local_request_options(model, extra=None, *, reasoning=False,
                           json_mode=False, json_schema=None):
    """Optional OpenAI-compatible params for a local runtime (never raises)."""
    try:
        return local_providers.request_options(
            str(model or ""), extra=extra, reasoning=reasoning,
            json_mode=json_mode, json_schema=json_schema)
    except Exception:                                      # noqa: BLE001
        logger.debug("local request options failed", exc_info=True)
        return dict(extra or {})


def _request_local_with_fallback(provider, model, cfg, payload, timeout,
                                 max_attempts=None):
    """POST a local request; retry once without fields the server rejected.

    Local runtimes differ in what they implement: one llama-server build
    accepts ``tools`` but not ``response_format``, another accepts both.
    A 400 that names an optional field is answered by dropping just that
    field instead of failing the user's chat.
    """
    data, error = _managed_provider_request(
        provider, model, cfg["url"], payload, provider_headers(provider), timeout=timeout,
        max_attempts=max_attempts)
    if data is not None or provider != "openai_compatible" or not error:
        return data, error, []
    fields = local_providers.rejected_fields(str(error))
    stripped = local_providers.strip_fields(payload, fields)
    if not fields or stripped == payload:
        return data, error, []
    logger.info("local server rejected optional fields %s — retrying without them", fields)
    data, retry_error = _managed_provider_request(
        provider, model, cfg["url"], stripped, provider_headers(provider), timeout=timeout,
        max_attempts=max_attempts)
    if data is not None:
        return data, None, fields
    return None, (retry_error or error), fields


def _chat_completion_once(messages, provider=None, model=None, system=None,
                          temperature=0.7, max_tokens=None, timeout=90, extra=None,
                          tools=None, tool_choice=None, json_mode=False,
                          json_schema=None, max_attempts=None):
    """
    Синхронный запрос к текущей модели.
    Возвращает (text, meta) или (None, error_string).

    `tools` — OpenAI-совместимый список инструментов. Если провайдер их
    поддерживает, ответ может не содержать текста, но содержать tool_calls:
    тогда meta["tool_calls"] — разобранные вызовы (см. normalize_tool_calls).

    Промпты здесь всегда английские; язык ответа задаёт language_policy,
    дописывая к системе короткую директиву под выбранный язык интерфейса.
    """
    provider, model, cfg = resolve_target(provider, model)
    system = language_policy.apply_to_system(system)
    tools_payload = openai_tools_payload(tools) if (tools and supports_native_tools(provider, model)) else []
    local_caps = {}

    if provider == "local_demo":
        import local_llm
        payload_messages = ([{"role": "system", "content": system}] if system else []) + list(messages or [])
        text = local_llm.complete(payload_messages)
        return text, {"provider": provider, "model": local_llm.MODEL_ID,
                      "model_name": local_llm.MODEL_NAME, "offline": True,
                      "tool_calls": [], "native_tools": False}

    payload = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else []) + list(messages or []),
        "temperature": temperature,
        "max_tokens": int(max_tokens or cfg.get("max_tokens", 8192)),
    }
    if tools_payload:
        payload["tools"] = tools_payload
        payload["tool_choice"] = tool_choice or "auto"
    if extra:
        payload.update(extra)
    if provider == "openai_compatible":
        # Same knobs hosted providers get for free: JSON mode, sampling,
        # reasoning controls and usage accounting.
        options = _local_request_options(model, extra=None, json_mode=json_mode,
                                         json_schema=json_schema)
        payload.update(options)
        local_caps = local_providers.capabilities_for(str(model))

    if provider == "google_ai_studio":
        data, error = gemini_request(model, payload, timeout=timeout,
                                     max_retries=max_attempts or 2)
    elif provider == "groq":
        data, error = groq_request_with_rotation(
            cfg["url"], payload, provider_headers("groq"), timeout=timeout,
            max_retries=max_attempts or 3)
    else:
        data, error, degraded = _request_local_with_fallback(
            provider, model, cfg, payload, timeout, max_attempts=max_attempts)
        if data is not None and degraded:
            local_caps = dict(local_caps or {})
            local_caps["degraded"] = degraded

    if error:
        return None, error
    try:
        message = (data.get("choices") or [{}])[0].get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):
            text = "".join(str(p.get("text", "")) for p in text if isinstance(p, dict))
        text = str(text)
        tool_calls = normalize_tool_calls(message.get("tool_calls"))
        thought = (message.get("reasoning") or message.get("reasoning_content")
                   or message.get("thinking") or "")
        thought = thought if isinstance(thought, str) else ""
        if not text.strip() and thought.strip() and not tool_calls:
            text = thought
    except (AttributeError, IndexError, TypeError):
        logger.warning("Неожиданный формат ответа (provider=%s, model=%s)", provider, model, exc_info=True)
        return None, "Провайдер вернул ответ в неожиданном формате"
    if not text.strip() and not tool_calls:
        logger.warning("Пустые content и reasoning (provider=%s, model=%s)", provider, model)
        return None, "Провайдер вернул пустой ответ (content и reasoning отсутствуют)"
    degraded_fields = list((local_caps or {}).get("degraded") or [])
    result_meta = {"provider": provider, "model": model, "offline": False,
                   "tool_calls": tool_calls,
                   "native_tools": bool(tools_payload) and "tools" not in degraded_fields,
                   "answer_language": language_policy.current_language(),
                   # usage пригодится на этапе счётчика токенов
                   "usage": data.get("usage") if isinstance(data, dict) else None}
    if local_caps:
        result_meta["capabilities"] = local_caps
        result_meta["local"] = provider == "openai_compatible"
    return text, result_meta


def chat_completion(messages, provider=None, model=None, system=None,
                    temperature=0.7, max_tokens=None, timeout=90, extra=None,
                    tools=None, tool_choice=None, json_mode=False,
                    json_schema=None, max_attempts=None):
    """Run exactly the selected free provider/model; never fail over."""
    try:
        targets = free_failover_targets(provider, model)
    except ValueError as exc:
        return None, str(exc)
    if not targets:
        return None, "Выбранная модель платная, неизвестная или не настроена. Автоматическая замена модели отключена."
    target_provider, target_model = targets[0]
    text, meta = _chat_completion_once(
        messages, provider=target_provider, model=target_model, system=system,
        temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        extra=extra, tools=tools, tool_choice=tool_choice,
        json_mode=json_mode, json_schema=json_schema, max_attempts=max_attempts,
    )
    if text is None:
        return None, meta
    result_meta = dict(meta or {}) if isinstance(meta, dict) else {}
    result_meta.update({"failover": [], "failover_count": 0, "free_model": True})
    return text, result_meta


def _chat_stream_once(messages, provider=None, model=None, system=None, temperature=0.7,
                max_tokens=None, timeout=90, reasoning=False, reasoning_effort="low"):
    """
    Потоковый ответ. Генератор кортежей (kind, text), где kind:
      "reasoning" — ход мыслей модели (если провайдер его отдаёт),
      "token"     — текст ответа,
      "error"     — ошибка (текст),
      "done"      — конец потока.

    Если модель не умеет отдавать рассуждения, а режим включён — сначала
    выполняется короткий «план рассуждений», затем основной ответ.
    """
    provider, model, cfg = resolve_target(provider, model)
    system = language_policy.apply_to_system(system)
    want_reasoning = bool(reasoning)

    if provider == "local_demo":
        import local_llm
        payload_messages = ([{"role": "system", "content": system}] if system else []) + list(messages or [])
        for kind, chunk in local_llm.stream(payload_messages, reasoning=want_reasoning):
            yield kind, chunk
        yield "done", ""
        return

    payload = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else []) + list(messages or []),
        "temperature": temperature,
        "max_tokens": int(max_tokens or cfg.get("max_tokens", 8192)),
        "stream": True,
    }
    if provider == "openai_compatible":
        # Local runtimes get the same optional features as hosted providers;
        # unsupported fields are dropped automatically in _raw_stream.
        payload.update(_local_request_options(model, reasoning=want_reasoning))
        if os.getenv("LOCAL_LLM_STREAM_USAGE", "1") == "1":
            # Token accounting while streaming, like Groq/OpenRouter provide.
            payload["stream_options"] = {"include_usage": True}
    # Groq: параметры рассуждений по документации — reasoning_effort (строка)
    # и reasoning_format. Раньше уходило поле "reasoning": {"effort": ...} в
    # стиле OpenRouter: Groq мог ответить 400, и пользователь получал
    # «Не удалось получить ответ модели».
    if provider == "groq" and "gpt-oss" in str(model or "").lower():
        payload["reasoning_effort"] = reasoning_effort if want_reasoning else "low"
        if not want_reasoning:
            payload["reasoning_format"] = "hidden"

    # Предварительный «план рассуждений» для моделей без нативного reasoning.
    prepass_needed = want_reasoning and not (provider == "groq" and supports_reasoning(model))
    produced_reasoning = False

    if prepass_needed:
        plan_messages = list(messages or []) + [{
            "role": "user",
            "content": (
                "Before answering, outline your reasoning in 3-6 short bullet points: "
                "what matters here, which options exist and what must be verified. "
                "No preamble, no answer yet."
            ),
        }]
        plan_payload = dict(payload)
        plan_payload["messages"] = ([{"role": "system", "content": system}] if system else []) + plan_messages
        plan_payload["max_tokens"] = 400
        plan_payload["temperature"] = 0.4
        for kind, chunk in _raw_stream(provider, model, plan_payload, cfg, timeout):
            if kind == "token":
                produced_reasoning = True
                yield "reasoning", chunk
            elif kind == "error":
                # План не получился — не роняем основной ответ.
                logger.warning("Reasoning prepass failed: %s", chunk)
                break

    tokens_seen = False
    reasoning_tail = []
    for kind, chunk in _raw_stream(provider, model, payload, cfg, timeout):
        if kind == "reasoning":
            produced_reasoning = True
            reasoning_tail.append(chunk)
            if len(reasoning_tail) > 400:
                reasoning_tail.pop(0)
        elif kind == "token":
            tokens_seen = True
            reasoning_tail = []
        if kind == "error":
            if tokens_seen:
                yield "error", f"Ответ получен не полностью: поток оборвался после части текста. {chunk}"
            else:
                yield "error", chunk
            continue
        yield kind, chunk

    # Не показываем пользователю служебные теги из хвоста reasoning.
    if not tokens_seen and reasoning_tail:
        tail = _clean_reasoning_tail("".join(reasoning_tail))
        if tail:
            yield "token", tail

    if want_reasoning and not produced_reasoning:
        yield "reasoning", (
            "Модель не передала внутренний ход рассуждений — "
            "сразу сформировала ответ. Включите модель с поддержкой reasoning "
            "(например, gpt-oss-120b), чтобы видеть развёрнутую цепочку мыслей."
        )
    yield "done", ""


def chat_stream(messages, provider=None, model=None, system=None, temperature=0.7,
                max_tokens=None, timeout=90, reasoning=False, reasoning_effort="low"):
    """Stream only from the selected free provider/model; never replay elsewhere."""
    try:
        targets = free_failover_targets(provider, model)
    except ValueError as exc:
        yield "error", str(exc)
        yield "done", ""
        return
    if not targets:
        yield "error", "Выбранная модель платная, неизвестная или не настроена. Автоматическая замена модели отключена."
        yield "done", ""
        return
    target_provider, target_model = targets[0]
    for kind, chunk in _chat_stream_once(
        messages, provider=target_provider, model=target_model, system=system,
        temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        reasoning=reasoning, reasoning_effort=reasoning_effort,
    ):
        if kind != "done":
            yield kind, chunk
    yield "done", ""


def _raw_stream(provider, model, payload, cfg, timeout):
    """Низкоуровневый поток токенов конкретного провайдера."""
    if provider == "google_ai_studio":
        upstream, error = gemini_stream_request(model, payload, timeout=timeout)
        if upstream is None:
            data, fallback_error = gemini_request(model, payload, timeout=timeout)
            if fallback_error:
                yield "error", f"{error}. Fallback: {fallback_error}"
                return
            choices = data.get("choices") if isinstance(data, dict) else None
            first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first.get("message"), dict) else {}
            text = message.get("content") or ""
            if isinstance(text, list):
                text = "".join(str(part.get("text", "")) for part in text if isinstance(part, dict))
            if isinstance(text, str) and text.strip():
                yield "token", text
            else:
                logger.warning("Gemini fallback вернул пустой или неожиданный ответ")
            return
        try:
            for raw in upstream.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except (TypeError, ValueError):
                    continue
                for candidate in chunk.get("candidates", []):
                    for part in (candidate.get("content") or {}).get("parts", []):
                        if part.get("thought"):
                            if part.get("text"):
                                yield "reasoning", part["text"]
                            continue
                        if part.get("text"):
                            yield "token", part["text"]
        except requests.exceptions.RequestException as exc:
            _mark_stream_key_failure("google_ai_studio", model, upstream, exc)
            yield "error", f"Соединение с Google AI Studio прервано: {exc.__class__.__name__}"
            return
        finally:
            upstream.close()
        return

    if provider != "groq":
        response, error = _managed_stream_request(
            provider, model, cfg["url"], payload, provider_headers(provider), timeout)
        # Local runtimes differ in the optional fields they implement: retry
        # once without whatever the server named in a 400 response.
        if response is None and provider == "openai_compatible" and error:
            fields = local_providers.rejected_fields(str(error))
            stripped = local_providers.strip_fields(payload, fields)
            if fields and stripped != payload:
                logger.info("local stream rejected %s — retrying without them", fields)
                response, error = _managed_stream_request(
                    provider, model, cfg["url"], stripped, provider_headers(provider), timeout)
        if response is None:
            yield "error", error or f"Не удалось открыть поток от {provider}"
            return
        response.encoding = "utf-8"
        try:
            # One parser for every local dialect: SSE, NDJSON, Ollama chat
            # chunks and llama.cpp completion chunks, including reasoning
            # deltas and streamed tool calls.
            for kind, chunk in local_providers.iter_stream(response):
                if kind in ("token", "reasoning"):
                    yield kind, chunk
                elif kind == "error":
                    yield "error", str(chunk)
                    return
                elif kind == "done":
                    break
        except requests.exceptions.RequestException as exc:
            _mark_stream_key_failure(provider, model, response, exc)
            yield "error", f"Соединение с моделью прервано: {exc.__class__.__name__}"
            return
        finally:
            try:
                response.close()
            except Exception:                              # noqa: BLE001
                pass
        return

    headers = provider_headers(provider)
    if provider == "groq":
        attempts = max(1, min(len(GROQ_KEYS_LIST()), 4))
    else:
        attempts = 1

    last_error = None
    for attempt in range(attempts):
        if provider == "groq":
            current_key = get_groq_key()
            if not current_key:
                yield "error", last_error or "Все ключи Groq временно недоступны. Проверьте GROQ_API_KEY и лимиты."
                return
            headers = dict(headers or {})
            headers["Authorization"] = f"Bearer {current_key}"

        try:
            # (подключение, ожидание очередной порции данных) — долгий первый
            # токен у reasoning-моделей не должен обрываться по общему таймауту
            response = requests.post(cfg["url"], json=payload, headers=headers,
                                     timeout=(min(30, timeout), timeout), stream=True)
        except requests.exceptions.RequestException as exc:
            last_error = f"Ошибка соединения с {provider}: {exc.__class__.__name__}"
            yield "error", last_error
            return

        if provider == "groq" and response.status_code in (401, 429):
            status_code = response.status_code
            detail = (response.text or "")[:400]
            response.close()
            if status_code == 401:
                last_error = "Groq HTTP 401: API-ключ отклонён; временная блокировка, отключение после 3 последовательных 401."
                mark_groq_key_exhausted(permanent=False, cooldown_seconds=60, key=current_key, auth_failure=True)
            else:
                last_error = f"Groq HTTP 429: превышен лимит запросов/токенов. {detail}".strip()
                cooldown = _groq_429_cooldown(response, detail)
                mark_groq_key_exhausted(cooldown_seconds=cooldown, key=current_key)
            continue

        if response.status_code >= 400:
            detail = (response.text or "")[:600]
            response.close()
            last_error = f"{provider} HTTP {response.status_code}: {detail}"
            # Ошибки модели/параметров не относятся к состоянию API-ключа.
            yield "error", last_error
            return

        if provider == "groq":
            mark_groq_key_success(current_key)
        response.encoding = "utf-8"
        try:
            for raw in response.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except (TypeError, ValueError):
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                thought = delta.get("reasoning") or delta.get("reasoning_content") or ""
                if thought:
                    yield "reasoning", thought
                token = delta.get("content") or choice.get("text") or ""
                if token:
                    yield "token", token
        except requests.exceptions.RequestException as exc:
            # Соединение оборвалось посреди потока. Молча рвать генератор нельзя:
            # всё, что уже пришло, показываем пользователю, и помечаем поток
            # оборванным — вызывающий код сам решит, дособирать ответ или нет.
            yield "error", f"Соединение с моделью прервано: {exc.__class__.__name__}"
            return
        finally:
            response.close()
        return

    if last_error:
        _pm_mark_failure(provider, model, last_error)
    yield "error", last_error or "Не удалось получить поток от провайдера"


def GROQ_KEYS_LIST():
    from groq_rotation import GROQ_KEYS
    return GROQ_KEYS


import re

# ---------- Метки провайдеров ----------
MODEL_ERRORS: dict = {}
PROVIDER_LABELS = {
    "groq":             "Groq",
    "cerebras":         "Cerebras",
    "openrouter":       "OpenRouter",
    "google_ai_studio": "Google AI Studio",
    "openai_compatible":"OpenAI-compatible / Local",
    "pollinations":       "Pollinations.ai",
    "local_demo":         "Локальная демо-модель (офлайн)",
}

# ---------- Вспомогательные функции моделей ----------
def model_size_billions(model_id, model_name=""):
    match = re.search(r"(?:^|[-_/ ])(\d+(?:\.\d+)?)(?:b|B)(?:$|[-_/ ])", f"{model_id} {model_name}")
    return float(match.group(1)) if match else 0

def normalize_model(model, provider):
    model = model or {}
    model_id = model.get("id", "")
    name = model.get("name") or model_id
    pricing = model.get("pricing") or {}
    prompt_price = str(pricing.get("prompt", model.get("prompt_price", "")))
    completion_price = str(pricing.get("completion", model.get("completion_price", "")))
    is_free = (is_free_ai_model(provider, model_id, model)
               or (prompt_price in {"0", "0.0", "0.000000"}
                   and completion_price in {"0", "0.0", "0.000000"}))
    context = model.get("context_length") or model.get("context_window") or model.get("max_context_length") or 0
    architecture = model.get("architecture") or {}
    modality = architecture.get("modality", "text->text") if isinstance(architecture, dict) else "text->text"
    input_modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
    output_modalities = architecture.get("output_modalities") if isinstance(architecture, dict) else None
    if not input_modalities:
        input_modalities = [str(modality).split("->")[0]] if "->" in str(modality) else ["text"]
    if not output_modalities:
        output_modalities = [str(modality).split("->")[-1]] if "->" in str(modality) else ["text"]
    in_mods = [str(x).lower() for x in (input_modalities or [])]
    out_mods = [str(x).lower() for x in (output_modalities or [])]
    media_types = []
    if "image" in out_mods: media_types.append("image")
    if "audio" in out_mods: media_types.append("audio")
    if "video" in out_mods: media_types.append("video")
    if not media_types: media_types.append("text")
    return {
        "id": model_id, "name": name, "provider": provider,
        "provider_name": PROVIDER_LABELS.get(provider, provider),
        "context_length": int(context or 0),
        "parameters_b": model.get("parameter_count") or model_size_billions(model_id, name),
        "prompt_price": prompt_price, "completion_price": completion_price,
        "free": is_free, "modality": modality,
        "input_modalities": in_mods, "output_modalities": out_mods,
        "media_types": media_types,
        "pricing_status": "free" if is_free else ("paid" if prompt_price or completion_price else "unknown"),
        "created": model.get("created", 0), "description": model.get("description", ""),
    }

def _catalog_api_key(provider):
    """Use the first currently available key from the shared provider manager."""
    state = provider_manager().available_key(provider)
    return state.key if state is not None else ""


def provider_api_headers(provider):
    from groq_rotation import get_groq_key
    if provider == "groq":
        key = get_groq_key()
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else None
    if provider == "openrouter":
        key = _catalog_api_key(provider)
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:5000", "X-Title": "NovaMind AI"} if key else None
    if provider == "cerebras":
        key = _catalog_api_key(provider)
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else None
    if provider == "google_ai_studio":
        key = _catalog_api_key(provider)
        return {"x-goog-api-key": key, "Content-Type": "application/json"} if key else None
    if provider == "pollinations":
        key = _catalog_api_key(provider)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers
    key = _catalog_api_key(provider) or os.getenv("OPENAI_COMPATIBLE_KEY") or os.getenv("OPENAI_API_KEY") or "ollama"
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

def _local_model_fallback():
    """Резервный каталог для локального OpenAI-compatible сервера.
    Эти записи не являются отдельными облачными моделями: они представляют
    загруженную локально модель, когда /v1/models временно недоступен.
    """
    return [
        normalize_model({
            "id": os.getenv("LOCAL_MODEL_ID", "local-llama"),
            "name": os.getenv("LOCAL_MODEL_NAME", "Local Llama (llama.cpp)"),
            "description": "Local model served by llama-server / an OpenAI-compatible API.",
            "context_length": int(os.getenv("LOCAL_MODEL_CONTEXT", "4096")),
            "pricing": {"prompt": "0", "completion": "0"},
        }, "openai_compatible"),
        normalize_model({
            "id": "llama.cpp",
            "name": "Llama.cpp Local",
            "description": "Fallback entry for a local llama-server.",
            "context_length": 4096,
            "pricing": {"prompt": "0", "completion": "0"},
        }, "openai_compatible"),
    ]


def _local_catalog_urls():
    """Возвращает /models endpoints для llama-server и совместимых API.

    FIX: раньше к базе вида http://host:8080/v1 добавлялся ещё и суффикс
    /v1/models, из-за чего каталог искался по несуществующему /v1/v1/models.
    Список кандидатов теперь строит local_providers: у Ollama есть нативный
    /api/tags, у LM Studio — /api/v0/models.
    """
    try:
        return local_providers.catalog_urls(local_providers.configured_base())
    except Exception:                                      # noqa: BLE001
        return []


def _local_entry_to_model(entry: dict) -> dict:
    """Local catalogue entry → the model card shape the settings UI expects."""
    item = normalize_model({
        "id": entry.get("id"),
        "name": entry.get("name") or entry.get("id"),
        "description": entry.get("description") or "",
        "context_length": entry.get("context_length") or 0,
        "pricing": {"prompt": "0", "completion": "0"},
    }, "openai_compatible")
    item.update({
        "family": entry.get("family") or "",
        "parameter_size": entry.get("parameter_size") or "",
        "quantization": entry.get("quantization") or "",
        "size_bytes": entry.get("size_bytes") or 0,
        "loaded": bool(entry.get("loaded")),
        "preset": entry.get("preset") or "",
        "server": entry.get("server") or "",
        "capabilities": entry.get("capabilities") or {},
        "local": True,
    })
    return item


def local_runtime_status(force: bool = False) -> dict:
    """Состояние локального движка для панели настроек.

    Возвращает пресеты (Ollama, LM Studio, llama.cpp, vLLM…), реальный адрес,
    найденные модели с их возможностями и матрицу функций: streaming, tools,
    JSON mode, reasoning, vision. Секрет (API-ключ) не отдаётся наружу.
    """
    import config
    status = local_providers.status(force=force)
    engine = dict(getattr(config, "local_engine", {}) or {})
    status["engine"] = {
        "preset": engine.get("preset") or "auto",
        "base_url": engine.get("base_url") or "",
        "has_key": bool(engine.get("api_key")),
    }
    status["answer_language"] = language_policy.current_language()
    status["autodetect"] = bool(getattr(config, "LOCAL_LLM_AUTODETECT", False))
    return status

def fetch_available_models(provider, force=False):
    from config import PROVIDERS, MODEL_CACHE, has_credentials
    if not force and provider in MODEL_CACHE:
        return MODEL_CACHE[provider]

    if provider == "openrouter":
        urls = ["https://openrouter.ai/api/v1/models"]
    elif provider == "google_ai_studio":
        if not has_credentials(provider):
            MODEL_ERRORS[provider] = "GEMINI_API_KEY / GOOGLE_AI_STUDIO_KEY не найден (проверьте также суффиксы _1…_9)"
            return []
        urls = ["https://generativelanguage.googleapis.com/v1beta/models"]
    elif provider == "groq":
        urls = ["https://api.groq.com/openai/v1/models"]
    elif provider == "cerebras":
        urls = ["https://api.cerebras.ai/v1/models"]
    elif provider == "pollinations":
        # Публичный каталог Pollinations не требует ключа.
        urls = ["https://gen.pollinations.ai/v1/models"]
    elif provider == "openai_compatible":
        # llama-server, Ollama, LM Studio, vLLM, LocalAI и другие.
        # local_providers знает нативные каталоги (Ollama /api/tags,
        # LM Studio /api/v0/models) и отдаёт контекст, семейство и возможности.
        discovered = local_providers.discover(force=force)
        if discovered.get("models"):
            models = [_local_entry_to_model(entry) for entry in discovered["models"]]
            MODEL_CACHE[provider] = models
            MODEL_ERRORS.pop(provider, None)
            return models
        MODEL_ERRORS[provider] = "; ".join(discovered.get("errors", [])[-2:]) \
            or "Local catalogue unavailable"
        urls = _local_catalog_urls()
    elif provider == "local_demo":
        import local_llm
        models = local_llm.models_catalog()
        MODEL_CACHE[provider] = models
        MODEL_ERRORS.pop(provider, None)
        return models
    else:
        return [normalize_model(m, provider) for m in PROVIDERS[provider]["models"]]

    headers = provider_api_headers(provider)
    if headers is None:
        return []

    last_error = None
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=8)
            resp.raise_for_status()
            if provider == "google_ai_studio":
                raw = resp.json().get("models", [])
                models = []
                for m in raw:
                    mid = str(m.get("name", "")).removeprefix("models/")
                    if mid and "generateContent" in (m.get("supportedGenerationMethods") or []):
                        models.append(normalize_model({
                            "id": mid,
                            "name": m.get("displayName") or mid,
                            "description": m.get("description", ""),
                            "context_length": m.get("inputTokenLimit", 0),
                            "pricing": {"prompt": "0", "completion": "0"},
                        }, provider))
            else:
                raw = resp.json().get("data", resp.json().get("models", []))
                # Groq can include inactive/retired IDs in some catalog responses.
                # Do not offer them in the model picker.
                models = [
                    normalize_model(m, provider)
                    for m in raw
                    if (m.get("id") or m.get("name"))
                    and (provider != "groq" or m.get("active", True) is not False)
                ]

            # The router alias is not part of OpenRouter's ordinary model list,
            # but is a documented free-only endpoint and must remain selectable.
            if provider == "openrouter" and not any(
                    item.get("id") == "openrouter/free" for item in models):
                models.append(normalize_model({
                    "id": "openrouter/free",
                    "name": "OpenRouter Free Models Router",
                    "free": True,
                }, provider))

            # NovaMind text chat is free-only: paid/unknown cloud models are
            # neither shown in settings nor eligible for failover.
            models = [item for item in models
                      if is_free_ai_model(provider, item.get("id", ""), item)]
            if models:
                MODEL_CACHE[provider] = models
                MODEL_ERRORS.pop(provider, None)
                return models

            last_error = f"{url}: каталог пуст"
        except (requests.RequestException, ValueError, TypeError) as exc:
            last_error = f"{url}: {exc}"
            if provider != "openai_compatible":
                break

    logger.warning("Model catalog error (%s): %s", provider, last_error)
    MODEL_ERRORS[provider] = last_error or "Каталог моделей недоступен"

    # Local provider must remain selectable even if its catalog endpoint
    # is unavailable. The selected model is sent to the same chat endpoint.
    if provider == "openai_compatible":
        fallback = _local_model_fallback()
    else:
        fallback = [
            normalize_model(m, provider)
            for m in PROVIDERS.get(provider, {}).get("models", [])
            if m.get("id")
        ]

    if fallback:
        MODEL_CACHE[provider] = fallback
        return fallback
    return []

# ---------- Генеративные media-модели ----------
GOOGLE_MEDIA_MODELS = [
    {"id":"gemini-3.1-flash-image","name":"Nano Banana 2","type":"image","free":False,"price":"paid","description":"Генерация и редактирование изображений."},
    {"id":"gemini-3-pro-image","name":"Nano Banana Pro","type":"image","free":False,"price":"paid","description":"Профессиональная генерация изображений."},
    {"id":"gemini-3.8-flash-tts","name":"Gemini 3.8 Flash TTS","type":"audio","free":True,"price":"free-tier","description":"Синтез речи из текста; есть бесплатный тариф с лимитами."},
    {"id":"gemini-3.8-flash-lite-tts","name":"Gemini 3.8 Flash-Lite TTS","type":"audio","free":True,"price":"free-tier","description":"Экономичный TTS; есть бесплатный тариф с лимитами."},
    {"id":"veo-3.1-generate-preview","name":"Veo 3.1","type":"video","free":False,"price":"paid","description":"Генерация видео с синхронизированным аудио; API — платный тариф."},
    {"id":"veo-3.1-lite-generate-preview","name":"Veo 3.1 Lite","type":"video","free":False,"price":"paid","description":"Экономичная генерация и редактирование видео."},
    {"id":"gemini-omni-1.1-flash","name":"Gemini Omni Flash","type":"video","free":False,"price":"paid","description":"Генерация и редактирование видео с нативным аудио."},
]

def media_models_catalog(media_type="all", provider="all", force=False):
    """Единый каталог image/audio/video. Pollinations берётся из live API."""
    try:
        import media_generation as mg
        models = mg.media_catalog(
            media_type=media_type,
            provider=provider,
            include_paid=True,
            include_unknown=True,
            include_trial=True,
            refresh=force,
        )
        return models
    except Exception as exc:
        MODEL_ERRORS["media"] = str(exc)
        return []


def provider_status():
    """Какие провайдеры реально подключены (есть ключ или ключ не нужен)."""
    from config import PROVIDERS, has_credentials
    return {provider: has_credentials(provider) for provider in PROVIDERS}
