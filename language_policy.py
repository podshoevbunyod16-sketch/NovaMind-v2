"""
language_policy.py — one place that decides which language the model answers in.

NovaMind keeps every prompt (system messages, ReAct rules, search instructions,
tool observations, reasoning pre-passes) in **English**. The only per-user part
is a short English directive appended to the system prompt:

    ANSWER LANGUAGE (required): Russian — Русский (ru).
    Write every user-visible part of your reply in Russian ...

So a user who selects English in the interface gets an English answer, a user
who selects Russian gets a Russian answer, Tajik gets Tajik, and any other
language added to ``LANGUAGES`` behaves exactly the same way — without touching
a single prompt file.

Resolution order for the active language (first non-empty wins):

1. an explicit ``language``/``ui_language``/``lang`` field of the current JSON
   request (useful for API clients that answer for several users at once);
2. the ``X-UI-Language`` request header;
3. the visitor's Flask session (set when the interface language is changed);
4. ``config.ui_language`` — the server default;
5. ``DEFAULT_LANGUAGE``.

The module deliberately has no Flask import at module import time: it can be
used by CLI tools, the Telegram bot and tests without an application context.
"""
from __future__ import annotations

import contextlib
import contextvars
import re

#: Language code → (English name, native name).
#: The English name is what the model sees; the native name is for the UI.
#: Adding a language here is enough for prompts to answer in it.
LANGUAGES: dict[str, tuple[str, str]] = {
    "en": ("English", "English"),
    "ru": ("Russian", "Русский"),
    "tg": ("Tajik", "Тоҷикӣ"),
    "uz": ("Uzbek", "Oʻzbekcha"),
    "kk": ("Kazakh", "Қазақша"),
    "ky": ("Kyrgyz", "Кыргызча"),
    "tk": ("Turkmen", "Türkmençe"),
    "az": ("Azerbaijani", "Azərbaycanca"),
    "tr": ("Turkish", "Türkçe"),
    "fa": ("Persian", "فارسی"),
    "ps": ("Pashto", "پښتو"),
    "ku": ("Kurdish", "Kurdî"),
    "ar": ("Arabic", "العربية"),
    "ur": ("Urdu", "اردو"),
    "he": ("Hebrew", "עברית"),
    "hi": ("Hindi", "हिन्दी"),
    "bn": ("Bengali", "বাংলা"),
    "mr": ("Marathi", "मराठी"),
    "gu": ("Gujarati", "ગુજરાતી"),
    "pa": ("Punjabi", "ਪੰਜਾਬੀ"),
    "ta": ("Tamil", "தமிழ்"),
    "te": ("Telugu", "తెలుగు"),
    "ml": ("Malayalam", "മലയാളം"),
    "si": ("Sinhala", "සිංහල"),
    "ne": ("Nepali", "नेपाली"),
    "zh": ("Chinese", "中文"),
    "ja": ("Japanese", "日本語"),
    "ko": ("Korean", "한국어"),
    "vi": ("Vietnamese", "Tiếng Việt"),
    "th": ("Thai", "ไทย"),
    "lo": ("Lao", "ລາວ"),
    "km": ("Khmer", "ខ្មែរ"),
    "my": ("Burmese", "မြန်မာ"),
    "id": ("Indonesian", "Bahasa Indonesia"),
    "ms": ("Malay", "Bahasa Melayu"),
    "tl": ("Filipino", "Filipino"),
    "sw": ("Swahili", "Kiswahili"),
    "am": ("Amharic", "አማርኛ"),
    "uk": ("Ukrainian", "Українська"),
    "be": ("Belarusian", "Беларуская"),
    "pl": ("Polish", "Polski"),
    "cs": ("Czech", "Čeština"),
    "sk": ("Slovak", "Slovenčina"),
    "ro": ("Romanian", "Română"),
    "hu": ("Hungarian", "Magyar"),
    "bg": ("Bulgarian", "Български"),
    "sr": ("Serbian", "Српски"),
    "hr": ("Croatian", "Hrvatski"),
    "sl": ("Slovenian", "Slovenščina"),
    "bs": ("Bosnian", "Bosanski"),
    "mk": ("Macedonian", "Македонски"),
    "sq": ("Albanian", "Shqip"),
    "el": ("Greek", "Ελληνικά"),
    "de": ("German", "Deutsch"),
    "fr": ("French", "Français"),
    "es": ("Spanish", "Español"),
    "pt": ("Portuguese", "Português"),
    "it": ("Italian", "Italiano"),
    "nl": ("Dutch", "Nederlands"),
    "sv": ("Swedish", "Svenska"),
    "no": ("Norwegian", "Norsk"),
    "da": ("Danish", "Dansk"),
    "fi": ("Finnish", "Suomi"),
    "is": ("Icelandic", "Íslenska"),
    "et": ("Estonian", "Eesti"),
    "lv": ("Latvian", "Latviešu"),
    "lt": ("Lithuanian", "Lietuvių"),
    "hy": ("Armenian", "Հայերեն"),
    "ka": ("Georgian", "ქართული"),
    "mn": ("Mongolian", "Монгол"),
    "af": ("Afrikaans", "Afrikaans"),
    "ca": ("Catalan", "Català"),
    "gl": ("Galician", "Galego"),
    "eu": ("Basque", "Euskara"),
    "cy": ("Welsh", "Cymraeg"),
    "ga": ("Irish", "Gaeilge"),
    "mt": ("Maltese", "Malti"),
    "eo": ("Esperanto", "Esperanto"),
    "yi": ("Yiddish", "ייִדיש"),
}

