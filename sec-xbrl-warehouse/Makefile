SHELL := /bin/bash
PYTHON ?= python3.12
VENV ?= .venv
BIN := $(VENV)/bin
COMPOSE ?= $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")

.PHONY: help install up monitoring down logs migrate init-clickhouse doctor test integration lint format typecheck scan-stubs api worker clean

help:
	@echo "Targets:"
	@echo "  install          Create venv and install runtime + dev dependencies"
	@echo "  up               Start PostgreSQL, ClickHouse and MinIO"
	@echo "  monitoring       Start the application, Prometheus and Grafana"
	@echo "  down             Stop local infrastructure"
	@echo "  migrate          Apply PostgreSQL Alembic migrations"
	@echo "  init-clickhouse  Apply ClickHouse DDL"
	@echo "  doctor           Verify configuration and service connectivity"
	@echo "  test             Run unit tests"
	@echo "  integration      Run integration tests"
	@echo "  lint             Run Ruff"
	@echo "  format           Format with Ruff"
	@echo "  typecheck        Run mypy"
	@echo "  scan-stubs       List remaining IMPLEMENT_ME markers"

install:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e ".[dev]"

up:
	$(COMPOSE) up -d postgres clickhouse minio minio-init

monitoring:
	$(COMPOSE) --profile application --profile monitoring up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

migrate:
	$(BIN)/alembic upgrade head

init-clickhouse:
	$(BIN)/xbrl-warehouse db init-clickhouse

doctor:
	$(BIN)/xbrl-warehouse doctor

test:
	$(BIN)/pytest -m "not integration"

integration:
	RUN_XBRL_INTEGRATION=1 $(BIN)/pytest -m integration

lint:
	$(BIN)/ruff check .

format:
	$(BIN)/ruff format .

format-check:
	$(BIN)/ruff format --check .

typecheck:
	$(BIN)/mypy src

scan-stubs:
	$(BIN)/python scripts/scan_stubs.py

api:
	$(BIN)/xbrl-warehouse api serve

worker:
	$(BIN)/xbrl-warehouse worker run

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage build dist *.egg-info
