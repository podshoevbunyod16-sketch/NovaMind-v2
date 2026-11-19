# AI_QUALITY.md — качество моделей и оценки

NovaMind-v2 — платформа вокруг подключаемых моделей, а не собственная foundation model. Мы не заявляем паритет с Claude/GPT/Gemini; качество ответа определяется выбранной моделью, качеством источников и агентной обвязкой.

## Что обеспечивает качество в этом репозитории

1. **Разделение ролей и данных**: system/developer/user/tool/MCP разделены; web и документы обёрнуты в `<untrusted>` (`agent/policy.wrap_untrusted`), что снижает indirect prompt injection.
2. **Guardrails**: `guardrails.py` проверяет ввод/вывод, вырезает ключи и не даёт модели повторить системный промпт. Тесты: `tests/test_guardrails.py`, `tests/test_reasoning_leak.py`.
3. **Прозрачность вместо covert CoT**: пользователю показываются инструменты, источники, проверяемые шаги и ограничения; внутренняя цепочка рассуждений не выдаётся как доказательство. См. `tests/test_reasoning_leak.py`.
4. **Провайдеры**: unified interface Groq/Gemini/OpenAI-compatible, ключи с ротацией, backoff, circuit breaker, registry возможностей и честная деградация до `local_demo`.
5. **Оценки**: `evals/` содержит 55 YAML-задач (включая `arena_signals`), запуск — `python evals/run.py`, гейт — `python -m evals.gate`. Категории safety и guardrails имеют порог 100%.

## Покрытие оценок

| Категория | Файл | Что проверяем |
|---|---|---|
| factuality / citations | `evals/tasks/factuality_citations.yaml` | ссылки на авторитетные источники, отказ выдумывать |
| tool use / coding | `evals/tasks/code.yaml`, `terminal.yaml` | реальный вызов инструмента и проверяемый результат |
| web search | `evals/tasks/search.yaml` | источники внутри ответа |
| safety / prompt injection | `evals/tasks/safety.yaml`, `guardrails.yaml` | запрещённые команды, утечка промпта, инъекции |
| multilingual RU/EN | `evals/tasks/multilingual.yaml` | язык ответа соответствует вопросу |
| long context | `evals/tasks/long_context.yaml` | удержание фактов и невыполнение инструкций внутри текста |
| provider fallback | `evals/tasks/provider_fallback.yaml` | честное сообщение об ошибке провайдера |
| regression | `evals/gate.py`, `evals/thresholds.json` | падение успеха/шагов/токенов/времени |

## Честные ограничения

- Офлайн-режим `local_demo` узкий и не вызывает инструменты: `evals.gate` помечает такой прогон `skipped` (exit 2) без `--judge-offline`.
- Оценки детерминированы по ожидаемым подстрокам и шагам; они не заменяют human review и не измеряют тонкую фактическую точность.
- Стоимость и токены считаются оценочно (`usage_stats.py`, `provider_policy.py`); тарификация провайдера меняется — проверяйте актуальные цены.
- Для реального повышения качества нужны живые ключи провайдеров, отдельный набор human-labeled eval, A/B-прогоны и мониторинг качества в проде.

## Как запускать

```bash
AGENT_RATE_LIMIT=0 python app.py
python evals/run.py --json /tmp/report.json
python -m evals.gate --report /tmp/report.json
```