#: Languages that ship a complete interface translation (static/i18n.js).
#: The answer language itself can be any code from ``LANGUAGES``.
UI_LANGUAGES: tuple[str, ...] = ("tg", "ru", "en")

DEFAULT_LANGUAGE = "tg"

#: Short aliases people and old clients use.
_ALIASES = {
    "tj": "tg", "tajik": "tg", "tojiki": "tg", "тоҷикӣ": "tg",
    "russian": "ru", "русский": "ru", "rus": "ru",
    "english": "en", "английский": "en", "eng": "en",
    "uzbek": "uz", "o'zbek": "uz", "ozbek": "uz",
    "ua": "uk", "ukrainian": "uk",
    "iw": "he", "in": "id", "ji": "yi", "mo": "ro", "nb": "no", "nn": "no",
    "zh-cn": "zh", "zh-hans": "zh", "zh-tw": "zh", "zh-hant": "zh",
    "pt-br": "pt", "pt-pt": "pt", "sr-latn": "sr", "fa-ir": "fa",
}

#: Marker used to recognise (and replace) a directive that is already present,
#: so appending it twice never happens.
MARKER = "NOVA_ANSWER_LANGUAGE"

_DIRECTIVE_RE = re.compile(
    rf"\n*\[{MARKER}\s+[a-zA-Z-]+\].*?(?=\n\n|\Z)", re.S)

_CONTEXT_LANGUAGE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nova_answer_language", default=None)


def normalize(code) -> str:
    """Return a bare, lower-case language code (``ru-RU`` → ``ru``, ``''`` ok)."""
    value = str(code or "").strip().lower().replace("_", "-")
    if not value:
        return ""
    if value in _ALIASES:
        return _ALIASES[value]
    base = value.split("-", 1)[0]
    if base in _ALIASES:
        return _ALIASES[base]
    if base in LANGUAGES:
        return base
    return ""


def is_supported(code) -> bool:
    return bool(normalize(code))


def english_name(code) -> str:
    base = normalize(code)
    return LANGUAGES[base][0] if base else ""


def native_name(code) -> str:
    base = normalize(code)
    return LANGUAGES[base][1] if base else ""


def supported() -> list[dict]:
    """Catalogue for the settings API and the language switch."""
    return [
        {
            "code": code,
            "english_name": english,
            "native_name": native,
            "ui": code in UI_LANGUAGES,
        }
        for code, (english, native) in LANGUAGES.items()
    ]


def directive(code) -> str:
    """English instruction that asks for an answer in ``code``.

    Returns ``""`` for an unknown/empty code: the model then keeps its own
    default instead of being told to answer in a language we cannot name.
    """
    base = normalize(code)
    if not base:
        return ""
    name = LANGUAGES[base][0]
    native = LANGUAGES[base][1]
    label = name if native == name else f"{name} ({native})"
    return (
        f"[{MARKER} {base}] ANSWER LANGUAGE (required): {label} — language code \"{base}\".\n"
        f"Write every user-visible part of your reply in {name}: prose, plans, headings, "
        f"lists, tables, summaries of tool results, warnings, error notes and follow-up "
        f"questions.\n"
        f"Keep exactly as-is: source code, file paths, shell commands, URLs, API, tool and "
        f"parameter names, identifiers and data quoted from tools or web pages.\n"
        f"If the user explicitly asks for another language for a specific answer, follow "
        f"that request for that answer only, then return to {name}."
    )


