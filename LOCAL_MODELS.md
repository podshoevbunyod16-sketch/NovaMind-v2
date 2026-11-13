# Локальные модели и язык ответов

Два связанных изменения:

1. **NovaMind работает с локальными моделями как с облачными.** Ollama, LM Studio,
   llama.cpp, vLLM, LocalAI, Jan, KoboldCpp и text-generation-webui получают те же
   возможности, что Groq и OpenRouter: потоковые ответы, вызов инструментов,
   JSON-режим, рассуждения, vision и учёт токенов.
2. **Все промпты написаны на английском, а ответы — на языке интерфейса.**
   Выбрали English — получили English, переключили на русский — ответ на русском,
   таджикский — на таджикском. Добавить ещё язык можно одной строкой.

---

## 1. Локальные модели

### Поддерживаемые рантаймы

| Рантайм | Адрес по умолчанию | Каталог моделей |
|---|---|---|
| Ollama | `http://127.0.0.1:11434/v1` | `/api/tags` + `/api/show` |
| LM Studio | `http://127.0.0.1:1234/v1` | `/api/v0/models` |
| llama.cpp (`llama-server`) | `http://127.0.0.1:8080/v1` | `/v1/models` |
| vLLM | `http://127.0.0.1:8000/v1` | `/v1/models` |
| LocalAI | `http://127.0.0.1:8080/v1` | `/v1/models` |
| Jan | `http://127.0.0.1:1337/v1` | `/v1/models` |
| KoboldCpp | `http://127.0.0.1:5001/v1` | `/v1/models` |
| text-generation-webui | `http://127.0.0.1:5000/v1` | `/v1/models` |

Пресет определяется по порту автоматически; в панели настроек его можно выбрать
вручную. Если сервер живёт на нестандартном порту, `local_providers.py` всё равно
попробует нативные эндпоинты (`/api/tags`, `/api/v0/models`) и возьмёт более
богатый каталог.

### Что именно даёт паритет

| Возможность Groq/OpenRouter | Как реализовано локально |
|---|---|
| Потоковые ответы | Один парсер (`local_providers.iter_stream`) понимает SSE `data: {...}`, NDJSON, чанки Ollama (`message.content` / `message.thinking`) и llama.cpp (`response`). |
| Нативный tool calling | `tools` + `tool_choice` уходят в модель; возможности берутся из `/api/show` (Ollama) или `/api/v0/models` (LM Studio). Если сервер вернул 400 про `tools`, запрос **один раз** повторяется без них — чат не падает. |
| JSON-режим / структурированный вывод | `chat_completion(..., json_mode=True)` → `response_format: {"type":"json_object"}`; `json_schema={...}` → `json_schema`. |
| Ход рассуждений | `reasoning_content` / `reasoning` / `thinking` стримятся в интерфейс; для моделей без нативного reasoning работает английский pre-pass «план». |
| Vision | Части `image_url` передаются как есть; каталог помечает модели с поддержкой изображений. |
| Каталог моделей с контекстом | Контекст, семейство, размер, квантование, состояние «загружена». |
| Учёт токенов | `usage` из ответа + `stream_options.include_usage` в потоке. |
| Классификация ошибок и повторы | Общий `provider_manager`: 401/403/404/429/5xx/timeout, Retry-After, backoff. |
| Параметры сэмплирования | `top_p`, `seed`, `stop`, `presence_penalty`, `frequency_penalty` — через `.env` (`LOCAL_LLM_*`), необязательные. |
| Автопоиск сервера | `LOCAL_LLM_AUTODETECT=1`: если облачных ключей нет, чат сам берёт локальный сервер. |

Локальные модели бесплатны: `is_free_ai_model` возвращает `True` для
`openai_compatible`, поэтому фильтр «только бесплатные» их не скрывает.

### Быстрый старт

```bash
# Ollama
ollama pull qwen3:8b
ollama serve

# в .env (или в панели «Локальные модели»)
OPENAI_BASE_URL=http://127.0.0.1:11434/v1
LOCAL_LLM_AUTODETECT=1
```

Откройте `/settings` → **Локальные модели**: там выбор пресета, адрес, проверка
сервера, список моделей и их реальные возможности (`tools`, `vision`,
`reasoning`, `JSON`). Кнопка «Сохранить и применить» пишет настройки в
`runtime_settings.json` и сразу меняет адрес провайдера `openai_compatible`.

