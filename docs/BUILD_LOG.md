# BUILD_LOG: P01 Legacy Integration Gateway

One section per milestone, in the order built. Every number here was measured in this lab. The spec's targets are quoted as targets, never as results.

## Session environment (session 1, 2026-09-26)

| Item | Observed | Spec / digest | Notes |
|---|---|---|---|
| VM | Ubuntu 24.04.4, x86_64, 4 vCPU, 15 GiB RAM, no swap | "modest" | Load targets may need scaling (M9) |
| uv | 0.8.17 preinstalled → **0.12.19** after fix | 0.12.18 (2026-09-22) | One patch newer than the digest; harmless |
| Python | 3.14.0rc2 (old uv) → **3.14.7** after fix | 3.14.7 | See M0 "What broke" |
| Docker | dockerd installed but not running → started by us; Engine **29.3.1**, API 1.54, containerd image store, cgroup v1 | Engine 29.x, containerd store | Docker path, **not** the native fallback |
| Compose | v5.1.1 | "Compose v2" | Compose's major version moved past 2; the `docker compose` CLI is unchanged |
| postgres:18 | 18.6 | 18.6 | Pulled from Docker Hub OK |
| k6 | v2.3.0 | v2.3.0 | GitHub release download OK |
| Container apt | `deb.debian.org` → **403** | n/a | Blocks the M2 SFTP image until the host is added to the network allowlist |
| Container pip | `CERTIFICATE_VERIFY_FAILED` | n/a | The session proxy's CA is not trusted inside containers; fix at build time with a BuildKit secret (M2/M8) |

---

## M0 — Toolchain and workspace   (2026-09-26, session 1)

**Goal / requirement served:** spec section 2 Constraints (Python 3.14; secrets from environment, never in the image), section 4 tools table (exact pins), ADR-P01-4 groundwork ("pin exactly" so the contract tests in M8 are reproducible).

**What we built:**
- `scripts/cloud-setup.sh`: now installs the pinned uv 0.12.19 from PyPI, warns if Python 3.14 resolves to a pre-release, and starts `dockerd` when it is stopped.
- `pyproject.toml`: the `meridian-gateway` distribution; import package `gateway`; exact runtime pins; a `dev` dependency group; pytest/ruff/mypy config.
- `.python-version`: `3.14` (resolves to 3.14.7).
- `uv.lock`: 68 packages, all with hashes.
- `src/gateway/` with the `api`, `ingest`, `soap` and `webhooks` subpackages (empty `__init__.py`; modules arrive in their milestones).
- The rest of the target tree, with `.gitkeep` in empty directories. `secrets/` is `chmod 700` and gitignored.
- `tests/unit/test_toolchain.py`: asserts a *final* 3.14, that t-strings work, and that the package imports.
- `.env.example`: every section 6 variable plus the demo knobs (poll interval, `WEBHOOK_MAX_AGE`).
- `Makefile`: `check`, `test`, `cov`, `contract-lint`, `keys`, `mocks`, `pin-hostkey`, `migrate`, `up`, `down`, `schemathesis`, `audit`, `reset`.
- `README.md` quick start, `docs/ARCHITECTURE.md`, and this log.

**How it works:**
- `uv` is package manager, virtualenv manager and Python installer in one binary. `.python-version` tells it which interpreter to use, and it downloads that interpreter if it is missing.
- `pyproject.toml` states *intent* (our direct dependencies, pinned with `==`). `uv.lock` records the *resolution*: every transitive package, version and hash. `uv sync --locked` refuses to run if the two disagree.
- The **src layout** puts the code under `src/gateway`, so `import gateway` resolves only to the *installed* package, never to a folder that happens to be in the working directory. Tests therefore exercise what ships in the image.
- pytest runs with `--import-mode=importlib` and no `tests/__init__.py`, so pytest does not add the repo root to `sys.path`. This keeps the src-layout guarantee intact.
- Dev tools live in a PEP 735 dependency group, so the M8 image (`uv sync --no-dev`) contains none of them.
- The Makefile is a thin wrapper. Each target is one or two plain commands you can type yourself.