def apply_to_system(system, code=None) -> str:
    """Append (or refresh) the answer-language directive in a system prompt.

    The prompt itself stays English; only the pinned language code changes.
    ``None`` becomes a system prompt that contains just the directive, so a
    caller without any system message still gets the right answer language.
    """
    text = str(system or "").strip()
    lang = normalize(code) or current_language()
    block = directive(lang)
    if not block:
        return text
    cleaned = _DIRECTIVE_RE.sub("", text).strip()
    return f"{cleaned}\n\n{block}" if cleaned else block


def strip_directive(system) -> str:
    """Remove the injected directive (used by tests and prompt export)."""
    return _DIRECTIVE_RE.sub("", str(system or "")).strip()


def current_language() -> str:
    """Active answer language for the current request/call."""
    forced = _CONTEXT_LANGUAGE.get()
    if forced:
        return normalize(forced) or DEFAULT_LANGUAGE
    for candidate in (_from_request(), _from_session(), _from_config()):
        code = normalize(candidate)
        if code:
            return code
    return DEFAULT_LANGUAGE


def _from_request() -> str:
    """Language explicitly sent by the client (header or JSON body)."""
    try:
        from flask import has_request_context, request
    except Exception:                                     # Flask not installed
        return ""
    if not has_request_context():
        return ""
    try:
        header = request.headers.get("X-UI-Language") or request.headers.get("X-Nova-Language")
        if header:
            return header
    except Exception:
        pass
    try:
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            if isinstance(payload, dict):
                for key in ("ui_language", "language", "lang"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        return value
    except Exception:
        pass
    return ""


def _from_session() -> str:
    try:
        from flask import has_request_context, session
    except Exception:
        return ""
    if not has_request_context():
        return ""
    try:
        return str(session.get("ui_language") or "")
    except Exception:
        return ""


def _from_config() -> str:
    try:
        import config
        return str(getattr(config, "ui_language", "") or "")
    except Exception:
        return ""


def set_session_language(code) -> str:
    """Remember the visitor's language in the session (best effort)."""
    base = normalize(code)
    if not base:
        return ""
    try:
        from flask import has_request_context, session
        if has_request_context():
            session.permanent = True
            session["ui_language"] = base
    except Exception:
        pass
    return base


@contextlib.contextmanager
def use_language(code):
    """Force an answer language inside a block (background jobs, tests)."""
    token = _CONTEXT_LANGUAGE.set(normalize(code) or None)
    try:
        yield normalize(code)
    finally:
        _CONTEXT_LANGUAGE.reset(token)


def apply_to_messages(messages, code=None):
    """Return a copy of an OpenAI-style message list with one system prompt.

    Used by integrations that pass conversation history directly instead of a
    separate ``system`` argument: existing system messages are merged, so the
    directive is present exactly once.
    """
    lang = normalize(code) or current_language()
    block = directive(lang)
    result = [dict(item) for item in (messages or []) if isinstance(item, dict)]
    if not block:
        return result
    system_parts = [str(item.get("content") or "") for item in result
                    if item.get("role") == "system"]
    rest = [item for item in result if item.get("role") != "system"]
    merged = apply_to_system("\n\n".join(part for part in system_parts if part.strip()),
                             lang)
    return [{"role": "system", "content": merged}] + rest


def pick(*, en: str, ru: str = "", tg: str = "", code=None) -> str:
    """Choose a localised user-facing string for the active language.

    Only for short runtime messages that are appended to an answer (for example
    the honest-completion prefix in ``agent/verification.py``). Prompts must not
    use this helper — they are always English — and languages without a
    translation fall back to English.
    """
    lang = normalize(code) or current_language()
    table = {"en": en, "ru": ru or en, "tg": tg or en}
    return table.get(lang) or en


def summary() -> dict:
    return {
        "default": DEFAULT_LANGUAGE,
        "ui_languages": list(UI_LANGUAGES),
        "languages": len(LANGUAGES),
        "current": current_language(),
    }
