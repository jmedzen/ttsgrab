# ttsgrab — make targets
#
# The 80% path is: `make up`, open http://localhost:8000, type, listen.
# Everything else here is for development or for a GPU box.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
PROFILE ?= core
PYTHON  ?= python3
VENV    ?= .venv
UV      ?= uv

# Keep the uv cache inside the repo so it never collides with a system-owned cache dir.
export UV_CACHE_DIR ?= $(CURDIR)/.uv-cache
export npm_config_cache ?= $(CURDIR)/.npm-cache

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# -- docker -----------------------------------------------------------------------------

.PHONY: up
up: .env ## Start the gateway (profile: core). Opens on http://localhost:8000
	$(COMPOSE) --profile $(PROFILE) up -d --build
	@echo ""
	@echo "  ttsgrab is starting → http://localhost:$(or $(TTSGRAB_PORT),8000)"
	@echo "  logs: make logs"

.PHONY: up-gpu
up-gpu: .env ## Start gateway + GPU workers
	$(COMPOSE) --profile gpu up -d --build

.PHONY: up-cpu
up-cpu: .env ## Start gateway + CPU Kokoro worker
	$(COMPOSE) --profile cpu-local up -d --build

.PHONY: down
down: ## Stop everything (keeps volumes)
	$(COMPOSE) --profile core --profile gpu --profile cpu-local --profile tls down

.PHONY: down-hard
down-hard: ## Stop everything AND delete cached audio, models and history
	$(COMPOSE) --profile core --profile gpu --profile cpu-local --profile tls down -v

.PHONY: logs
logs: ## Follow gateway logs
	$(COMPOSE) logs -f gateway

.PHONY: logs-worker
logs-worker: ## Follow worker logs
	$(COMPOSE) logs -f kokoro cosyvoice

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) ps

.PHONY: build
build: ## Build all images for linux/amd64
	docker buildx build --platform linux/amd64 \
	  -f deploy/Dockerfile.gateway -t ttsgrab/gateway:0.1.0 .
	docker buildx build --platform linux/amd64 \
	  -f deploy/Dockerfile.worker --build-arg WORKER=kokoro -t ttsgrab/worker-kokoro:0.1.0 .

.PHONY: build-arm64
build-arm64: ## Build all images for linux/arm64 (Apple Silicon, ARM NAS)
	docker buildx build --platform linux/arm64 \
	  -f deploy/Dockerfile.gateway -t ttsgrab/gateway:0.1.0-arm64 .

# -- local development (no Docker) ------------------------------------------------------

.PHONY: venv
venv: ## Create the virtualenv and install dev dependencies
	$(UV) venv --python 3.11 $(VENV)
	$(UV) pip install -e ".[dev,mcp,gcp]"

.PHONY: dev
dev: ## Run the gateway with autoreload on :8000
	$(VENV)/bin/python -m ttsgrab --reload --port 8000

.PHONY: web-dev
web-dev: ## Run the Vite dev server on :5173 (proxies /v1 to :8000)
	cd web && npm run dev

.PHONY: web
web: ## Build the SPA into the Python package
	cd web && npm run build

.PHONY: test
test: ## Run the Python test suite
	$(VENV)/bin/python -m pytest -q

.PHONY: test-e2e
test-e2e: ## Run browser tests (needs a server on :8011)
	cd web && npx playwright test

.PHONY: lint
lint: ## Ruff + mypy
	$(VENV)/bin/ruff check src tests workers
	$(VENV)/bin/mypy src/ttsgrab

.PHONY: fmt
fmt: ## Format with ruff
	$(VENV)/bin/ruff format src tests workers
	$(VENV)/bin/ruff check --fix src tests workers

.PHONY: typecheck-web
typecheck-web: ## TypeScript check
	cd web && npx tsc --noEmit

# -- housekeeping -----------------------------------------------------------------------

.PHONY: env
.env: ## Create .env from the example, with a generated admin token
	@if [ -f .env ]; then echo ".env already exists — leaving it alone"; else \
	  cp .env.example .env && \
	  token=$$(openssl rand -hex 24) && \
	  $(PYTHON) -c "import pathlib,sys; p=pathlib.Path('.env'); \
	    p.write_text(p.read_text().replace('TTSGRAB_ADMIN_TOKEN=', 'TTSGRAB_ADMIN_TOKEN=' + sys.argv[1]))" $$token && \
	  echo "Created .env with a generated admin token: $$token"; \
	fi

.PHONY: token
token: ## Print the admin token from .env
	@grep '^TTSGRAB_ADMIN_TOKEN=' .env | cut -d= -f2-

.PHONY: clean
clean: ## Remove build artefacts and caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
	rm -rf web/dist web/test-results web/playwright-report
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

.PHONY: smoke
smoke: ## Hit the running gateway and check the acceptance path
	@bash scripts/smoke.sh

.PHONY: migrate
migrate: ## Apply database migrations (use before upgrading an existing install)
	$(VENV)/bin/python -m alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration from model changes: make migration m="add foo"
	@test -n "$(m)" || { echo 'usage: make migration m="what changed"'; exit 1; }
	$(VENV)/bin/python -m alembic revision --autogenerate -m "$(m)"
	@echo "Review the generated file in alembic/versions/ before committing it."
