# Thin wrappers around the commands in README and the spec. Every target is also runnable by hand.
# Targets marked (Mn) need files that milestone creates.
SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE ?= docker compose

.PHONY: help setup sync lint fmt typecheck test cov check contract-lint keys mocks pin-hostkey migrate up down logs schemathesis audit reset

help:  ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'

setup:  ## Idempotent VM setup (uv, Python 3.14, k6, dockerd)
	bash scripts/cloud-setup.sh

sync:  ## Install locked dependencies into .venv
	uv sync --locked

lint:  ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

fmt:  ## Apply ruff fixes and formatting
	uv run ruff check --fix .
	uv run ruff format .

typecheck:  ## mypy --strict on src/
	uv run mypy src

test:  ## Unit + golden tests
	uv run pytest

cov:  ## Tests with branch coverage on the section 7 targets (M9 gate: >= 90%)
	uv run pytest --cov=gateway.ingest --cov=gateway.resilience --cov=gateway.webhooks --cov-branch --cov-report=term-missing

check: lint typecheck test  ## Everything a contributor runs before pushing

contract-lint:  ## (M1) Lint the OpenAPI 3.1 contract
	npx --yes @redocly/cli lint contracts/openapi.yaml

keys:  ## (M2) Generate the gateway SFTP key into secrets/ (skips if present)
	@mkdir -p secrets && chmod 700 secrets
	@test -f secrets/gateway_ed25519 || ssh-keygen -t ed25519 -N "" -C gateway@meridian-lab -f secrets/gateway_ed25519
	cp secrets/gateway_ed25519.pub mocks/sftp/gateway_ed25519.pub

mocks:  ## (M2) Start Postgres and the legacy stand-ins
	$(COMPOSE) up -d --build postgres sftp soap-mock webhook-sink

pin-hostkey:  ## (M2) Pin the SFTP host key under the name the workers use ("sftp")
	$(COMPOSE) exec -T sftp sh -c 'echo "sftp $$(cut -d" " -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"' > secrets/known_hosts

migrate:  ## (M8) Apply additive migrations
	$(COMPOSE) run --rm gateway-api python -m gateway.migrate

up:  ## (M8) Start the API and both workers
	$(COMPOSE) up -d gateway-api sftp-poller webhook-dispatcher

down:  ## Stop this project's containers (keeps volumes)
	$(COMPOSE) down --remove-orphans

logs:  ## Follow logs (S=<service>)
	$(COMPOSE) logs -f --tail=100 $(S)

schemathesis:  ## (M8) Conformance tests against the running API
	uvx schemathesis==4.28.0 run contracts/openapi.yaml --url http://localhost:8000 -H "X-API-Key: dev-shipper-key" --checks all

audit:  ## Known-vulnerability scan of the locked dependencies
	uv export --frozen --no-dev --no-hashes -o /tmp/requirements-audit.txt
	uvx pip-audit==2.10.1 -r /tmp/requirements-audit.txt

reset:  ## DESTRUCTIVE: remove this project's containers AND volumes (asks first)
	@read -p "Delete this project's volumes (Postgres data, SFTP host keys)? [y/N] " a && [ "$$a" = y ]
	$(COMPOSE) down -v --remove-orphans
