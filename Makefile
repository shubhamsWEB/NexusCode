.PHONY: help install install-dev dev worker dashboard test test-cov lint format typecheck check clean docker-up docker-down docker-logs db-init db-index

# ── Config ────────────────────────────────────────────────────────────────────
PYTHON     := .venv/bin/python
PIP        := .venv/bin/pip
PYTEST     := .venv/bin/pytest
RUFF       := .venv/bin/ruff
MYPY       := .venv/bin/mypy
UVICORN    := .venv/bin/uvicorn
STREAMLIT  := .venv/bin/streamlit
RQ         := .venv/bin/rq

PYTHONPATH := .
OBJC_FLAG  := OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Setup ─────────────────────────────────────────────────────────────────────

install:  ## Create venv and install runtime dependencies
	python -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

install-dev: install  ## Install runtime + dev dependencies + pre-commit hooks
	$(PIP) install -r requirements-dev.txt
	.venv/bin/pre-commit install
	@echo "✅ Dev environment ready. Copy .env.example to .env and fill in your keys."

# ── Runtime ───────────────────────────────────────────────────────────────────

dev:  ## Start API server with hot-reload (development)
	PYTHONPATH=$(PYTHONPATH) $(UVICORN) src.api.app:app --reload --port 8000

server:  ## Start API server (production mode)
	PYTHONPATH=$(PYTHONPATH) $(OBJC_FLAG) $(UVICORN) src.api.app:app --port 8000

worker:  ## Start RQ indexing worker
	PYTHONPATH=$(PYTHONPATH) $(OBJC_FLAG) $(RQ) worker indexing --url redis://localhost:6379

dashboard:  ## Start Streamlit admin dashboard
	PYTHONPATH=$(PYTHONPATH) API_URL=http://localhost:8000 $(STREAMLIT) run src/ui/dashboard.py --server.port 8501

# ── Database ──────────────────────────────────────────────────────────────────

db-init:  ## Initialise database schema and extensions
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/init_db.py

db-index:  ## Full index of a repository (usage: make db-index REPO=owner/name)
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/full_index.py $(REPO)

# ── Testing ───────────────────────────────────────────────────────────────────

test:  ## Run all tests
	PYTHONPATH=$(PYTHONPATH) $(PYTEST) tests/ -v

test-cov:  ## Run tests with coverage report
	PYTHONPATH=$(PYTHONPATH) $(PYTEST) tests/ -v \
		--cov=src --cov-report=term-missing --cov-report=html

test-fast:  ## Run tests excluding slow/integration markers
	PYTHONPATH=$(PYTHONPATH) $(PYTEST) tests/ -v -m "not slow and not integration"

# ── Code quality ──────────────────────────────────────────────────────────────

lint:  ## Run ruff linter
	$(RUFF) check src/ tests/ scripts/

lint-fix:  ## Run ruff linter and auto-fix issues
	$(RUFF) check --fix src/ tests/ scripts/

format:  ## Format code with ruff
	$(RUFF) format src/ tests/ scripts/

format-check:  ## Check formatting without modifying files
	$(RUFF) format --check src/ tests/ scripts/

typecheck:  ## Run mypy type checker
	$(MYPY) src/ --ignore-missing-imports

check: lint format-check typecheck test  ## Run all quality checks (CI equivalent)

# ── Docker ────────────────────────────────────────────────────────────────────

docker-up:  ## Start full stack with Docker Compose
	docker compose up -d
	@echo "✅ Stack running. API: http://localhost:8000  Dashboard: http://localhost:8501"

docker-down:  ## Stop Docker Compose stack
	docker compose down

docker-infra:  ## Start only infrastructure (postgres + redis)
	docker compose up postgres redis -d

docker-logs:  ## Tail logs from all services
	docker compose logs -f

docker-build:  ## Rebuild Docker images
	docker compose build --no-cache

# ── Utilities ─────────────────────────────────────────────────────────────────

deploy-check:  ## Run pre-deploy environment verification
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/deploy_check.py

simulate-webhook:  ## Simulate a GitHub push webhook (usage: make simulate-webhook FILE=path/to/file.py)
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/simulate_webhook.py $(if $(FILE),--file $(FILE),)

clean:  ## Remove build artifacts, caches, and coverage reports
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true
	find . -name ".coverage" -delete 2>/dev/null; true
	@echo "✅ Cleaned."
