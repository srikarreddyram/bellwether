# Bellwether — development tasks.
.DEFAULT_GOAL := help
.PHONY: help install test test-fast lint fmt typecheck check dev api proxy dashboard clean

PY ?= python3
# Use whichever virtualenv is already present, preferring the modern name.
VENV ?= $(if $(wildcard .venv),.venv,$(if $(wildcard venv),venv,.venv))
BIN := $(VENV)/bin

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

$(BIN)/python:
	$(PY) -m venv $(VENV)

install: $(BIN)/python ## Editable install with dev extras, plus dashboard deps
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev,tracking]"
	cd frontend && npm install

test: ## Full suite, including real end-to-end rollouts
	$(BIN)/pytest

test-fast: ## Skip the tests that spawn processes and bind sockets
	$(BIN)/pytest -m "not integration"

lint: ## ruff + mypy (strict) + eslint
	$(BIN)/ruff check src tests
	$(BIN)/ruff format --check src tests
	$(BIN)/mypy
	cd frontend && npm run lint

fmt: ## Apply ruff's autofixes and formatting
	$(BIN)/ruff check --fix src tests
	$(BIN)/ruff format src tests

typecheck: ## mypy only
	$(BIN)/mypy

check: lint test ## Everything CI runs

api: ## Run the control plane
	$(BIN)/bellwether api

proxy: ## Run the data plane on its own
	$(BIN)/bellwether proxy

dashboard: ## Run the dashboard dev server
	cd frontend && npm run dev

dev: ## API and dashboard together
	@$(MAKE) -j2 api dashboard

clean: ## Remove build artefacts and caches
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf frontend/dist frontend/.vite
