# P01 — Legacy Integration Gateway (Meridian Freight, composite scenario)

A learning build from the FDE Onboarding Handbook. A Python 3.14 FastAPI gateway sits in front of a legacy AS/400 SFTP drop and a fragile SOAP rate-quote service. It provides idempotent quotes, a bulkhead, retries and a circuit breaker, signed webhooks through an outbox, and RFC 9457 errors.

**Status:** not built yet. The build is done in Claude Code cloud sessions, following [`docs/CLOUD_BUILD_PROMPT.md`](docs/CLOUD_BUILD_PROMPT.md).

| Path | What it is |
|---|---|
| [`spec/P01-legacy-integration-gateway.md`](spec/P01-legacy-integration-gateway.md) | The P01 spec: source of truth |
| [`spec/ground-truth-digest.md`](spec/ground-truth-digest.md) | Verified versions and dates (Sept 2026) |
| [`spec/P01-P04-full-projects-file.md`](spec/P01-P04-full-projects-file.md) | P01–P04 for context |
| [`CLAUDE.md`](CLAUDE.md) | Environment facts, native fallback and safety rules for Claude Code |
| [`scripts/cloud-setup.sh`](scripts/cloud-setup.sh) | Idempotent toolchain setup for the cloud VM |
| [`docs/CLOUD_BUILD_PROMPT.md`](docs/CLOUD_BUILD_PROMPT.md) | Prerequisites, the kickoff prompt and the resume prompt |
| `docs/BUILD_LOG.md` | Step-by-step setup/build/deploy/test record (created during the build) |
