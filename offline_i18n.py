"""
offline_i18n.py — texts of the built-in offline helper in the interface language.

The offline helper (``local_llm.py``) is deterministic: it computes, converts,
reads files and hashes text without any model. Its *prompts* do not exist, but
its answers are still user-facing, so they follow the same rule as model
answers: English source text plus translations, selected by
``language_policy.current_language()``.

English is the base language. ``ru`` and ``tg`` are translated; any other
language falls back to English instead of returning a half-translated mix.
"""
from __future__ import annotations

import language_policy

def _tpl(lang: str, key: str) -> str:
    table = MESSAGES.get(lang) or MESSAGES["en"]
    return table.get(key) or MESSAGES["en"].get(key) or ""


MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "disclaimer": ("— NovaMind offline mode: no internet and no API keys. This is a "
                       "deterministic helper, not a language model."),
        "setup_hint": (
            "To get full answers to any question, connect a model:\n"
            "- an API key in `.env`: `GROQ_API_KEY`, `GEMINI_API_KEY`, "
            "`OPENROUTER_API_KEY` or `CEREBRAS_API_KEY`;\n"
            "- or a local server: `OPENAI_BASE_URL=http://127.0.0.1:8080/v1` "
            "(llama.cpp / Ollama / LM Studio) and choose the `openai_compatible` "
            "provider in the settings."
        ),
        "empty_query": "empty request",
        "percent_of": "{part}% of {whole} = **{value}**",
        "time_now": ("It is now **{time}** ({zone}, UTC{offset}). UTC: {utc}."),
        "date_today": "Today is **{date}**, {weekday}. Time: {time} ({zone}).",
        "local_time": "local time",
        "local": "local",
        "system_line": "**System:** {system} {release} ({machine})",
        "python_line": "**Python:** {version}",
        "device_line": "**Device:** {device}",
        "server_time_line": "**Server time:** {time}",
        "unknown": "unknown",
        "words": "Words: **{count}**",
        "chars": "Characters: **{count}** (the command itself is not counted — only your text)",
        "base64_decoded": "Base64 → text:\n\n```\n{text}\n```",
        "base64_encoded": "Text → base64:\n\n```\n{text}\n```",
        "invalid_json": "This is not valid JSON: {error}",
        "file_not_found": "File `{name}` was not found in the working folder.",
        "file_read_error": "Could not read the file: {error}",
        "reasoning_header": "Plan:",
        "reasoning_step_1": "Determine the request type: arithmetic, units, date, text or a general question.",
        "reasoning_step_math": "The request looks like arithmetic — computing with AST, without eval.",
        "reasoning_step_none": "No ready offline tool applies — stating the limitations honestly.",
        "fallback": (
            "**Offline mode: there is no language model.**\n\n"
            "Question: \"{prompt}\"\n\n"
            "What I can do right now:\n"
            "- calculate: `2+2*2`, `15% of 200`;\n"
            "- convert units: `10 km to miles`, `20 C to F`;\n"
            "- show the date and time: `what time is it`;\n"
            "- process text: `sha256: hello`, `base64: hello`, `json: {{\"a\":1}}`.\n\n"
            "{setup}\n\n{disclaimer}"
        ),
        "project_answer": (
            "**Offline mode: there is no language model**, but I do know the project layout "
            "exactly.\n\n"
            "**Repository root**\n"
            "- `app.py` — Flask entry point: creates the app, registers blueprints, starts the server.\n"
            "- `config.py` — AI providers, models, prompt defaults, runtime settings.\n"
            "- `ai_providers.py` — the single model layer (Groq, Gemini, OpenRouter, Cerebras, "
            "OpenAI-compatible local servers and the offline helper).\n"
            "- `language_policy.py` — appends the answer-language directive to every system prompt.\n"
            "- `local_providers.py` — Ollama / LM Studio / llama.cpp / vLLM discovery and capabilities.\n"
            "- `database.py` — SQLite: chats, messages, step traces, agent runs, schedules.\n"
            "- `identity.py`, `security.py` — chat owner, limits, permissions.\n"
            "- `mcp_registry.py`, `mcp_chat.py`, `memory.py`, `skills_registry.py`, `scheduler.py` — "
            "MCP servers, memory, skills and scheduled jobs.\n"
            "- `media_generation.py`, `media_agent.py`, `tts_service.py` — images, video, audio and speech.\n"
            "- `local_llm.py` — this deterministic offline helper.\n\n"
            "**HTTP layer (`routes/`)**\n"
            "`agent.py` — NDJSON agent stream, `chat.py` — ordinary chat and `/send_stream`, "
            "`terminal.py` — Linux sandbox, `search.py` — web search, `media.py` — media, "
            "`admin.py` — sign-in and settings.\n\n"
            "**ReAct agent (`agent/`)**\n"
            "`loop.py` drives the model → tool → observation → answer cycle, `verification.py` "
            "helps judge the result honestly, `workspace.py` isolates working folders, and "
            "`runtime.py` holds shared session and tool opt-in rules. `tool_registry.py` is the "
            "single tool catalogue and dispatch point; `ai_providers.py` is the single model layer.\n\n"
            "**Frontend (`static/`, `templates/`)**\n"
            "`app.js` renders the chat and the answer, `linux.js` parses the NDJSON stream, "
            "`i18n.js` switches the interface language, `style.css` holds the styling, and the "
            "remaining files cover settings, MCP, media, speech and accordions.\n\n"
            "**Checks (`tests/`)**\n"
            "PyTest suites for the engine, routes, search, terminal, MCP and media, plus a "
            "jsdom smoke test for the frontend.\n\n"
            "In short: a thin Flask layer over one AI provider abstraction and one agent engine; "
            "every user-facing capability lives in its own route and the state is kept in SQLite.\n\n"
            "For a full answer to any other question, connect a model:\n{setup}\n\n{disclaimer}"
        ),
    },
    "ru": {
        "disclaimer": ("— офлайн-режим NovaMind: без интернета и без API-ключей. Это "
                       "детерминированный помощник, а не языковая модель."),
        "setup_hint": (
            "Чтобы получить полноценные ответы на любые вопросы, подключите модель:\n"
            "- ключ в `.env`: `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` или "
            "`CEREBRAS_API_KEY`;\n"
            "- либо локальный сервер: `OPENAI_BASE_URL=http://127.0.0.1:8080/v1` "
            "(llama.cpp / Ollama / LM Studio) и выберите провайдера `openai_compatible` "
            "в настройках."
        ),
        "empty_query": "пустой запрос",
        "percent_of": "{part}% от {whole} = **{value}**",
        "time_now": ("Сейчас **{time}** ({zone}, UTC{offset}). UTC: {utc}."),
        "date_today": "Сегодня **{date}**, {weekday}. Время: {time} ({zone}).",
        "local_time": "локальное время",
        "local": "локальное",
        "system_line": "**Система:** {system} {release} ({machine})",
        "python_line": "**Python:** {version}",
        "device_line": "**Устройство:** {device}",
        "server_time_line": "**Время сервера:** {time}",
        "unknown": "неизвестно",
        "words": "Слов: **{count}**",
        "chars": "Символов: **{count}** (без учёта служебной части команды — считаю сам текст)",
        "base64_decoded": "Base64 → текст:\n\n```\n{text}\n```",
        "base64_encoded": "Текст → base64:\n\n```\n{text}\n```",
        "invalid_json": "Это не валидный JSON: {error}",
        "file_not_found": "Файл `{name}` не найден в рабочей папке.",
        "file_read_error": "Не удалось прочитать файл: {error}",
        "reasoning_header": "План:",
        "reasoning_step_1": ("Определяю тип запроса: арифметика, единицы, дата, текст или общий вопрос."),
        "reasoning_step_math": "Запрос похож на арифметику — считаю по AST без eval.",
        "reasoning_step_none": "Готового офлайн-инструмента нет — честно сообщаю ограничения.",
        "fallback": (
            "**Офлайн-режим: языковой модели нет.**\n\n"
            "Вопрос: «{prompt}»\n\n"
            "Я могу прямо сейчас:\n"
            "- посчитать: `2+2*2`, `15% от 200`;\n"
            "- перевести единицы: `10 км в мили`, `20 C в F`;\n"
            "- показать дату и время: `сколько времени`;\n"
            "- обработать текст: `sha256: привет`, `base64: привет`, `json: {{\"a\":1}}`.\n\n"
            "{setup}\n\n{disclaimer}"
        ),
        "project_answer": (
            "**Офлайн-режим: языковой модели нет**, но устройство проекта я знаю точно.\n\n"
            "**Корень репозитория**\n"
            "- `app.py` — точка входа Flask: создаёт приложение, регистрирует blueprints, поднимает сервер.\n"
            "- `config.py` — провайдеры ИИ, модели, системный промпт, runtime-настройки.\n"
            "- `ai_providers.py` — единый слой обращения к моделям (Groq, Gemini, OpenRouter, "
            "Cerebras, OpenAI-совместимые, локальный сервер и офлайн-контур).\n"
            "- `language_policy.py` — дописывает к каждому системному промпту директиву о языке ответа.\n"
            "- `local_providers.py` — поиск и возможности Ollama / LM Studio / llama.cpp / vLLM.\n"
            "- `database.py` — SQLite: чаты, сообщения, трассы шагов, запуски агента, расписания.\n"
            "- `identity.py`, `security.py` — владелец чата, лимиты, права.\n"
            "- `mcp_registry.py`, `mcp_chat.py`, `memory.py`, `skills_registry.py`, `scheduler.py` — "
            "MCP-серверы, память, навыки и задачи по расписанию.\n"
            "- `media_generation.py`, `media_agent.py`, `tts_service.py` — картинки, видео, аудио и озвучка.\n"
            "- `local_llm.py` — вот этот детерминированный офлайн-помощник.\n\n"
            "**HTTP-слой (`routes/`)**\n"
            "`agent.py` — NDJSON-поток агента, `chat.py` — обычный чат и `/send_stream`, "
            "`terminal.py` — песочница Linux, `search.py` — поиск, `media.py` — медиа, "
            "`admin.py` — вход и настройки.\n\n"
            "**ReAct-агент (`agent/`)**\n"
            "`loop.py` управляет циклом «модель → инструмент → наблюдение → ответ», "
            "`verification.py` помогает честно оценить результат, `workspace.py` изолирует "
            "рабочие каталоги, а `runtime.py` содержит общие правила работы с сессией и "
            "opt-in инструментов. `tool_registry.py` — единый каталог и точка запуска "
            "инструментов; `ai_providers.py` — единый слой моделей.\n\n"
            "**Фронтенд (`static/`, `templates/`)**\n"
            "`app.js` рисует чат и ответ, `linux.js` разбирает NDJSON-поток, `i18n.js` "
            "переключает язык интерфейса, `style.css` — оформление, остальные файлы — "
            "настройки, MCP, медиа, озвучка, аккордеоны.\n\n"
            "**Проверки (`tests/`)**\n"
            "PyTest-тесты движка, маршрутов, поиска, терминала, MCP и медиа плюс jsdom-смоук фронтенда.\n\n"
            "Итог: это тонкий Flask-слой поверх единого провайдера ИИ и агентного движка; все "
            "пользовательские возможности вынесены в отдельные маршруты, а состояние хранится в SQLite.\n\n"
            "Чтобы получить развёрнутый ответ на любой другой вопрос, подключите модель:\n"
            "{setup}\n\n{disclaimer}"
        ),
    },
    "tg": {
        "disclaimer": ("— реҷаи офлайнӣ NovaMind: бе интернет ва бе калидҳои API. Ин "
                       "ёрдамчии детерминистӣ аст, на модели забонӣ."),
        "setup_hint": (
            "Барои гирифтани ҷавобҳои пурра ба ҳар савол моделеро пайваст кунед:\n"
            "- калид дар `.env`: `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` ё "
            "`CEREBRAS_API_KEY`;\n"
            "- ё сервери маҳаллӣ: `OPENAI_BASE_URL=http://127.0.0.1:8080/v1` "
            "(llama.cpp / Ollama / LM Studio) ва дар танзимот провайдери "
            "`openai_compatible`-ро интихоб кунед."
        ),
        "empty_query": "дархости холӣ",
        "percent_of": "{part}% аз {whole} = **{value}**",
        "time_now": "Ҳоло **{time}** ({zone}, UTC{offset}). UTC: {utc}.",
        "date_today": "Имрӯз **{date}**, {weekday}. Вақт: {time} ({zone}).",
        "local_time": "вақти маҳаллӣ",
        "local": "маҳаллӣ",
        "system_line": "**Система:** {system} {release} ({machine})",
        "python_line": "**Python:** {version}",
        "device_line": "**Дастгоҳ:** {device}",
        "server_time_line": "**Вақти сервер:** {time}",
        "unknown": "номаълум",
        "words": "Калимаҳо: **{count}**",
        "chars": "Аломатҳо: **{count}** (қисми хидматии фармон ҳисоб намешавад — худи матн ҳисоб мешавад)",
        "base64_decoded": "Base64 → матн:\n\n```\n{text}\n```",
        "base64_encoded": "Матн → base64:\n\n```\n{text}\n```",
        "invalid_json": "Ин JSON-и дуруст нест: {error}",
        "file_not_found": "Файли `{name}` дар папкаи корӣ ёфт нашуд.",
        "file_read_error": "Хондани файл муяссар нашуд: {error}",
        "reasoning_header": "Нақша:",
        "reasoning_step_1": "Навъи дархостро муайян мекунам: арифметика, воҳидҳо, сана, матн ё саволи умумӣ.",
        "reasoning_step_math": "Дархост ба арифметика монанд аст — бо AST бе eval ҳисоб мекунам.",
        "reasoning_step_none": "Асбоби омодаи офлайнӣ нест — маҳдудиятҳоро рост мегӯям.",
        "fallback": (
            "**Реҷаи офлайнӣ: модели забонӣ нест.**\n\n"
            "Савол: «{prompt}»\n\n"
            "Ҳозир ман метавонам:\n"
            "- ҳисоб кунам: `2+2*2`, `15% аз 200`;\n"
            "- воҳидҳоро табдил диҳам: `10 км ба мил`, `20 C ба F`;\n"
            "- сана ва вақтро нишон диҳам: `соат чанд аст`;\n"
            "- матнро коркард кунам: `sha256: салом`, `base64: салом`, `json: {{\"a\":1}}`.\n\n"
            "{setup}\n\n{disclaimer}"
        ),
        "project_answer": (
            "**Реҷаи офлайнӣ: модели забонӣ нест**, аммо сохтори лоиҳаро дақиқ медонам.\n\n"
            "**Решаи репозиторий**\n"
            "- `app.py` — нуқтаи вуруди Flask: барнома месозад, blueprint-ҳоро сабт мекунад, серверро ба кор медарорад.\n"
            "- `config.py` — провайдерҳои ИИ, моделҳо, промпти системавӣ, танзимоти runtime.\n"
            "- `ai_providers.py` — қабати ягонаи муроҷиат ба моделҳо (Groq, Gemini, OpenRouter, "
            "Cerebras, OpenAI-мувофиқ, сервери маҳаллӣ ва контури офлайнӣ).\n"
            "- `language_policy.py` — ба ҳар промпти системавӣ дастури забони ҷавобро илова мекунад.\n"
            "- `local_providers.py` — ҷустуҷӯ ва имкониятҳои Ollama / LM Studio / llama.cpp / vLLM.\n"
            "- `database.py` — SQLite: чатҳо, паёмҳо, трассаҳои қадамҳо, иҷрои агент, ҷадвалҳо.\n"
            "- `identity.py`, `security.py` — соҳиби чат, лимитҳо, ҳуқуқҳо.\n"
            "- `mcp_registry.py`, `mcp_chat.py`, `memory.py`, `skills_registry.py`, `scheduler.py` — "
            "серверҳои MCP, хотира, малакаҳо ва вазифаҳои ҷадвалӣ.\n"
            "- `media_generation.py`, `media_agent.py`, `tts_service.py` — тасвир, видео, аудио ва овоз.\n"
            "- `local_llm.py` — ҳамин ёрдамчии детерминистии офлайнӣ.\n\n"
            "**Қабати HTTP (`routes/`)**\n"
            "`agent.py` — ҷараёни NDJSON-и агент, `chat.py` — чати оддӣ ва `/send_stream`, "
            "`terminal.py` — санҷишгоҳи Linux, `search.py` — ҷустуҷӯ, `media.py` — медиа, "
            "`admin.py` — вуруд ва танзимот.\n\n"
            "**Агенти ReAct (`agent/`)**\n"
            "`loop.py` давраи «модел → асбоб → мушоҳида → ҷавоб»-ро идора мекунад, "
            "`verification.py` ба арзёбии рости натиҷа кӯмак мекунад, `workspace.py` "
            "папкаҳои корӣро ҷудо мекунад, ва `runtime.py` қоидаҳои умумии сессия ва "
            "opt-in асбобҳоро дар бар мегирад. `tool_registry.py` — феҳристи ягона ва "
            "нуқтаи иҷрои асбобҳо; `ai_providers.py` — қабати ягонаи моделҳо.\n\n"
            "**Фротенд (`static/`, `templates/`)**\n"
            "`app.js` чат ва ҷавобро мекашад, `linux.js` ҷараёни NDJSON-ро таҳлил мекунад, "
            "`i18n.js` забони интерфейсро иваз мекунад, `style.css` — ороиш, файлҳои "
            "боқимонда — танзимот, MCP, медиа, овоз, аккордеонҳо.\n\n"
            "**Санҷишҳо (`tests/`)**\n"
            "Тестҳои PyTest барои муҳаррик, маршрутҳо, ҷустуҷӯ, терминал, MCP ва медиа "
            "ҳамчунин смоуки jsdom барои фротенд.\n\n"
            "Хулоса: ин қабати тунуки Flask болои провайдери ягонаи ИИ ва муҳаррики агент аст; "
            "ҳамаи имкониятҳои корбарӣ ба маршрутҳои алоҳида бароварда шудаанд ва ҳолат дар SQLite нигоҳ дошта мешавад.\n\n"
            "Барои ҷавоби пурра ба ҳар саволи дигар моделеро пайваст кунед:\n{setup}\n\n{disclaimer}"
        ),
    },
}

WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "ru": ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"],
    "tg": ["душанбе", "сешанбе", "чоршанбе", "панҷшанбе", "ҷумъа", "шанбе", "якшанбе"],
}

UNIT_CATEGORIES = {
    "en": {"length": "length", "mass": "mass", "volume": "volume", "speed": "speed"},
    "ru": {"length": "длина", "mass": "масса", "volume": "объём", "speed": "скорость"},
    "tg": {"length": "дарозӣ", "mass": "вазн", "volume": "ҳаҷм", "speed": "суръат"},
}

#: Offline knowledge entries. ``triggers`` are matched in any language so a user
#: can ask in Russian while the interface is English and still get an answer.
KNOWLEDGE: list[dict] = [
    {
        "triggers": ("hello", "hi ", "hey", "привет", "салом", "здравствуй"),
        "text": {
            "en": ("Hello! I am the NovaMind offline helper. I can calculate, convert units, "
                   "show the date and time and process text. Connect a model for answers to "
                   "general questions."),
            "ru": ("Привет! Я офлайн-помощник NovaMind. Могу посчитать, перевести единицы, "
                   "показать дату/время и обработать текст. Для ответов на общие вопросы "
                   "подключите модель."),
            "tg": ("Салом! Ман ёрдамчии офлайнии NovaMind ҳастам. Метвонам ҳисоб кунам, "
                   "воҳидҳоро табдил диҳам, сана/вақтро нишон диҳам ва матнро коркард кунам. "
                   "Барои ҷавоб ба саволҳои умумӣ моделеро пайваст кунед."),
        },
    },
    {
        "triggers": ("who are you", "кто ты", "ту кистӣ", "что ты за"),
        "text": {
            "en": ("I am the NovaMind offline loop. I work without a network or keys: "
                   "arithmetic, units, date and time, text. I am not a language model."),
            "ru": ("Я офлайн-контур NovaMind. Работаю без сети и ключей: арифметика, единицы, "
                   "дата и время, текст. Языковой моделью не являюсь."),
            "tg": ("Ман контури офлайнии NovaMind ҳастам. Бе шабака ва калидҳо кор мекунам: "
                   "арифметика, воҳидҳо, сана ва вақт, матн. Ман модели забонӣ нестам."),
        },
    },
    {
        "triggers": ("what can you do", "что ты умеешь", "чиро метавонӣ", "возможности"),
        "text": {
            "en": ("**The offline mode can:**\n\n"
                   "- 🧮 arithmetic and percentages: `2+2*2`, `15% of 200`\n"
                   "- 📏 units: `10 km to miles`, `20 C to F`, `5 kg to pounds`\n"
                   "- 🕒 date and time: `what time is it`, `what day is it today`\n"
                   "- 🔤 text: `how many words in …`, `reverse …`, `base64: …`, `sha256: …`, `json: …`\n\n"
                   "Everything else needs a model — an API key in `.env` or a local llama-server."),
            "ru": ("**Офлайн-режим умеет:**\n\n"
                   "- 🧮 арифметика и проценты: `2+2*2`, `15% от 200`\n"
                   "- 📏 единицы: `10 км в мили`, `20 C в F`, `5 кг в фунты`\n"
                   "- 🕒 дата и время: `сколько времени`, `какой сегодня день`\n"
                   "- 🔤 текст: `сколько слов в …`, `переверни …`, `base64: …`, `sha256: …`, `json: …`\n\n"
                   "Для остального нужна модель — ключ в `.env` или локальный llama-server."),
            "tg": ("**Реҷаи офлайнӣ метавонад:**\n\n"
                   "- 🧮 арифметика ва фоизҳо: `2+2*2`, `15% аз 200`\n"
                   "- 📏 воҳидҳо: `10 км ба мил`, `20 C ба F`, `5 кг ба фунт`\n"
                   "- 🕒 сана ва вақт: `соат чанд аст`, `имрӯз чанд рӯз аст`\n"
                   "- 🔤 матн: `дар … чанд калима`, `баръакс …`, `base64: …`, `sha256: …`, `json: …`\n\n"
                   "Барои боқимонда модел лозим аст — калид дар `.env` ё llama-server-и маҳаллӣ."),
        },
    },
    {
        "triggers": ("help", "помощь", "кумак", "команды"),
        "text": {
            "en": ("Type `/help` for the list of chat commands. Offline capabilities: `/help` → "
                   "the \"Offline mode\" section."),
            "ru": ("Введите `/help` — список команд чата. Офлайн-возможности: `/help` → раздел "
                   "«Офлайн-режим»."),
            "tg": ("Барои рӯйхати фармонҳои чат `/help`-ро ворид кунед. Имконоти офлайнӣ: "
                   "`/help` → бахши «Реҷаи офлайнӣ»."),
        },
    },
]


