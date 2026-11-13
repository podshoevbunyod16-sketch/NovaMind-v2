# ══════════════════════════════════════════════════════════════════
# NovaMind · быстрые команды разработки и эксплуатации
#   make help — список
# ══════════════════════════════════════════════════════════════════
PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
PORT ?= 5000

.PHONY: help install dev run test test-fast compile preflight prod \
        receipts verify-council docker-build docker-run compose-up backup clean

.DEFAULT_GOAL := help

help:            ## Показать команды
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:         ## Создать venv и поставить зависимости (+dev)
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -U pip
	$(BIN)/pip install -r requirements-dev.txt

dev:             ## Запустить dev-сервер с автоперезагрузкой
	$(BIN)/python app.py --dev

serve:           ## Production-сервер: gunicorn поднимается из app.py
	$(BIN)/python app.py

check:           ## Проверить окружение и показать баннер запуска
	$(BIN)/python app.py --check

run:             ## То же через gunicorn напрямую (конфиг в gunicorn.conf.py)
	$(BIN)/gunicorn -c gunicorn.conf.py wsgi:application

deploy:          ## Деплой: venv, зависимости, бэкап, рестарт, health
	$(BIN)/python scripts/deploy.py

service:         ## Отрендерить systemd-unit под текущий путь
	$(BIN)/python scripts/install_service.py

test:            ## Все тесты
	$(BIN)/python -m pytest -q

test-fast:       ## Только фирменные подсистемы
	$(BIN)/python -m pytest -q tests/test_receipts.py tests/test_council.py \
		tests/test_production.py tests/test_trust_center.py

compile:         ## Проверить синтаксис всех модулей
	$(BIN)/python -m compileall -q -x '(\.venv|__pycache__)' .

preflight:       ## Проверка окружения перед деплоем
	$(BIN)/python scripts/preflight.py

prod:            ## production-режим локально (строгие проверки)
	NOVA_ENV=production $(BIN)/python app.py

receipts:        ## Проверить цепочку квитанций
	$(BIN)/python -m receipts verify

backup:          ## Резервная копия данных (хранит 7 последних)
	$(BIN)/python scripts/backup.py --keep 7

# ── Docker — опционально, если он вообще нужен ──────────────────
docker-build:    ## Собрать production-образ (опционально)
	docker build -t novamind:v3 .

docker-run:      ## Запустить образ с данными на томе
	docker run --rm -p $(PORT):5000 --env-file .env.production \
		-v novamind-data:/data novamind:v3

compose-up:      ## Поднять compose-стек
	docker compose up -d --build

clean:           ## Убрать кэши (не трогает данные)
	rm -rf .pytest_cache __pycache__ */__pycache__ */*/__pycache__
