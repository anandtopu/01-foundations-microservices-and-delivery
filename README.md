# P01 — Legacy Integration Gateway (Meridian Freight, composite scenario)

A learning build from the FDE Onboarding Handbook. A Python 3.14 FastAPI gateway sits in front of a legacy AS/400 SFTP drop and a fragile SOAP rate-quote service. It provides idempotent quotes, a bulkhead, retries and a circuit breaker, signed webhooks through an outbox, and RFC 9457 errors.

**Status:** M0 (toolchain) and M1 (OpenAPI 3.1 contract) done; see [`docs/BUILD_LOG.md`](docs/BUILD_LOG.md). The build is done in Claude Code cloud sessions, following [`docs/CLOUD_BUILD_PROMPT.md`](docs/CLOUD_BUILD_PROMPT.md).

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
