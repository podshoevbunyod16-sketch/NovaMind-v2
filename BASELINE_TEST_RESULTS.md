# BASELINE_TEST_RESULTS.md — состояние до изменений

Дата: 2026-11-19. Ветка: `arena/b009de6e-novamind-v2`, commit `868df79`.
Окружение: Python 3.11.2, venv `.venv`, Node 22 (для smoke), Linux x86_64. Зависимости установлены из `requirements-dev.txt`; инструменты проверки установлены дополнительно (`pytest-cov`, `ruff`, `mypy`, `bandit`, `pip-audit`).

| Проверка | Команда | Результат | Детали |
|---|---|---|---|
| Syntax check | `.venv/bin/python -m compileall -q -x '(\.venv|__pycache__|node_modules)' .` | ✅ PASS | exit 0 |
| Unit/integration tests | `.venv/bin/python -m pytest -q tests` | ✅ PASS | **1089 passed in 58.38s** |
| Coverage | `.venv/bin/python -m pytest -q tests --cov=. --cov-report=term-missing:skip-covered` | ✅ PASS (test run) | 1089 passed, **TOTAL 81%** (24718 statements, 4573 missing). Coverage considers tests themselves; production-only coverage below. 69 files fully covered. |
| Frontend smoke | `node tests/frontend/smoke.mjs http://127.0.0.1:5000` | ✅ PASS | 196 positive checks, «Фронтенд-смоук пройден полностью», exit 0. Server started with `SCHEDULER_ENABLED=0 TERMINAL_ENABLED=1 .venv/bin/python app.py`. |
| Ruff lint | `.venv/bin/ruff check .` | ❌ FAIL | **566 errors**, 336 autofixable (50 unsafe). Main classes: style/import order/regex flags. Не исправлялось молча. |
| MyPy type-check | `.venv/bin/mypy --ignore-missing-imports --no-error-summary .` | ❌ FAIL | `Duplicate module named "app" (also at "./examples/flask_shop/app.py")` — конфигурация отсутствует. |
| Bandit (all files recursively) | `.venv/bin/bandit -q -r . -x .venv,node_modules,tests` | ⚠️ TIMEOUT/неприменимо | Рекурсивный обход ушёл в `.venv`; команда остановлена по таймауту 180 s. Перезапуск на first-party файлах: `.venv/bin/bandit -q <список *.py>`. |
| Bandit (first-party) | `find ... -name '*.py' (без .venv/tests/examples) \| xargs bandit -q` | ❌ FAIL | **75 issues**: 65 low, 10 medium, 0 high. Типы: B110 (26), B603 (10), B404 (8), B607 (7), B608 (4), B113 (4), B405/B314 (2), B311 (1), прочие. |
| pip-audit | `.venv/bin/pip-audit -r requirements.txt` | ❌ FAIL | 3 known vulnerabilities in 1 package: `soupsieve 2.10` → CVE-2026-126190/126191/126193, fix 3.0.2/4.0.0. |
| Secret scan (repo, history) | visual inspection + `git grep` | ⚠️ PARTIAL | В `.env.example` только заглушки; найден закоммиченный пользовательский скриншот `routes/uploads/*.jpg`. Проверка истории без переписывания history; автоматический сканер добавляется в этой ветке. |
| Docker build | `docker` CLI | ⚠️ SKIPPED | Docker в sandbox недоступен (`which docker` пусто). Dockerfile будет добавлен, сборка останется непроверенной локально. |

## Что НЕ делалось до аудита

- Ни один исходный файл не изменялся до публикации `AUDIT_BEFORE_CHANGES.md` и этого файла.
- Никакие тесты не отключались, пороги не снижались.
- Секреты и значения `.env` не выводились; сгенерированный при старте одноразовый ADMIN_CODE не сохранялся и не публиковался.

## Вывод

Функциональный baseline зелёный (1089 тестов + frontend smoke), качество инструментов — нет: lint/type/security/dependency проверки красные или неприменимые. Это __baseline__, а не «всё работает»: исправления и повторные прогоны — в `FINAL_AUDIT.md`.