**Commands run, in order:**
1. `bash scripts/cloud-setup.sh`: first run. uv 0.8.17 installed Python 3.14.0rc2, and Docker gave a WARN (daemon not reachable).
2. `nohup dockerd &`: started the daemon. `docker version` → server 29.3.1.
3. `docker run --rm postgres:18 postgres --version` → `PostgreSQL 18.6`. Docker Hub works.
4. `apt-get update` inside `debian:trixie-slim` → `403 Forbidden` from `deb.debian.org`.
5. `pip download fastapi` inside `python:3.14-slim` → `CERTIFICATE_VERIFY_FAILED`.
6. `git checkout -b build/p01`.
7. `uv self update` → `GitHub API rate limit exceeded`. The astral.sh installer → HTTP 403.
8. `uv tool install --force uv==0.12.19` → uv 0.12.19. `uv python install 3.14` → Python 3.14.7.
9. Patched and re-ran `cloud-setup.sh` → clean and idempotent (no WARN).
10. `uv init --package --name meridian-gateway --python 3.14 --build-backend uv --vcs none --no-readme`: created the scaffold.
11. `uv sync` → `Resolved 68 packages`; every package came from a wheel, so nothing was compiled.
12. `make check` → first failure (see below), then green.

**Verification:** the Done-when gate.

```text
$ uv run python -c "import fastapi, pydantic, httpx, asyncssh, psycopg; print('ok')"
ok
$ uv run python -c "import psycopg, sys; print(sys.version.split()[0], 'libpq', psycopg.pq.version(), psycopg.pq.__impl__)"
3.14.7 libpq 180006 binary
$ make check
All checks passed!            (ruff)
Success: no issues found in 5 source files   (mypy --strict)
3 passed in 0.02s             (pytest 9.1.1)
```

**What broke and how we fixed it:**
1. *Symptom:* `uv python install 3.14` installed **3.14.0rc2**. *Hypothesis:* the uv binary predates 3.14.0 final. *Evidence:* `uv --version` → 0.8.17 (built Sept 2025). `uv python list 3.14` showed nothing newer than rc2. *Root cause:* the setup script only installed uv when it was *missing*, never upgraded it. `uv self update` calls the GitHub API (rate-limited on the shared egress IP), and `astral.sh` is not on the Trusted allowlist (403). *Fix:* install the pinned uv wheel from PyPI (`uv tool install --force uv==0.12.19`), and warn if the interpreter's `releaselevel` is not `final`. A unit test now asserts a final 3.14.
2. *Symptom:* `make lint` failed with "2 files would be reformatted", although every Python file was clean. *Hypothesis:* ruff is formatting something that is not a `.py` file. *Evidence:* the diagnostic paths were `spec/P01-legacy-integration-gateway.md` and `spec/P01-P04-full-projects-file.md`. *Root cause:* ruff 0.16 formats Python code blocks inside Markdown. *Fix:* `extend-exclude = ["spec"]`. The spec is the read-only source of truth, so we exclude it and do not reformat it.

**Cloud vs real customer environment:** at Meridian, the toolchain comes from an internal PyPI mirror (Artifactory or Nexus), not from pypi.org. The same `uv.lock` hashes let you prove the mirror served identical bytes. The Python and uv versions would be pinned in CI images, not installed per VM. The egress restrictions we hit (a blocked apt mirror, a TLS-inspecting proxy) are *normal* in a customer VPC, and handling them explicitly is FDE work: ask for the mirror early, and never "fix" TLS by turning verification off.

**Check yourself:**
1. What does `uv.lock` protect against that `fastapi==0.141.1` in `pyproject.toml` does not?
2. Why does the src layout matter for tests, and what did we change in pytest's config to keep that guarantee?
3. `uv self update` failed. Why didn't we fix it with `curl -k` or by setting `SSL_CERT_FILE` to skip verification, and how did we get the new uv instead?

<details><summary>answers</summary>

1. The `==` pin fixes only our *direct* dependency. FastAPI itself depends on Starlette, Pydantic-core, anyio and others with version *ranges*, so two installs a week apart can differ. The lockfile pins every transitive package and its sha256 hash, which gives reproducibility (the same tree in CI, the image and your laptop) and integrity (a tampered or re-uploaded wheel fails the hash check).
2. With a flat layout, `import gateway` can pick up the source folder in the current directory instead of the installed package. Tests could then pass against code that isn't actually packaged (a missing file in the wheel, or a wrong `module-name`). With the src layout only the installed package is importable. pytest's default "prepend" mode would still add the repo root to `sys.path` if `tests/__init__.py` existed, so we deleted that file and set `--import-mode=importlib`.
3. The failures were not certificate problems: the GitHub API rate limit and a 403 on astral.sh are policy and quota problems, and disabling TLS verification would only hide the next real problem while teaching a dangerous habit. PyPI was allowed, and uv is published there as a wheel, so the old uv installed the pinned new uv from PyPI (`uv tool install --force uv==0.12.19`), with the version pinned in the setup script.
</details>

