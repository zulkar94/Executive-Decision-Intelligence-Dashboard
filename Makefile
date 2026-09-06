.DEFAULT_GOAL := help
PY ?= python3
BACKEND := backend

.PHONY: help install dev test cov lint fmt type security audit smoke run docker-build docker-up docker-down secret clean

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime and development dependencies
	cd $(BACKEND) && $(PY) -m pip install -r requirements-dev.txt

run: ## Start the API and dashboard on http://localhost:8000
	cd $(BACKEND) && $(PY) -m uvicorn app.main:app --reload --port 8000

test: ## Run the test suite
	cd $(BACKEND) && $(PY) -m pytest

cov: ## Run tests with a coverage report
	cd $(BACKEND) && $(PY) -m pytest --cov=app --cov-report=term-missing --cov-report=xml

lint: ## Lint and check formatting
	cd $(BACKEND) && $(PY) -m ruff check . && $(PY) -m ruff format --check .

fmt: ## Apply formatting and safe lint fixes
	cd $(BACKEND) && $(PY) -m ruff check --fix . && $(PY) -m ruff format .

type: ## Static type check
	cd $(BACKEND) && $(PY) -m mypy app

security: ## Static security scan
	cd $(BACKEND) && $(PY) -m bandit -c pyproject.toml -r app

audit: ## Check dependencies for known vulnerabilities
	cd $(BACKEND) && $(PY) -m pip_audit -r requirements.txt

smoke: ## End-to-end check against an in-process server
	cd $(BACKEND) && $(PY) ../scripts/smoke.py

docker-build: ## Build the container image
	docker compose build

docker-up: ## Start the full stack
	docker compose up -d

docker-down: ## Stop the stack
	docker compose down

secret: ## Print a fresh EDID_SECRET_KEY
	@$(PY) -c "import secrets; print(secrets.token_urlsafe(48))"

clean: ## Remove caches and local databases
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf backend/.pytest_cache backend/.ruff_cache backend/.mypy_cache backend/.coverage backend/edid.db
