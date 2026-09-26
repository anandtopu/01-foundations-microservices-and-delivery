# Thin wrappers around the commands in README and the spec. Every target is also runnable by hand.
# Targets marked (Mn) need files that milestone creates.
SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE ?= docker compose

.PHONY: help setup sync lint fmt typecheck test cov check contract-lint keys certs base-images mocks pin-hostkey image migrate migrate-local dev-keys poll-local poll-once api-local dispatch-local load-quotes up down logs schemathesis audit reset drop

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
	@chown 10001:10001 secrets/gateway_ed25519 2>/dev/null || true  # (M8) the poller runs as uid 10001
	cp secrets/gateway_ed25519.pub mocks/sftp/gateway_ed25519.pub

# Docker Hub allows 100 anonymous pulls/h per egress IP, and the cloud VM shares its IP, so we hit 429.
# mirror.gcr.io is Google's read-through cache of Docker Hub: same image digests, no Hub quota. We pull only
# what is missing and tag it under the Docker Hub name, so Dockerfiles and compose.yaml stay unchanged.
BASE_IMAGES ?= library/postgres:18 library/debian:trixie-slim library/python:3.14-slim library/python:3.14-alpine docker/dockerfile:1
IMAGE_MIRROR ?= mirror.gcr.io

base-images:  ## (M2) Pre-pull missing base images via mirror.gcr.io (avoids Docker Hub 429s)
	@for i in $(BASE_IMAGES); do n=$${i#library/}; \
	  if docker image inspect "$$n" >/dev/null 2>&1; then echo "have $$n"; \
	  else docker pull -q "$(IMAGE_MIRROR)/$$i" && docker tag "$(IMAGE_MIRROR)/$$i" "$$n" && echo "pulled $$n via $(IMAGE_MIRROR)"; fi; \
	done

certs:  ## (M7) Lab CA + webhook-sink TLS cert in secrets/ (gitignored); only the dispatcher trusts the CA
	@mkdir -p secrets/sink-tls && chmod 700 secrets
	@test -f secrets/webhook-ca.key || openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
	  -days 30 -subj "/CN=Meridian lab webhook CA" -addext "keyUsage=critical,keyCertSign,cRLSign" \
	  -keyout secrets/webhook-ca.key -out secrets/webhook-ca.crt 2>/dev/null
	@test -f secrets/sink-tls/tls.key || { \
	  printf 'subjectAltName=DNS:webhook-sink,DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n' > secrets/sink-tls/ext.cnf && \
	  openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=webhook-sink" \
	    -keyout secrets/sink-tls/tls.key -out secrets/sink-tls/tls.csr 2>/dev/null && \
	  openssl x509 -req -in secrets/sink-tls/tls.csr -CA secrets/webhook-ca.crt -CAkey secrets/webhook-ca.key \
	    -CAcreateserial -days 30 -extfile secrets/sink-tls/ext.cnf -out secrets/sink-tls/tls.crt 2>/dev/null && \
	  rm -f secrets/sink-tls/tls.csr secrets/sink-tls/ext.cnf; }
	@cp secrets/webhook-ca.crt secrets/sink-tls/ca.crt
	@chown -R 10001:10001 secrets/sink-tls 2>/dev/null || true  # the sink runs as uid 10001
	@chmod 600 secrets/sink-tls/tls.key
	@openssl verify -CAfile secrets/webhook-ca.crt secrets/sink-tls/tls.crt

mocks: base-images certs  ## (M2) Start Postgres and the legacy stand-ins
	$(COMPOSE) up -d --build postgres sftp soap-mock webhook-sink

pin-hostkey:  ## (M2) Pin the SFTP host key under the name the workers use ("sftp")
	$(COMPOSE) exec -T sftp sh -c 'echo "sftp $$(cut -d" " -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"' > secrets/known_hosts

# Until M8 packages the gateway, run it on the host against the published ports. The host key stays
# pinned under "sftp" (the in-network name); SFTP_HOST_KEY_ALIAS makes the lookup use that entry.
HOST_ENV = DATABASE_URL=postgresql://gateway:gateway@localhost:5432/gateway \
	SFTP_HOST=localhost SFTP_PORT=2222 SFTP_HOST_KEY_ALIAS=sftp \
	SFTP_KEY_PATH=secrets/gateway_ed25519 SFTP_KNOWN_HOSTS=secrets/known_hosts

migrate-local:  ## (M3) Apply migrations from the host to the Compose Postgres
	env $(HOST_ENV) uv run python -m gateway.migrate

dev-keys:  ## (M6) Store the .env dev API keys (hashed): DEV_SHIPPER_API_KEY -> ACME, DEV_OPS_API_KEY -> ops
	@set -a; . ./.env; set +a; \
	for spec in "$$DEV_SHIPPER_API_KEY ACME shipper dev-shipper" "$$DEV_OPS_API_KEY meridian-ops ops dev-ops"; do \
	  set -- $$spec; \
	  API_KEY="$$1" env $(HOST_ENV) uv run python -m gateway.auth add --client "$$2" --scope "$$3" --label "$$4" --allow-weak \
	    || echo "  (already registered: fine for dev keys)"; \
	done

poll-local:  ## (M3) Run the sftp-poller on the host, every 5 s (demo interval); Ctrl-C to stop
	env $(HOST_ENV) SFTP_POLL_INTERVAL_S=5 uv run python -m gateway.ingest.poller

poll-once:  ## (M3) One sftp-poller cycle on the host, then exit
	env $(HOST_ENV) uv run python -m gateway.ingest.poller --once

# Lab only: the local sink resolves to 127.0.0.1, which the SSRF guard refuses. This exact-name
# allowance (and the lab CA) exist only in these host targets, never in an image or production.
LAB_WEBHOOKS = WEBHOOK_DEV_ALLOW_HOSTS=localhost WEBHOOK_CA_BUNDLE=secrets/webhook-ca.crt

api-local:  ## (M5) Run the gateway API on the host, :8000, against the mocks; Ctrl-C to stop
	env $(HOST_ENV) $(LAB_WEBHOOKS) SOAP_BASE_URL=http://localhost:8080 uv run uvicorn gateway.app:app --port 8000 --no-server-header

dispatch-local:  ## (M7) Run the webhook dispatcher on the host (demo: WEBHOOK_MAX_AGE=120s make dispatch-local); Ctrl-C to stop
	env $(HOST_ENV) $(LAB_WEBHOOKS) uv run python -m gateway.webhooks.dispatcher

load-quotes:  ## (M5) 50-VU k6 burst on POST /v1/rate-quotes (needs api-local); then check /__stats
	k6 run --no-usage-report load/quotes.js

image: base-images  ## (M8) Build meridian-gateway (one image: API, poller, dispatcher, migrations) and show its size
	$(COMPOSE) build gateway-api
	docker tag meridian-gateway:latest meridian-gateway:$$(git rev-parse --short HEAD)  # for rollback (spec section 6)
	docker image ls meridian-gateway

migrate:  ## (M8) Apply additive migrations
	$(COMPOSE) run --rm gateway-api python -m gateway.migrate

up:  ## (M8) Start the API and both workers (after make image and make migrate)
	$(COMPOSE) up -d --no-build gateway-api sftp-poller webhook-dispatcher

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