def language(lang: str | None = None) -> str:
    """Resolve a supported text language (en/ru/tg), falling back to English."""
    code = language_policy.normalize(lang) if lang else language_policy.current_language()
    return code if code in MESSAGES else "en"


def t(key: str, lang: str | None = None, **values) -> str:
    """Translate ``key`` and format placeholders (missing placeholders are kept)."""
    template = _tpl(language(lang), key)
    if not values:
        return template
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


def weekday(index: int, lang: str | None = None) -> str:
    names = WEEKDAYS.get(language(lang)) or WEEKDAYS["en"]
    return names[max(0, min(6, int(index)))]


def unit_category(name: str, lang: str | None = None) -> str:
    table = UNIT_CATEGORIES.get(language(lang)) or UNIT_CATEGORIES["en"]
    return table.get(name, name)


def knowledge_answer(text: str, lang: str | None = None) -> str | None:
    """Match a greeting/identity/capabilities question in any language."""
    lowered = " " + str(text or "").lower() + " "
    code = language(lang)
    for entry in KNOWLEDGE:
        for trigger in entry["triggers"]:
            if trigger in lowered:
                return entry["text"].get(code) or entry["text"]["en"]
    return None


#: Markers that identify an offline answer, in every supported language.
OFFLINE_MARKERS: tuple[str, ...] = (
    "offline mode", "there is no language model", "deterministic helper",
    "офлайн-режим novamind", "офлайн-режим: языковой модели нет",
    "детерминированный помощник", "языковой модели нет",
    "реҷаи офлайнӣ", "модели забонӣ нест",
)
