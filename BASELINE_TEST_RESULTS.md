# Baseline до исправлений

## Сводка

Baseline снят на ветке `agent/production-hardening`, исходный commit `868df79` от `origin/main`, до правок приложения. В репозитории на тот момент было только добавлено требуемое предварительное аудиторское заключение. Полный pytest повторён в отдельном `HOME`, без API credentials, с блокировкой всех внешних DNS/socket соединений; разрешался только loopback.

| Проверка | Исходный результат |
|---|---|
| Python syntax (`compileall`) | Успешно |
| `pytest -q --cov=.` | **1 089 passed** за 60.31 сек. |
| Coverage | **81%**: 24 718 statements, 4 578 пропущено. Важно: команда включает тестовые модули, поэтому показатель не является production-only coverage. |
| Frontend smoke (`tests/frontend/smoke.mjs`) | Успешно на локальном Flask `127.0.0.1:5000` с jsdom |
| Ruff | **566 ошибок** по текущим default rules, 336 отмечены autofixable |
| Pyright | **483 ошибки**, 0 warnings |
| Bandit | **11 medium**, 2 778 low, **0 high**; анализ включал тестовые файлы |
| `pip-audit -r requirements.txt` | Известных уязвимостей не найдено |
| `npm audit` | 0 уязвимостей по доступной npm metadata |
| Gitleaks 8.30.1, Git history (`--all`) | **0 совпадений** |
| Gitleaks, исходное дерево | 2 pattern matches только в тестовых файлах: `tests/test_agent_confirm.py:87` и `tests/test_memory.py:62`; значения не выводились. Они выглядят как тестовые примеры, но будут заменены на составные фиктивные строки для устранения неоднозначных срабатываний. |
| Docker | CLI отсутствует; Docker build проверить невозможно |
| Mypy / Black / Flake8 | Отсутствуют; отдельная проверка этими инструментами не проводилась |

## Условия и ограничения baseline

Изначально в окружении не было pytest/coverage, Ruff, Bandit, pip-audit и jsdom. Для baseline зависимости установлены локально в игнорируемый `.venv` и `node_modules`; файлы `requirements*.txt`/`package.json` этим не менялись. `pyright`, Python, Node/npm и GitHub CLI уже были доступны. Позже в изменениях проекта будет добавлена воспроизводимая конфигурация требуемых dev/CI проверок.

Frontend smoke требует запущенный сервер. Проверка проведена на loopback; тест прошёл, сервер остановлен.

## Безопасность тестового прогона

Первоначальная попытка полного pytest выполнялась до обнаружения дефекта в тестовом bootstrap: `tests/conftest.py` импортирует `config` до удаления environment keys, а `config.PROVIDERS` фиксирует заголовки на этапе импорта. Этот прогон был прерван на тесте списка media models во время TLS-handshake после 540 успешных тестов; он **не включён** в baseline и не трактуется как завершённая проверка. HTTP-ответ/результат теста в этой попытке не был получен.

Полный итоговый baseline повторён с `env -i`, отдельным `HOME` и временным `sitecustomize`, который запрещал внешний DNS/socket-доступ; proxy fallback также направлялся на недоступный loopback. Из этого запуска не мог уйти запрос к AI-провайдеру или другому внешнему endpoint. Результат: все 1 089 тестов пройдены, coverage 81%.

## Артефакты тестов

Набор `tests/test_turtle_headless.py` изменил отслеживаемый файл `examples/turtle_spin/spin.gif`; до тестов ветка была чистой. GIF возвращён ровно к версии `HEAD`. Сгенерированный `.coverage`, отсутствовавший до запуска, удалён; дальнейшие покрытия будут записываться в `/tmp`, а сам тест будет исправлен так, чтобы использовать `tmp_path`.

## Результаты security/dependency scan

- Bandit baseline не содержит high findings, но 11 medium и значительное число low findings требуют последующей triage; существующие правила не подавлялись ради прохода CI.
- pip-audit/npm audit не нашли известные уязвимости для текущих manifest/metadata. Это не заменяет pinned lockfiles и анализ runtime/deployment image.
- Gitleaks не нашёл совпадений в Git history по доступным refs. Два текущих срабатывания находятся в тестовых сценариях детектирования; потенциальные значения в отчёт не включались.
- GitHub Actions, Docker image, production secrets, production database и production deployment не изменялись и в этом локальном baseline не запускались.