---

## M1 — Contract first   (2026-09-26, session 1)

**Goal / requirement served:** FR-3 through FR-8, ADR-P01-4 (design-first OpenAPI 3.1), ADR-P01-1 ("the limit goes in the API contract"), success criterion 5.

**What we built:**
- `contracts/openapi.yaml`: OpenAPI 3.1 contract. 9 operations: shipments list/get, rate-quote create/get, webhook-subscription create/get/delete, dead-letter list/replay, plus `/healthz` and `/readyz`. Three events in the top-level `webhooks` section. RFC 9457 `Problem` schema, `X-API-Key` scheme, cursor pagination, and `Retry-After` on 409/429/503.
- `redocly.yaml`: states the `recommended` ruleset explicitly.
- `.redocly.lint-ignore.yaml`: 3 per-location exceptions, each with a reason.
- `tests/unit/test_contract.py`: 40 house-rule tests covering FR coverage, Problem Details everywhere, 401/429/500 on authenticated operations, Retry-After, the Idempotency-Key header, `limit` ≤ 200, 403 on ops endpoints, `additionalProperties: false`, and Python-compatible regex patterns.
- `docs/adr/ADR-P01-1..4`: the four spec decisions in MADR 4.0 format.
- `Makefile`: `contract-lint` pinned to `@redocly/cli@2.54.3`. `pyproject.toml`: PyYAML 6.0.3 as a dev-only dependency.

