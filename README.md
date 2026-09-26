# P01 — Legacy Integration Gateway (Meridian Freight, composite scenario)

A learning build from the FDE Onboarding Handbook. A Python 3.14 FastAPI gateway sits in front of a legacy AS/400 SFTP drop and a fragile SOAP rate-quote service. It provides idempotent quotes, a bulkhead, retries and a circuit breaker, signed webhooks through an outbox, and RFC 9457 errors.

**Status:** M0 (toolchain), M1 (OpenAPI 3.1 contract), M2 (legacy stand-ins), M3 (CSV ingestion), M4 (SOAP adapter), M5 (resilience) and M6 (idempotency) done; see [`docs/BUILD_LOG.md`](docs/BUILD_LOG.md). The build is done in Claude Code cloud sessions, following [`docs/CLOUD_BUILD_PROMPT.md`](docs/CLOUD_BUILD_PROMPT.md).

| Path | What it is |
|---|---|
| [`spec/P01-legacy-integration-gateway.md`](spec/P01-legacy-integration-gateway.md) | The P01 spec: source of truth |
| [`spec/ground-truth-digest.md`](spec/ground-truth-digest.md) | Verified versions and dates (Sept 2026) |
| [`spec/P01-P04-full-projects-file.md`](spec/P01-P04-full-projects-file.md) | P01–P04 for context |
| [`CLAUDE.md`](CLAUDE.md) | Environment facts, native fallback and safety rules for Claude Code |
| [`scripts/cloud-setup.sh`](scripts/cloud-setup.sh) | Idempotent toolchain setup for the cloud VM |
| [`docs/CLOUD_BUILD_PROMPT.md`](docs/CLOUD_BUILD_PROMPT.md) | Prerequisites, the kickoff prompt and the resume prompt |
| [`docs/BUILD_LOG.md`](docs/BUILD_LOG.md) | Step-by-step setup/build/deploy/test record, one section per milestone |
| [`contracts/openapi.yaml`](contracts/openapi.yaml) | The design-first API contract (ADR-P01-4) |
| [`docs/adr/`](docs/adr/) | ADR-P01-1..4 in MADR format |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The architecture as built (diverges from the spec only where recorded) |

## Quick start (from zero)

These steps grow with each milestone. On a fresh Claude Code cloud VM or any Ubuntu 24.04 x86_64 host:

Install the toolchain (uv 0.12.19, Python 3.14.7, k6, and start dockerd if it is installed but stopped):

```bash
bash scripts/cloud-setup.sh
```

Install the locked dependencies into `.venv`:

```bash
uv sync --locked
```

Check that the core libraries import (the M0 gate):

```bash
uv run python -c "import fastapi, pydantic, httpx, asyncssh, psycopg; print('ok')"
```

Run lint, type checks and tests:

```bash
make check
```

Create your local environment file (gitignored):

```bash
cp .env.example .env
```

Lint the OpenAPI 3.1 contract (the M1 gate):

```bash
make contract-lint
```

On the cloud VM, uncomment `BUILD_CA_BUNDLE` in `.env` so the mock images can `pip install` through the session's TLS proxy:

```bash
sed -i 's|^# BUILD_CA_BUNDLE=|BUILD_CA_BUNDLE=|' .env
```

Generate the gateway's SFTP key into the gitignored `secrets/` (regenerate on every new VM):

```bash
make keys
```

Start Postgres 18, the SFTP drop, the SOAP mock and the webhook sink (base images come via mirror.gcr.io if missing):

```bash
make mocks
```

Pin the SFTP host key under the name the workers use, `sftp`, read from inside the container rather than trusted off the network:

```bash
make pin-hostkey
```

Play the IBM i job: drop the golden CSV and then its `.done` trigger:

```bash
make drop F=fixtures/csv/SHPSTS_20260924_0915.csv
```

Open an SFTP session as the gateway (the M2 gate). Expect `sftp>`; `put` fails with `Permission denied`:

```bash
sftp -i secrets/gateway_ed25519 -P 2222 -o UserKnownHostsFile=secrets/known_hosts -o HostKeyAlias=sftp -o StrictHostKeyChecking=yes gateway@localhost:/outbound/shipments
```

Apply the database migrations from the host (M8 moves this into the gateway image):

```bash
make migrate-local
```

Run one poller cycle: it ingests every CSV whose `.done` exists (the M3 gate expects 3 shipments, 2 dead letters):

```bash
make poll-once
```

Check the result with the spec's query:

```bash
docker compose exec postgres psql -U gateway -d gateway -c "select count(*) from shipments; select reason from dead_letters;"
```

Or keep the poller running every 5 s while you drop files from another terminal:

```bash
make poll-local
```

Run the SOAP adapter tests (the M4 gate; the golden tests need nothing running, the live ones use the mock):

```bash
uv run pytest tests/soap -q
```

Re-record the SOAP golden fixtures from the mock (only when the mock's responses change):

```bash
uv run python tests/soap/record_fixtures.py
```

Store the dev API keys from `.env` (only their SHA-256 hashes reach the database):

```bash
make dev-keys
```

Run the gateway API on the host (port 8000) against the mocks, in its own terminal:

```bash
make api-local
```

Ask for a rate quote (201, or a 503 Problem Details with `Retry-After` when Meridian is saturated):

```bash
curl -s -X POST localhost:8000/v1/rate-quotes -H 'X-API-Key: dev-shipper-key' -H 'Content-Type: application/json' -H 'Idempotency-Key: readme-0001-aaaaaaaa' -d '{"origin_zip":"30301","dest_zip":"60601","weight_lb":1200,"service_level":"LTL_STANDARD"}'
```

Reset the mock's counters, so its peak concurrency reflects only the burst:

```bash
curl -s -X POST localhost:8080/__reset
```

Burst it with 50 virtual users for 20 s (the M5 gate), then see what Meridian's side saw (`peak_concurrency` must be at most 4):

```bash
make load-quotes
```

```bash
curl -s localhost:8080/__stats
```

Run the same request again: same key, same body, so the stored quote is replayed (`Idempotent-Replayed: true`) and Meridian is not called:

```bash
curl -si -X POST localhost:8000/v1/rate-quotes -H 'X-API-Key: dev-shipper-key' -H 'Content-Type: application/json' -H 'Idempotency-Key: readme-0001-aaaaaaaa' -d '{"origin_zip":"30301","dest_zip":"60601","weight_lb":1200,"service_level":"LTL_STANDARD"}'
```

Fire 20 concurrent identical requests with one key (the M6 gate: expect `{'calls': 1}` and `GATE: PASS`):

```bash
uv run python load/idempotency_burst.py
```

List every other target:

```bash
make help
```

## Layout

```text
contracts/openapi.yaml          design-first OpenAPI 3.1 contract (M1)
src/gateway/                    the importable package (src layout)
  api/  ingest/  soap/  webhooks/
migrations/                     additive-only SQL
mocks/{sftp,soap,webhook-sink}/ legacy stand-ins (M2)
fixtures/csv/                   golden and generated CSV files
tests/{unit,soap,integration,security}/
load/                           k6 scripts
docs/{BUILD_LOG.md,ARCHITECTURE.md,adr/,runbooks/}
secrets/                        gitignored: SSH keys, pinned known_hosts
```