### API

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/api/settings/local` | Пресеты, адрес, каталог моделей, матрица функций |
| `POST` | `/api/settings/local` | Сохранить пресет/адрес/ключ и применить |
| `POST` | `/api/settings/local/test` | Проверить адрес до сохранения |

Эндпоинты админские, как и вся панель настроек; API-ключ никогда не отдаётся
в ответе (только `has_key: true`).

### Настройки `.env`

Полный список — в `.env.example` (раздел «Локальные модели»). Главные:

```bash
LOCAL_LLM_URL=http://127.0.0.1:11434/v1
LOCAL_LLM_TOOLS=auto            # auto | on | off
LOCAL_LLM_REASONING=auto        # auto | on | off
LOCAL_LLM_TOP_P=0.9
LOCAL_LLM_SEED=42
LOCAL_LLM_STOP=</s>||<|im_end|> # разделитель — ||
LOCAL_LLM_DEEP_SCAN=1           # точные возможности из Ollama /api/show
LOCAL_LLM_STREAM_USAGE=1
```

---

## 2. Язык ответов

### Правило

* **Промпты — только английские.** Системные промпты, правила ReAct, поисковые
  инструкции, наблюдения инструментов и pre-pass рассуждений написаны на
  английском и не содержат «отвечай по-русски».
* **Ответ — на языке интерфейса.** `language_policy.py` дописывает к системному
  промпту короткую английскую директиву:

  ```
  [NOVA_ANSWER_LANGUAGE ru] ANSWER LANGUAGE (required): Russian (Русский) — language code "ru".
  Write every user-visible part of your reply in Russian: prose, plans, headings,
  lists, tables, summaries of tool results, warnings, error notes and follow-up questions.
  Keep exactly as-is: source code, file paths, shell commands, URLs, API, tool and
  parameter names, identifiers and data quoted from tools or web pages.
  If the user explicitly asks for another language for a specific answer, follow
  that request for that answer only, then return to Russian.
  ```

  Код, пути, команды, URL, имена инструментов и цитаты из источников остаются
  как есть — переводится только текст для пользователя.

Директива добавляется в одном месте — `ai_providers._chat_completion_once` и
`_chat_stream_once`, поэтому работает для **всех** провайдеров: Groq, Cerebras,
OpenRouter, Google AI Studio, Pollinations, локальных серверов и офлайн-помощника.

### Откуда берётся язык

Порядок (`language_policy.current_language()`):

1. поле `ui_language` / `language` / `lang` в JSON-запросе (для интеграций);
2. заголовок `X-UI-Language`;
3. язык в сессии посетителя (ставится при переключении в интерфейсе);
4. `config.ui_language` (серверный по умолчанию, `tg`);
5. `DEFAULT_LANGUAGE`.

Поэтому на одном сервере разные пользователи могут получать ответы на разных
языках, а переключатель в интерфейсе не «переключает всех».

### Поддерживаемые языки

`language_policy.LANGUAGES` содержит ~75 языков (включая `tg`, `ru`, `en`, `uz`,
`kk`, `fa`, `ar`, `tr`, `de`, `fr`, `es`, `zh`, `ja`, `hi` и другие), а также
алиасы (`ru-RU`, `en_US`, `tj`, `zh-Hans`). Полный перевод интерфейса есть для
`tg`, `ru`, `en` (`language_policy.UI_LANGUAGES` + `static/i18n.js`).

Добавить новый язык ответа:

```python
# language_policy.py
LANGUAGES["fr"] = ("French", "Français")   # уже есть; пример для нового кода
```

Добавить полный перевод интерфейса: добавить код в `UI_LANGUAGES` и строки в
`static/i18n.js`. Если перевода интерфейса нет, ответы всё равно пойдут на
выбранном языке — достаточно записи в `LANGUAGES` и установки `ui_language`.

### Офлайн-помощник

`local_demo` не является языковой моделью, но и он отвечает на языке интерфейса:
`offline_i18n.py` хранит тексты на `en` / `ru` / `tg` (база — английский,
неизвестные языки получают английский). Вопросы при этом понимаются на любом из
трёх языков: «привет» и «hello» дают один и тот же ответ в выбранном языке.

### Проверки

```bash
pytest -q tests/test_language_policy.py tests/test_local_providers.py
```

Тесты проверяют, что промпты не содержат русских инструкций, что директива не
дублируется и переключается, что локальный каталог читает возможности сервера,
что поток разбирается во всех диалектах и что `tools`/`response_format`
отбрасываются только при реальном отказе сервера.
