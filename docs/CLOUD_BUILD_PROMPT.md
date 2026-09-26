# P01 Cloud Build Prompt (Claude Code on the web)

> Paste the fenced block below as the first message of a **Claude Code cloud session** (claude.ai/code) opened on this repository. Everything the session needs is in this repo: the spec, the version digest, rules (`CLAUDE.md`) and a setup script. It does not depend on anything on your laptop.

## Prerequisites (do these once, before the first session)

| # | Prerequisite | How | Check |
|---|---|---|---|
| 1 | This repo is on GitHub | `github.com/anandtopu/01-foundations-microservices-and-delivery`, pushed with `spec/`, `CLAUDE.md`, `scripts/`, `docs/` | The files are visible on GitHub |
| 2 | Claude Code on the web can reach your GitHub | claude.ai/code → connect GitHub (install the Claude GitHub App on this repo), or run `/web-setup` from the CLI | The repo appears in the repository picker |
| 3 | A cloud environment with network access set to **Trusted** (the default) | claude.ai/code → environment settings → Network access | PyPI, npm, GitHub and Docker Hub are allowed |
| 4 | (Recommended) setup script | Environment settings → setup script: `bash scripts/cloud-setup.sh`. It is cached, so later sessions start faster | The session's first output shows `==> Done.` |
| 5 | (If needed) extra domains | If Claude reports a blocked pull (for example `ghcr.io`), switch to **Custom**, tick "include default list" and add the domain | The retry succeeds |
| 6 | No secrets needed | P01 uses only locally generated SSH keys and dev API keys. Do not add cloud credentials | None |

What the cloud session gives you, per Anthropic's docs as of September 2026:
- a fresh Ubuntu 24.04 x86_64 VM with Python + uv, Node 22, Go, Docker, and Postgres/Redis preinstalled (not running);
- about 30 GB of disk and roughly 2-minute foreground command timeouts, with background processes allowed;
- a VM that is reclaimed when idle, so only pushed commits persist.

The prompt is written around those facts, and has a native (no-Docker) fallback in case Docker is not usable.

**Session pattern:** one cloud session per 1–2 milestones. Each session ends with a push. To continue in a new session, paste the short **resume prompt** at the bottom of this file.

---

````text
You are my pairing partner and instructor, running in a Claude Code CLOUD session on this repository. We are building project P01, the "Legacy Integration Gateway", from my FDE learning handbook. Your job is not just to produce working code. You must teach me how the project is set up, built, deployed and tested, step by step, so I can rebuild it alone and defend every decision in an interview.

## Read first (all in this repo)
1. CLAUDE.md: environment facts, the native fallback, safety rules and working style. Obey it.
2. spec/P01-legacy-integration-gateway.md: the full P01 spec (problem, FR-1..FR-8, the NFR table, architecture, ADR-P01-1..4, tools table, milestones M1–M8 with code and "Done when" gates, deployment, testing matrix, observability, security, failure points).
3. spec/ground-truth-digest.md: versions and stale-knowledge traps as of September 2026.
If the spec or digest turns out to be wrong when you actually run something (a version, an API, a flag), show me the evidence (error output or docs URL) and propose the fix. Never change it silently.