**How it works:**
- The spec's M1 snippet (the `createRateQuote` operation and the `RateQuoteRequest` schema) is embedded verbatim. Everything else is our design, reviewed "as a shipper".
- Every error response points at one shared `Problem` schema under `application/problem+json`. The `type` URI is the stable machine-readable part; `detail` is for humans.
- Pagination is cursor-based and ordered by `(updated_at, shipment_id)`. The cursor is opaque, so we can change its encoding without breaking clients, and it binds to its filter.
- Money and weights are **strings with a decimal pattern** in responses, so JavaScript clients never round `412.10` through a binary float. The *request* keeps the spec's `weight_lb: number`.
- Requests use `additionalProperties: false`, so the API must reject unknown fields. Schemathesis's negative tests will check that in M8.
- Records belonging to another shipper return `404`, never `403`, so the API does not confirm that a foreign ID exists (BOLA, OWASP API1:2023).
- OpenAPI 3.1's `webhooks` section documents the *outbound* events, including the Standard Webhooks `webhook-id`, `webhook-timestamp` and `webhook-signature` headers and the `410 Gone` behaviour.
- The contract-level choices that weren't in the spec:
  - a SOAP `Client` fault maps to `502` (the upstream rejected us; retrying won't help);
  - retries exhausted on `Server.Busy` map to `503` with `Retry-After`;
  - an expired quote returns `404`;
  - replaying a `row` dead letter returns `200 applied`; replaying a `webhook` one returns `202 queued`; an already-resolved one returns `409`.

**Commands run, in order:**
1. `npx --yes @redocly/cli@2.54.3 lint contracts/openapi.yaml`: valid, 3 warnings (the localhost server; no 4xx on the two probes).
2. `npx --yes @redocly/cli@2.54.3 lint contracts/openapi.yaml --generate-ignore-file`: wrote the 3 exceptions, which we then annotated.
3. `uvx openapi-spec-validator contracts/openapi.yaml` → `OK` (independent 3.1 validator).
4. `uv add --dev pyyaml==6.0.3`, then `make check` → one E501 in the new test, fixed; then 43 passed.
5. A mutation check: removed `Retry-After` from the 503 and made the shared 404 `application/json` → **6 failed** (as intended). Restored the file → 40 passed.

**Verification:** the Done-when gate.

```text
$ make contract-lint      # npx --yes @redocly/cli@2.54.3 lint contracts/openapi.yaml
Woohoo! Your API description is valid. 🎉
3 problems are explicitly ignored.
```

Zero errors and zero unexplained warnings. `openapi-spec-validator`: `OK`. `make check`: ruff clean, mypy clean, 43 passed.

**What broke and how we fixed it:**
1. *Symptom:* a design mistake caught during self-review, before running anything: `parameters: { $ref: '#/components/x-webhook-parameters' }`. *Root cause:* in OpenAPI a Reference Object stands in for **one** object, never for an array. *Fix:* three `components/parameters` (`WebhookId`, `WebhookTimestamp`, `WebhookSignature`), each referenced individually.
2. *Symptom:* 3 Redocly warnings. *Decision:* each is a deliberate exception, suppressed **per location** in `.redocly.lint-ignore.yaml` with the reason written down. We did not turn the rules off globally and did not invent fake 4xx responses. Any *new* violation still warns.

**Spec gap found (needs a decision before M3):** section 9 requires every query to filter on the shipper's `client_id` (BOLA). The spec's `ShipmentRow` (shipment_id, order_no, status, ship_date, weight_lb) has **no shipper field**, so ingested rows cannot be attributed to a shipper. See the M1 checkpoint for the options.

**Cloud vs real customer environment:** at Meridian this contract goes to the three shipper teams for review *before* any code, and each signs it off. `servers` would list the real sandbox and production hosts behind their API gateway or WAF, and the per-key rate limit (429) would probably be enforced there too. The `Retry-After` and 5-concurrent facts come from a joint test window with Meridian, confirmed in writing (ADR-P01-1).

**Check yourself:**
1. Why does every error status the API *can* return have to be listed in the contract, even `500`?
2. A shipper asks for `GET /v1/shipments/{id}` of another shipper's shipment. Which status do they get, and why not `403`?
3. Why is `total_charge` a string like `"412.50"` and not the JSON number `412.5`?

<details><summary>answers</summary>

1. In M8, Schemathesis runs `--checks all`, which includes status-code conformance: any response code not documented for that operation is a failure. More importantly, clients generate code from the contract, so an undocumented status is an unhandled branch in every shipper's integration. The contract is a promise about *all* outcomes, not only the happy path.
2. `404`. A `403` would confirm the ID exists and belongs to someone else, which leaks information and helps enumeration (BOLA, OWASP API1:2023). The query filters on the caller's `client_id`, so from their point of view the record does not exist.
3. JSON numbers are usually parsed as IEEE-754 binary floats (JavaScript always does this), and decimals like `0.1` cannot be represented exactly, so money gets rounded and trailing zeros are lost. A string with a decimal pattern keeps the exact value, and clients parse it into a decimal type deliberately.
</details>

---

## M2 — Local legacy environment   (IN PROGRESS, 2026-09-26, session 2)

*The full section is written when the gate passes. This records the state so a VM reclaim loses nothing.*

**Session 2 start:** the VM was reclaimed and restored between M1 and M2 (`uptime` 0 min, disk intact, processes gone). `cloud-setup.sh` restarted `dockerd` through the new auto-start path (`INFO: docker daemon not running; starting dockerd`), the first live test of that fix.

**Done so far (pushed in `cc7da69`):** the SOAP mock, webhook sink, shared Python mock Dockerfile (proxy CA as a BuildKit secret, verified absent from the image), the SFTP Dockerfile, `sshd_config` and entrypoint, `compose.yaml`, the golden CSV with `SHIPPER_CODE` (decision A), `make drop`, `make keys` (key generated, gitignored). `postgres`, `soap-mock` and `webhook-sink` are up and healthy. `tests/integration/test_mocks.py`: 6/6 passed on 3 consecutive runs.

**Debugged:** `test_above_five_concurrent_is_server_busy` expected peak concurrency 8 for 8 simultaneous calls and got 6. The mock logs showed rejected calls finish in about 1 ms, so they barely overlap each other. The test's model was wrong, not the mock: exceeding the limit always yields a peak of at least 6, which is what the test now asserts (and why "peak ≤ 4" is a sound M5 gate).

**Blocked:**
1. `docker compose build sftp` → `403 Forbidden` from `deb.debian.org` (trixie, trixie-updates, trixie-security). The host gets the same 403: a network-policy block.
2. Trying `ubuntu:24.04` as a plan B → Docker Hub `429 Too Many Requests` (anonymous pull rate limit on a shared egress IP).
