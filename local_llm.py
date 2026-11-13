"""
local_llm.py — офлайн-режим NovaMind.

Зачем: приложение должно отвечать даже без интернета и без API-ключей.
Раньше здесь была заглушка, которая на любой вопрос отвечала «вопрос принят
и разобран (N токенов)»: это выглядело как ответ модели, но не содержало
ни одного факта. Теперь офлайн-режим — честный детерминированный помощник:

  * считает арифметику и проценты (AST, без eval);
  * переводит единицы (длина, вес, температура, скорость, объём);
  * сообщает реальные дату/время и данные системы;
  * делает текстовые операции: длина, слова, регистр, реверс, base64,
    md5/sha1/sha256, pretty-JSON;
  * читает файл из рабочей папки, если он доступен;
  * на всё остальное отвечает прямо: «я не языковая модель, вот что я умею
    и как подключить настоящую модель».

Это НЕ замена большой модели. Настоящий локальный LLM, если он запущен
(llama.cpp / Ollama / LM Studio), подключается через OPENAI_BASE_URL
или автоматически при LOCAL_LLM_AUTODETECT=1.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import operator
import os
import platform
import re
import sys
import time
from datetime import datetime, timezone

import offline_i18n

MODEL_ID = "nova-local-1"
MODEL_NAME = "Nova Local 1 (offline)"
PROVIDER_ID = "local_demo"
PROVIDER_NAME = "Local offline assistant"


def _t(key: str, **values) -> str:
    """Text of the offline helper in the current interface language."""
    return offline_i18n.t(key, **values)


#: English defaults kept as module attributes for integrations that import them.
DISCLAIMER = offline_i18n.t("disclaimer", lang="en")
_SETUP_HINT = offline_i18n.t("setup_hint", lang="en")

_NUMBER = r"[-+]?\d+(?:[.,]\d+)?"
_MATH_CHARS = re.compile(r"^[\d\s.+\-*/%()^,]+$")

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _tokens(text: str) -> int:
    """Грубая оценка длины текста — оставлена для совместимости."""
    return max(1, len(text) // 4)


# ─────────────────────────── арифметика ───────────────────────────

def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 64:
            raise ValueError("Слишком большая степень")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("Недопустимое выражение")


def _pretty_number(value) -> str:
    if isinstance(value, float):
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        return f"{value:.10g}"
    return str(value)


def _math_answer(text: str):
    """Безопасный калькулятор: только числа и арифметические операторы."""
    expr = re.sub(r"[\s=?]+$", "", (text or "").strip())
    expr = re.sub(r"^(посчитай|вычисли|сколько\s+будет|calc|compute)\s*:?\s*", "", expr, flags=re.I)
    expr = expr.replace(",", ".").replace("^", "**").replace("×", "*").replace("÷", "/")
    # «15% от 200» / «15 процентов от 200»
    percent = re.fullmatch(rf"\s*({_NUMBER})\s*%?\s*(?:от|of|аз)\s*({_NUMBER})\s*", expr, re.I)
    if percent:
        part, whole = float(percent.group(1)), float(percent.group(2))
        return _t("percent_of", part=_pretty_number(part), whole=_pretty_number(whole),
                  value=_pretty_number(part * whole / 100))
    if not re.search(r"\d", expr) or not re.search(r"[+\-*/%]", expr):
        return None
    if not _MATH_CHARS.fullmatch(expr):
        return None
    try:
        value = _eval_node(ast.parse(expr, mode="eval"))
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
        return None
    if not isinstance(value, (int, float)):
        return None
    return f"{expr.replace('**', '^')} = **{_pretty_number(value)}**"


# ─────────────────────────── единицы ───────────────────────────

_UNITS = {
    # длина (в метрах)
    "мм": ("length", 0.001), "миллиметр": ("length", 0.001), "миллиметров": ("length", 0.001),
    "см": ("length", 0.01), "сантиметр": ("length", 0.01), "сантиметров": ("length", 0.01),
    "м": ("length", 1.0), "метр": ("length", 1.0), "метров": ("length", 1.0),
    "км": ("length", 1000.0), "километр": ("length", 1000.0), "километров": ("length", 1000.0),
    "mile": ("length", 1609.344), "miles": ("length", 1609.344),
    "миля": ("length", 1609.344), "миль": ("length", 1609.344), "мили": ("length", 1609.344),
    "милях": ("length", 1609.344), "милю": ("length", 1609.344),
    "ft": ("length", 0.3048), "фут": ("length", 0.3048), "футов": ("length", 0.3048),
    "футы": ("length", 0.3048), "футах": ("length", 0.3048),
    "inch": ("length", 0.0254), "inches": ("length", 0.0254), "in": ("length", 0.0254),
    "дюйм": ("length", 0.0254), "дюймов": ("length", 0.0254), "дюймы": ("length", 0.0254),
    "ли": ("length", 500.0),
    "метрах": ("length", 1.0), "метры": ("length", 1.0), "метров": ("length", 1.0),
    "километрах": ("length", 1000.0), "километры": ("length", 1000.0),
    "сантиметрах": ("length", 0.01), "сантиметры": ("length", 0.01),
    "m": ("length", 1.0), "km": ("length", 1000.0), "cm": ("length", 0.01), "mm": ("length", 0.001),
    # вес (в килограммах)
    "г": ("mass", 0.001), "грамм": ("mass", 0.001), "граммов": ("mass", 0.001),
    "кг": ("mass", 1.0), "килограмм": ("mass", 1.0), "килограммов": ("mass", 1.0),
    "т": ("mass", 1000.0), "тонна": ("mass", 1000.0), "тонн": ("mass", 1000.0),
    "lb": ("mass", 0.45359237), "lbs": ("mass", 0.45359237),
    "фунт": ("mass", 0.45359237), "фунтов": ("mass", 0.45359237),
    "фунты": ("mass", 0.45359237), "фунтах": ("mass", 0.45359237),
    "килограммах": ("mass", 1.0), "килограммы": ("mass", 1.0),
    "граммах": ("mass", 0.001), "граммы": ("mass", 0.001),
    "тоннах": ("mass", 1000.0), "тонны": ("mass", 1000.0),
    "oz": ("mass", 0.028349523125), "унция": ("mass", 0.028349523125),
    "kg": ("mass", 1.0), "g": ("mass", 0.001),
    # объём (в литрах)
    "л": ("volume", 1.0), "литр": ("volume", 1.0), "литров": ("volume", 1.0),
    "литры": ("volume", 1.0), "литрах": ("volume", 1.0),
    "мл": ("volume", 0.001), "миллилитр": ("volume", 0.001), "миллилитров": ("volume", 0.001),
    "миллилитры": ("volume", 0.001), "миллилитрах": ("volume", 0.001),
    "галлон": ("volume", 3.785411784), "gallon": ("volume", 3.785411784),
    "l": ("volume", 1.0), "ml": ("volume", 0.001),
    # скорость (в м/с)
    "км/ч": ("speed", 1 / 3.6), "km/h": ("speed", 1 / 3.6),
    "м/с": ("speed", 1.0), "m/s": ("speed", 1.0),
    "mph": ("speed", 0.44704), "миль/ч": ("speed", 0.44704),
}


def _unit_value(name: str, value: float) -> str:
    """Переводит температуру отдельно: там не пропорция, а сдвиг."""
    low = name.lower()
    if low in ("c", "ц", "celsius", "цельсий", "цельсия", "°c"):
        return f"{_pretty_number(value)} °C = **{_pretty_number(value * 9 / 5 + 32)} °F**"
    if low in ("f", "fahrenheit", "фаренгейт", "фаренгейта", "°f"):
        return f"{_pretty_number(value)} °F = **{_pretty_number((value - 32) * 5 / 9)} °C**"
    return ""


def _unit_answer(text: str):
    match = re.fullmatch(
        rf"\s*({_NUMBER})\s*(°?[a-zA-Zа-яА-ЯёЁ/]+)\s*(?:в|into|to|→|->)\s*(°?[a-zA-Zа-яА-ЯёЁ/]+)\s*",
        (text or "").strip(), re.I)
    if not match:
        return None
    raw_value, src, dst = match.groups()
    value = float(raw_value.replace(",", "."))
    temp = _unit_value(src, value)
    if temp and dst.lower() in ("f", "фаренгейт", "°f", "fahrenheit", "c", "ц", "°c", "celsius", "цельсий"):
        return temp
    src_info, dst_info = _UNITS.get(src.lower()), _UNITS.get(dst.lower())
    if not src_info or not dst_info or src_info[0] != dst_info[0]:
        return None
    result = value * src_info[1] / dst_info[1]
    shown = f"{result:.4f}".rstrip("0").rstrip(".") if abs(result) < 1e6 else _pretty_number(result)
    category = offline_i18n.unit_category(src_info[0])
    return f"{_pretty_number(value)} {src} = **{shown} {dst}** ({category})"


# ─────────────────────────── дата / система ───────────────────────────

_TIME_QUERY = re.compile(r"(\bкотор\w*\s+час\b|\bврем\w*|\bчасов\b|\btime\b)", re.I)
_DATE_QUERY = re.compile(r"(\bдат[аыеу]\b|\bчисло\b|\bсегодня\b|\bday\b|\bdate\b)", re.I)


def _datetime_answer(text: str):
    low = (text or "").lower()
    # Границы слов обязательны: иначе «сейчас» внутри служебной инструкции
    # («Ответь сейчас…») включал режим времени и съедал финальный ответ.
    wants_time = _TIME_QUERY.search(low)
    wants_date = _DATE_QUERY.search(low)
    if not (wants_time or wants_date):
        return None
    now_local = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)
    if wants_time and not wants_date:
        return _t("time_now",
                  time=now_local.strftime("%H:%M:%S"),
                  zone=now_local.tzname() or _t("local_time"),
                  offset=now_local.strftime("%z")[:3] + ":00",
                  utc=now_utc.strftime("%H:%M:%S"))
    return _t("date_today",
              date=now_local.strftime("%d.%m.%Y"),
              weekday=offline_i18n.weekday(now_local.weekday()),
              time=now_local.strftime("%H:%M"),
              zone=now_local.tzname() or _t("local"))


_SYSTEM_QUERY = re.compile(r"(\bсистем\w*|\bплатформ\w*|\bpython\b|\bверси\w*|\bos\b|\btermux\b)", re.I)


def _system_answer(text: str):
    if not _SYSTEM_QUERY.search(text or ""):
        return None
    return "\n".join((
        _t("system_line", system=platform.system(), release=platform.release(),
           machine=platform.machine()),
        _t("python_line", version=sys.version.split()[0]),
        _t("device_line", device=platform.node() or _t("unknown")),
        _t("server_time_line", time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ))


# ─────────────────────────── работа с текстом ───────────────────────────

def _payload_after(text: str, prefix: str):
    """Всё, что идёт после «prefix:» — полезная нагрузка текстовой команды."""
    match = re.match(rf"\s*{prefix}\s*:?\s*(.+)$", text or "", re.I | re.S)
    return match.group(1) if match else None


def _text_answer(text: str):
    original = (text or "").strip()
    low = original.lower()

    if re.search(r"(сколько|число)\s+(слов|word)", low):
        words = re.findall(r"\w+", _payload_after(original, r".*?(?:слов|word)") or original)
        return _t("words", count=len(words))
    if re.search(r"(сколько|число)\s+(символ|букв|character)", low):
        payload = _payload_after(original, r".*?(?:символ\w*|букв\w*)") or original
        return _t("chars", count=len(payload))
    if low.startswith("переверни "):
        return "".join(reversed(original[len("переверни "):]))
    if low.startswith("в верхний регистр "):
        return original[len("в верхний регистр "):].upper()
    if low.startswith("в нижний регистр "):
        return original[len("в нижний регистр "):].lower()

    payload = _payload_after(original, r"base64")
    if payload is not None:
        if re.fullmatch(r"[A-Za-z0-9+/=\s]+", payload) and len(payload.strip()) % 4 == 0:
            try:
                decoded = base64.b64decode(payload.strip(), validate=True).decode("utf-8")
                return _t("base64_decoded", text=decoded)
            except Exception:
                pass
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        return _t("base64_encoded", text=encoded)

    for algo in ("md5", "sha1", "sha256"):
        payload = _payload_after(original, algo)
        if payload is not None:
            digest = hashlib.new(algo, payload.encode("utf-8")).hexdigest()
            return f"{algo.upper()}(`{payload[:60]}`) = `{digest}`"

    payload = _payload_after(original, r"json")
    if payload is not None:
        try:
            return "```json\n" + json.dumps(json.loads(payload), ensure_ascii=False, indent=2) + "\n```"
        except ValueError as exc:
            return _t("invalid_json", error=exc)
    return None


#: Вопросы про устройство самого проекта офлайн-контур отвечает по-настоящему:
#: структура репозитория известна ему без языковой модели.
_PROJECT_RE = re.compile(
    r"(?i)(структур\w*\s+проект|как\s+устроен\w*\s+проект|устройств\w*\s+проект|"
    r"архитектур\w*\s+проект|из\s+чего\s+состоит|какие\s+(?:файлы|модули|папки)|"
    r"где\s+(?:лежит|находится|хранится)|про\s+структуру|project\s+structure|"
    r"how\s+is\s+the\s+project\s+(?:structured|organized)|what\s+files|"
    r"architecture\s+of\s+the\s+project|repository\s+layout|сохтори\s+лоиҳа)")

#: Project-structure answer template; localised in offline_i18n.py.
#: (Kept as a module attribute for integrations that import it.)
_PROJECT_ANSWER = offline_i18n.t("project_answer", lang="en")



def _project_answer(text: str):
    """Полный ответ про структуру проекта в офлайн-режиме."""
    if not _PROJECT_RE.search(text or ""):
        return None
    return _t("project_answer").format(setup=_t("setup_hint"),
                                       disclaimer=_t("disclaimer"))


def _file_answer(text: str):
    # «покажи файлы» — это не имя файла: после «покажи» обязательно
    # должно идти слово «файл»/«содержимое», иначе получался ложный ответ
    # «Файл `файлы` не найден».
    match = re.match(
        r"\s*(?:прочитай\s+(?:файл\s+)?|cat\s+|покажи\s+(?:файл|содержимое(?:\s+файла)?)\s+)"
        r"([\w./-]+)\s*$", text or "", re.I)
    if match and match.group(1).lower() in {"файл", "файлы", "файлов", "файла"}:
        return None
    if not match:
        return None
    try:
        from routes.terminal import safe_path
        path = safe_path(match.group(1))
        if not os.path.isfile(path):
            return _t("file_not_found", name=match.group(1))
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            body = handle.read(4000)
        return f"`{match.group(1)}`:\n\n```\n{body}\n```"
    except Exception as exc:
        return _t("file_read_error", error=exc)


# ─────────────────────────── публичный API ───────────────────────────

_META_BRACKETS = re.compile(r"\[[^\]]{10,}\]", re.DOTALL)


def clean_prompt(text: str) -> str:
    """Убирает служебные вставки вида «[Поиск не дал результатов. Отвечай…]»."""
    cleaned = _META_BRACKETS.sub(" ", text or "")
    cleaned = re.sub(r"^(Вопрос|Запрос|Question|Request)\s*:\s*", "", cleaned.strip())
    cleaned = re.sub(r"(Данные из интернета|Результаты поиска|Web data|Search results)"
                     r"\s*:\s*.*$", "", cleaned, flags=re.DOTALL)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


# Служебные сообщения, которые агент дописывает в историю между шагами:
# это не вопрос пользователя, и офлайн-контур не должен их «отвечать».
_INTERNAL_RE = re.compile(
    r"(Не вызывай\s+run|Do not call\s+run|Отвечай по своим знаниям|Answer from your own|"
    r"уже показано пользователю|already shown to the user|"
    r"^Инструменты |^Tools |^SEARCH HINT|^Результат инструмента|^Tool result|"
    r"^Наблюдение|^Observation|^План принят|^Plan accepted)",
    re.I | re.M)


def _looks_internal(text: str) -> bool:
    value = str(text or "").strip()
    return bool(value) and bool(_INTERNAL_RE.search(value))


def _last_user_prompt(messages) -> str:
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "").strip()
        if not content or _looks_internal(content):
            continue
        return content
    return ""


def complete(messages, **_kwargs) -> str:
    """Возвращает текст ответа для списка сообщений в OpenAI-формате.

    Язык ответа — язык интерфейса (language_policy): офлайн-помощник, как и
    обычная модель, отвечает на выбранном языке, а не на жёстко заданном.
    """
    raw = _last_user_prompt(messages) or _t("empty_query")
    prompt = clean_prompt(raw) or _t("empty_query")

    for handler in (_math_answer, _unit_answer, _datetime_answer, _system_answer,
                    _text_answer, _project_answer, _file_answer):
        try:
            answer = handler(prompt)
        except Exception:                     # ни одна команда не должна ронять чат
            answer = None
        if answer:
            return answer

    knowledge = offline_i18n.knowledge_answer(prompt)
    if knowledge:
        return f"{knowledge}\n\n{_t('disclaimer')}"

    shown = prompt[:300] + ("…" if len(prompt) > 300 else "")
    return _t("fallback", prompt=shown, setup=_t("setup_hint"),
              disclaimer=_t("disclaimer"))


def reasoning(prompt: str) -> str:
    """Ход «рассуждений» офлайн-режима — честный, короткий и на языке интерфейса."""
    steps = [_t("reasoning_step_1")]
    steps.append(_t("reasoning_step_math") if _math_answer(clean_prompt(prompt))
                 else _t("reasoning_step_none"))
    return (_t("reasoning_header") + "\n"
            + "\n".join(f"{i}. {text}" for i, text in enumerate(steps, 1)))


def stream(messages, **kwargs):
    """Генератор (kind, text): reasoning-токены, затем токены ответа."""
    prompt = _last_user_prompt(messages)
    if kwargs.get("reasoning"):
        for chunk in _split(reasoning(prompt)):
            yield "reasoning", chunk
            time.sleep(0.012)

    for chunk in _split(complete(messages)):
        yield "token", chunk
        time.sleep(0.012)


def _split(text: str, size: int = 18):
    """Режем текст на кусочки примерно по границам слов — для плавной печати."""
    text = text or ""
    for start in range(0, len(text), size):
        yield text[start:start + size]


#: Маркеры офлайн-ответа на всех поддерживаемых языках.
OFFLINE_MARKERS = offline_i18n.OFFLINE_MARKERS


def looks_offline(text: str) -> bool:
    """Похоже ли это на ответ офлайн-контура, а не на ответ настоящей модели."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in OFFLINE_MARKERS)


def models_catalog():
    return [
        {
            "id": MODEL_ID,
            "name": MODEL_NAME,
            "description": "Offline helper: arithmetic, units, date/time, text. "
                           "No internet and no keys; it is not a language model.",
            "provider": PROVIDER_ID,
            "provider_name": PROVIDER_NAME,
            "context_length": 8192,
            "modality": "text",
            "pricing_status": "free",
            "price_prompt": "0",
            "price_completion": "0",
            "pricing_label": "free (offline)",
            "recommended": True,
            "free": True,
        }
    ]