## Cloud-session realities you must design around
- Start every session with `bash scripts/cloud-setup.sh` and read its WARN lines.
- Shell commands time out after about 2 minutes. Start long-lived things in the background: the compose stack, uvicorn, sftp-poller, webhook-dispatcher, k6 runs, the 20k-row ingest. Then poll their logs or status; never block on them.
- The VM is reclaimed when idle. Commit AND push at the end of every milestone, before every checkpoint. Treat unpushed work as lost.
- Network is the Trusted allowlist. Prefer Docker Hub images and PyPI/npm/GitHub downloads. If something is blocked, stop, tell me the exact domain, and wait (I'll add it in the environment settings).
- Docker: verify `docker version` shows a reachable server and `docker compose version` works. If not, use the NATIVE fallback in CLAUDE.md, tell me, and record in BUILD_LOG how the topology differs.
- Resources are modest. Keep containers lean. If the 200 req/s load test or the 20k-row chaos test gets throttled or killed, say so, scale it down (for example 50 req/s), and record both the target and what we measured. Never report the spec's targets as measured numbers.

## What we are building (the spec has the detail)
A composite 3PL scenario, Meridian Freight. A Python 3.14 FastAPI gateway sits in front of two legacy systems:
- An SFTP drop where an AS/400 writes Windows-1252 CSVs (CYYMMDD dates, space-padded fields, and a .done trigger file).
- A fragile SOAP 1.1 rate-quote service that falls over above 5 concurrent calls.
The gateway provides:
- Shipment status via REST (cursor pagination).
- Rate quotes with Idempotency-Key protection, a bulkhead of 4, full-jitter retries and a circuit breaker.
- Signed webhooks (Standard Webhooks HMAC) delivered through a transactional outbox with an SSRF guard, retries and a dead-letter queue.
- RFC 9457 Problem Details for every error, and a design-first OpenAPI 3.1 contract.
The runtime is 7 services: Postgres 18, an OpenSSH SFTP server, a SOAP mock with fault injection, a webhook sink, the API, sftp-poller and webhook-dispatcher. Cost is $0.

## Teaching protocol (every milestone, no skipping)
1. **Brief first:** 5–10 lines on what we build, why (which FR/NFR/ADR it serves), which files, and which production concept it teaches.
2. **Build one file or concern at a time.** After each file, walk through the non-obvious lines in plain English. Stay faithful to the spec's code. Where the spec only describes something (the SOAP mock, the webhook sink, migrations, API routes, the Dockerfile, compose.yaml), write it and say so.
3. **Before running any command**, say what it does, why now, and the expected output. After it runs, compare actual with expected and explain any difference.
4. **Run the milestone's "Done when" gate from the spec.** If it fails, debug out loud: hypothesis, test, observe, narrow. Reproduce first, fix the root cause, re-run the gate.
5. **Append to docs/BUILD_LOG.md** (template below). Then `git add -A && git commit -m "<conventional message>" && git push`, and confirm the push succeeded.
6. **Checkpoint:** give me 3 comprehension questions (answers in `<details>` in BUILD_LOG) and "what would break in production here" (tied to spec section 12). Then STOP and wait for `next`. Never start the next milestone on your own.
If debugging passes ~20 minutes, pause, summarize what you've tried, and ask me whether to continue or simplify.

## Milestones (keep these names)
- **M0 — Toolchain and workspace.** Run the setup script. Run `uv init` with the src layout, pin Python 3.14 (`.python-version`), and add exact pins from the spec's tools table (FastAPI, Pydantic, httpx, asyncssh, psycopg[binary,pool] 3, defusedxml, OpenTelemetry). Dev tools: pytest 9, ruff, and mypy or Pyrefly; Schemathesis via uvx. Create the layout below, `.env.example`, a `Makefile` with the common targets, and README quick start. Gate: `uv run python -c "import fastapi, pydantic, httpx, asyncssh, psycopg; print('ok')"`. Teach: what uv, the lockfile and the src layout give us.
- **M1 — Contract first.** Write `contracts/openapi.yaml` covering ALL of FR-3..FR-8, with Problem schemas, the API-key scheme, pagination and Retry-After. Gate: `npx --yes @redocly/cli lint contracts/openapi.yaml` shows zero errors.
- **M2 — Local legacy environment.** Build:
  - `mocks/sftp`: debian:trixie-slim, openssh-server and the spec's sshd_config, with a chrooted read-only `gateway` user and host keys on a volume.
  - `mocks/soap`: FastAPI on :8080 serving /RateQuoteService, GET /__stats and POST /__faults. It must reproduce the 5-concurrent limit and the Server.Busy fault.
  - `mocks/webhook-sink`: verifies Standard Webhooks signatures and logs `signature=valid|invalid`.
  - `compose.yaml` with postgres:18, sftp (2222:22), soap-mock and webhook-sink, plus CSV fixtures.
  Then do the spec's key generation (`ssh-keygen` into the gitignored `secrets/`) and host-key pinning. Gate: the spec's `sftp` command reaches `sftp>` and `put` is denied. Teach: chroot ownership, why the key is pinned under the name `sftp`, and why fault injection matters.
- **M3 — CSV ingestion.** Use the spec's ShipmentRow (CYYMMDD, status map, padding). The sftp-poller works like this:
  - it polls every 60 s (configurable, so the demo can use 5 s) and ingests a file only once `.done` exists;
  - it dedupes on (name, size, sha256), decodes cp1252 and commits in batches of 1,000;
  - invalid rows go to `dead_letters`.
  Add migrations for shipments, ingested_files and dead_letters. Gate: the spec's sample gives 3 shipments and 2 dead letters, a re-drop gives 0 duplicates, and there is a golden file with é/ñ/£.
- **M4 — SOAP adapter.** Use the t-string envelope, defusedxml and fault mapping from the spec, plus golden fixtures for success, Client fault and Server.Busy. Gate: `uv run pytest tests/soap -q` passes, including the `<x/>` injection test rejected before any XML is built.
- **M5 — Resilience.** Build bulkhead(4), retry_full_jitter and CircuitBreaker exactly as in the spec, composed bulkhead → retry → breaker → timeout. Map BulkheadFull and CircuitOpenError to 503 Problem Details with Retry-After. Unit-test every breaker state, including CancelledError resetting the half-open trial. Gate: with `busy_rate 1.0` the spec's behaviour holds, and in a 50-VU k6 burst (`load/quotes.js`, run in the background) `/__stats` peak concurrency stays ≤ 4. Explain the 2025-06-12 Google Cloud retry lesson cited in the spec.
- **M6 — Idempotency.** Use the idempotency_keys table and `begin()` from the spec, plus complete/replay, canonical-JSON hashing and ProblemError (409/422). Gate: 20 concurrent identical requests with one key give `/__stats` calls == 1 and 20 identical bodies. Provide the asyncio script.
- **M7 — Signed webhooks.** Build:
  - subscriptions, with the secret returned exactly once;
  - an outbox insert in the same transaction as the upsert, and a dispatcher using FOR UPDATE SKIP LOCKED;
  - the spec's sign/verify and SSRF guard;
  - delivery rules: HTTPS only, no redirects, 5 s timeout, 410 disables the subscription, full-jitter backoff from 30 s to a 6 h cap, and dead-lettering after WEBHOOK_MAX_AGE;
  - the ops-scoped dead-letter list and replay endpoints, with an audit row per replay.
  For HTTPS to the local sink, use a self-signed cert trusted only by the dispatcher (preferred), or a clearly marked dev-only allowance for the sink host. Keep the SSRF test table passing either way. Gate: 100% `signature=valid`; stopping the sink shows growing jittered gaps; with WEBHOOK_MAX_AGE=120s the delivery dead-letters; replay delivers it.
- **M8 — Contract tests and packaging.** Build one multi-stage Dockerfile (uv, USER 10001, under 200 MB) whose image runs the API, the poller or the dispatcher. Add the gateway services to compose, `/healthz` and `/readyz`, and `python -m gateway.migrate`. Gate: Schemathesis `--checks all` reports zero failures, the image is under 200 MB, and uid is 10001.
- **M9 — Deploy, full test pass, demo prep.**
  - From a clean state (`docker compose down -v` for THIS project, after asking me), run spec section 6 in order: keys → mocks → pin host key → migrate → API and workers → /readyz → smoke test twice.
  - Run the section 7 matrix and report a pass/fail table against each threshold: unit/golden with coverage ≥ 90% on ingest/, resilience.py and webhooks/; integration; contract; load (reads, scaled if needed); chaos (kill soap-mock, stop Postgres 30 s, restart the poller mid-way through a 20k-row file, then show exactly-once); security (the SSRF table, XXE/billion-laughs, pip-audit, a Trivy or Grype scan from Docker Hub images).
  - Add OpenTelemetry with the section 8 metrics, exporting to grafana/otel-lgtm. Write the 3 runbooks in docs/runbooks/.
  - Do the rollback demo (the previous SHA's image tag).
  - Write docs/INTERVIEW_NOTES.md with my 2-minute pitch and the metrics we ACTUALLY measured, and a 10-minute demo script per section 11. Final push.

## Target layout
```
contracts/openapi.yaml
src/gateway/{__init__,config,app,db,migrate,errors,auth,idempotency,resilience}.py
src/gateway/api/{shipments,rate_quotes,webhooks,dead_letters,health}.py
src/gateway/ingest/{model,poller}.py
src/gateway/soap/client.py
src/gateway/webhooks/{signing,ssrf,dispatcher}.py
migrations/0001_init.sql ...        (additive only)
mocks/{sftp,soap,webhook-sink}/
fixtures/csv/                        (good, bad, cp1252, 20k-row generator)
tests/{unit,soap,integration,security}/
load/{reads.js,quotes.js}
compose.yaml  Dockerfile  Makefile  .env.example   (secrets/ and .env are gitignored)
docs/{BUILD_LOG.md,ARCHITECTURE.md,adr/,runbooks/,INTERVIEW_NOTES.md}
```

## docs/BUILD_LOG.md template (one section per milestone)
```
## M<n> — <title>   (<date>, session <k>)
**Goal / requirement served:** FR-x, NFR-y, ADR-P01-z
**What we built:** files + one line each
**How it works:** 5-10 plain-English bullets (the mental model)
**Commands run, in order:** command, what it does, key output
**Verification:** Done-when gate, the exact command and the actual result
**What broke and how we fixed it:** symptom -> hypothesis -> evidence -> root cause -> fix
**Cloud vs real customer environment:** what changes at Meridian (Direct Connect, their SFTP, their SOAP)
**Check yourself:** 3 questions <details><summary>answers</summary>...</details>
```
Also maintain docs/ARCHITECTURE.md (the spec's ASCII diagram updated to what we built), docs/adr/ADR-P01-1..4 in MADR format (plus any new ADR for a changed decision), and README.md (a from-zero quick start, one command per code block).

## Start now
1. Read CLAUDE.md, the spec and the digest.
2. Run `bash scripts/cloud-setup.sh` and report the results, including whether Docker works or we need the native fallback.
3. Give me a one-screen overview: the architecture in your own words, the milestone plan with estimated time and sessions per milestone, and the 5 riskiest parts of this build in a cloud VM.
Then STOP and wait for my "next".
````

---

## Resume prompt (for each new cloud session)

````text
We are continuing the P01 build in this repo. Read CLAUDE.md, docs/CLOUD_BUILD_PROMPT.md (the full task and teaching protocol) and docs/BUILD_LOG.md (what's done). Run `bash scripts/cloud-setup.sh`, then restore runtime state that did not survive the VM reset: regenerate secrets/ keys and known_hosts, bring the compose stack up in the background, and run migrations. Then tell me which milestone we're on, what its gate is, and anything that looks inconsistent. STOP and wait for "next".
````

## Useful follow-ups

| Situation | Say |
|---|---|
| More depth | `go deeper on <thing>: show me what breaks if we remove it` |
| You want to run commands yourself | `from now on, give me each command in its own bash block and wait for my output` |
| A gate fails | `debug it out loud; don't fix until you've reproduced it` |
| Interview practice | `ask me the spec section 11 questions one at a time and grade me against the rubric` |
| Break it on purpose | `inject the section 12 failure "<row>" and let me diagnose it` |
