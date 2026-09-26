# Thin wrappers around the commands in README and the spec. Every target is also runnable by hand.
# Targets marked (Mn) need files that milestone creates.
SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE ?= docker compose

.PHONY: help setup sync lint fmt typecheck test cov check contract-lint keys base-images mocks pin-hostkey migrate up down logs schemathesis audit reset drop

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
	npx --yes @redocly/cli@2.54.3 lint contracts/openapi.yaml

keys:  ## (M2) Generate the gateway SFTP key into secrets/ (skips if present)
	@mkdir -p secrets && chmod 700 secrets
	@test -f secrets/gateway_ed25519 || ssh-keygen -t ed25519 -N "" -C gateway@meridian-lab -f secrets/gateway_ed25519
	cp secrets/gateway_ed25519.pub mocks/sftp/gateway_ed25519.pub

# Docker Hub allows 100 anonymous pulls/h per egress IP, and the cloud VM shares its IP, so we hit 429.
# mirror.gcr.io is Google's read-through cache of Docker Hub: same image digests, no Hub quota. We pull only
# what is missing and tag it under the Docker Hub name, so Dockerfiles and compose.yaml stay unchanged.
BASE_IMAGES ?= library/postgres:18 library/debian:trixie-slim library/python:3.14-slim docker/dockerfile:1
IMAGE_MIRROR ?= mirror.gcr.io

base-images:  ## (M2) Pre-pull missing base images via mirror.gcr.io (avoids Docker Hub 429s)
	@for i in $(BASE_IMAGES); do n=$${i#library/}; \
	  if docker image inspect "$$n" >/dev/null 2>&1; then echo "have $$n"; \
	  else docker pull -q "$(IMAGE_MIRROR)/$$i" && docker tag "$(IMAGE_MIRROR)/$$i" "$$n" && echo "pulled $$n via $(IMAGE_MIRROR)"; fi; \
	done

mocks: base-images  ## (M2) Start Postgres and the legacy stand-ins
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

drop:  ## Play the IBM i job: copy F=<csv> into the SFTP drop, THEN write its .done trigger
	@test -n "$(F)" || { echo "usage: make drop F=fixtures/csv/<file>.csv"; exit 2; }
	mkdir -p var/sftp-drop
	cp "$(F)" "var/sftp-drop/$$(basename "$(F)").tmp"
	mv "var/sftp-drop/$$(basename "$(F)").tmp" "var/sftp-drop/$$(basename "$(F)")"
	touch "var/sftp-drop/$$(basename "$(F)").done"
