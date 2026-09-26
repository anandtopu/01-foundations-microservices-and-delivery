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
| Container apt (session 3) | `deb.debian.org` → **200** | n/a | Now reachable; the M2 SFTP image builds |
| Docker Hub | **429** on a shared egress IP (`ratelimit-limit: 100;w=3600`, 3 left at session 3 start) | n/a | Workaround: `make base-images` pulls via `mirror.gcr.io` (see M2) |

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

## M2 — Local legacy environment   (2026-09-26, sessions 2–3)

**Goal / requirement served:** the test harness for FR-1/FR-2 (SFTP ingest), FR-3/FR-5 (SOAP quotes behind a bulkhead), FR-6/FR-7 (signed webhooks); ADR-P01-1 (the 5-concurrent limit is reproduced, not assumed); spec section 12 rows "chroot ownership" and "host key mismatch".

**What we built:**
- `mocks/sftp/Dockerfile`: `debian:trixie-slim` + `openssh-server` (10.0p1), a `gateway` user with a `nologin` shell, a root-owned chroot, and the gateway's public key in `authorized_keys`.
- `mocks/sftp/sshd_config`: the spec's config verbatim (key auth only, `ChrootDirectory /srv/sftp`, `ForceCommand internal-sftp -R`) plus lab lines, each marked: `HostKey`, `PermitRootLogin no`, `KbdInteractiveAuthentication no` and `LogLevel VERBOSE`.
- `mocks/sftp/entrypoint.sh`: generates the host key **once** onto the `sftp-hostkeys` volume, then `exec sshd -D -e`.
- `mocks/soap/app.py`: the SOAP 1.1 RateQuoteService mock on :8080. It enforces the 5-in-flight limit (HTTP 500 + `soapenv:Server.Busy`) and adds `busy_rate` and `latency_ms` faults. The control plane is `GET /__stats` (calls, in-flight, **peak** concurrency), `POST /__faults` and `POST /__reset`. It parses with `defusedxml`.
- `mocks/webhook-sink/app.py`: verifies Standard Webhooks HMAC signatures (supports multiple secrets, for rotation) and logs `signature=valid|invalid`. Its `__mode` endpoint makes it answer 410/500 and similar, for M7.
- `mocks/Dockerfile.python`: one image for both Python mocks. The proxy CA is a BuildKit secret, so it is never in a layer. Runs as `USER 10001`.
- `compose.yaml` (project `meridian`): `postgres:18`, `sftp` (2222:22), `soap-mock` (8080), `webhook-sink` (9000), each with a healthcheck. Postgres is published only on 127.0.0.1.
- `fixtures/csv/SHPSTS_20260924_0915.csv`: a byte-exact cp1252/CRLF golden file with `SHIPPER_CODE` (decision A).
- `tests/integration/test_mocks.py`: 6 behaviour tests of the mocks.
- `Makefile`: `keys`, `mocks`, `pin-hostkey`, `drop`, and new this session, `base-images`. `README.md`: the M2 quick-start steps.

**How it works:**
- **Chroot:** sshd `chroot()`s the gateway user into `/srv/sftp` only if every path component up to it is `root:root` and not group- or world-writable. We verified `root:root 755` on `/srv/sftp`, `/srv/sftp/outbound` and `/srv/sftp/outbound/shipments`. If the rule is broken, sshd drops the connection *after* auth with "bad ownership or modes", which looks like a network failure (spec section 12).
- **Read-only, twice:** `internal-sftp -R` refuses every write request at the protocol level, and the drop directory is also bind-mounted `:ro`. The gate's `put` is denied by the first layer, before the filesystem is even involved.
- **No shell:** `ForceCommand internal-sftp` means `ssh gateway@… id` gets "This service allows sftp connections only." The `nologin` shell is a second layer.
- **Host identity survives rebuilds:** the host key lives on a named volume, so `docker compose build` does not mint a new key and break every pinned `known_hosts`. On a new VM the volume is gone, so the key is new and must be re-pinned (as we did this session).
- **Pinning under `sftp`:** the workers connect to the Compose service name `sftp` on port 22, so ssh and asyncssh look up the key under `sftp`. A keyscan of `localhost:2222` records it under `[localhost]:2222`, a different name, so the workers would fail. `make pin-hostkey` reads the public key from **inside** the container (a trusted channel) and writes it under `sftp`. From the host we use `-o HostKeyAlias=sftp` to verify against that same entry.
- **Fault injection:** the gateway's resilience code (M5) is only as good as the failures it has been tested against. The mock makes "above 5 concurrent → `Server.Busy`" deterministic, so "peak ≤ 4" at `/__stats` is a measurement, not a hope.
- **Rate-limit workaround:** `make base-images` pulls missing base images from `mirror.gcr.io` (Google's read-through cache of Docker Hub, with the same digests) and tags them under the Hub name, so no Dockerfile changes. `docker/dockerfile:1` is included because the `# syntax=` line makes BuildKit fetch that frontend from Hub too.

**Commands run, in order (session 3):**
1. `bash scripts/cloud-setup.sh`: exit 0, **no WARN lines**. `INFO: docker daemon not running; starting dockerd`. Docker 29.3.1, Compose v5.1.1.
2. `curl -w '%{http_code}' https://deb.debian.org/debian/dists/trixie/Release` → `200` (it was 403 in session 2).
3. `uv sync --locked`, then `make keys`: a new ed25519 key in `secrets/`, whose `.pub` is copied into the build context (both gitignored).
4. `.env` from `.env.example` with `BUILD_CA_BUNDLE=/root/.ccr/ca-bundle.crt` uncommented.
5. `docker compose up -d --build …` (in the background) → **429** on `python:3.14-slim`; a retry of just `sftp` → **429** on `debian:trixie-slim`.
6. Docker Hub rate-limit probe → `ratelimit-limit: 100;w=3600`, `ratelimit-remaining: 3`, source `160.79.106.130` (shared egress). `mirror.gcr.io/v2/` → 401 (reachable; 401 is the normal auth challenge). `quay.io` → proxy 403.
7. `docker pull mirror.gcr.io/library/{debian:trixie-slim,python:3.14-slim,postgres:18}` and `mirror.gcr.io/docker/dockerfile:1`, then `docker tag` → all OK.
8. `docker compose up -d --build postgres sftp soap-mock webhook-sink` → all 4 `(healthy)`; apt inside the build fetched from `deb.debian.org`. Image sizes: sftp 160 MB, soap-mock 217 MB, webhook-sink 216 MB.
9. `make pin-hostkey` → `sftp ssh-ed25519 AAAA…` (`SHA256:T3MXTgEr+hUc7l8eJKwt7yeeWtLHiWJtelsSn0QOBew`, the same fingerprint the entrypoint logged). Cross-checked against `ssh-keyscan -t ed25519 -p 2222 localhost` → MATCH.
10. `make drop F=fixtures/csv/SHPSTS_20260924_0915.csv`, then the gate (below).
11. `uv run pytest tests/integration/test_mocks.py -q` → `6 passed`.

**Verification:** the Done-when gate. It is the spec's command, plus `HostKeyAlias=sftp` (see "What broke" 3), plus explicit `StrictHostKeyChecking=yes`, run in batch mode:

```text
$ sftp -b batch.txt -i secrets/gateway_ed25519 -P 2222 -o UserKnownHostsFile=secrets/known_hosts \
       -o HostKeyAlias=sftp -o StrictHostKeyChecking=yes gateway@localhost:/outbound/shipments
sftp> pwd
Remote working directory: /outbound/shipments
sftp> ls -l
-rw-r--r--    ? 0        0             347 Sep 26 17:48 SHPSTS_20260924_0915.csv
-rw-r--r--    ? 0        0               0 Sep 26 17:48 SHPSTS_20260924_0915.csv.done
sftp> -put probe.txt
dest open "/outbound/shipments/probe.txt": Permission denied
sftp> bye
```

Negative controls, to prove that the pin is what lets us in:

| Case | Result |
|---|---|
| Same command **without** `HostKeyAlias` (the key is pinned under `sftp`, not `[localhost]:2222`) | `Host key verification failed.` exit 255 |
| An empty `known_hosts` | `Host key verification failed.` exit 255 |
| `ssh gateway@… id` (asking for a shell) | `This service allows sftp connections only.` |

sshd log: `Accepted publickey for gateway … ED25519 SHA256:64hkPe…`, which is the key from `make keys`.

**What broke and how we fixed it:**
1. *(session 2)* `test_above_five_concurrent_is_server_busy` expected a peak of 8 and got 6. Rejected calls return in about 1 ms, so they barely overlap each other. The test's model was wrong, not the mock, so it now asserts peak ≥ 6.
2. *(sessions 2–3)* The SFTP build was blocked. *Session 2:* `deb.debian.org` returned 403 (network policy), and `ubuntu:24.04` as a plan B got a Hub 429. *Session 3:* Debian is now allowlisted, but Hub returned 429 on `python:3.14-slim` and then on `debian:trixie-slim`. *Hypothesis:* an anonymous per-IP quota. *Evidence:* the rate-limit headers show 3 of 100 left, on a shared egress IP. *Root cause:* the quota is shared with other tenants, so it is outside our control. *Fix:* `make base-images` through `mirror.gcr.io`, which leaves the Dockerfiles unchanged and needs no credentials. `make mocks` now depends on it, so it is automatic.
3. *A spec inconsistency (not changed; flagged):* the M2 gate pins with `ssh-keyscan -p 2222 localhost > secrets/known_hosts`, but section 6 (and `make pin-hostkey`) pins under `sftp`. They write different host names into the **same** file, and whichever runs last wins. If the section 6 pin runs last, the gate's command as written fails with "Host key verification failed". If the M2 keyscan runs last, the workers fail. The keyscan is also trust-on-first-use over the network. *Resolution in this lab:* one pin, under `sftp`, taken from inside the container, and the gate verified with `-o HostKeyAlias=sftp`. *Proposed spec fix:* change the gate's command to add `-o HostKeyAlias=sftp` and drop the keyscan line (or keep it only as a cross-check).
4. *A self-inflicted false positive, caught:* my first cross-check used `ssh-keyscan -q`, which this OpenSSH doesn't have. Both variables came back empty, and `"" = ""` printed MATCH. I re-ran it with the right flags and a non-empty guard. Lesson: a check that can't fail isn't a check.

**Cloud vs real customer environment:** Meridian's SFTP drop sits in their DMZ, reached over Direct Connect or a site-to-site VPN. Their security architect sends the host-key fingerprint **out of band** (a signed email or a ticket), and we compare it before pinning; nobody runs `ssh-keyscan` against production and trusts the answer. Our public key is registered through their change process, and access is IP-allowlisted. The SOAP service's concurrency limit is agreed in writing during a joint test window (ADR-P01-1). Nobody gets to inject faults into their production system, which is exactly why the mock exists. Base images come from the company's own registry mirror (Artifactory, ECR pull-through cache), never anonymously from Docker Hub.

**Check yourself:**
1. You change `/srv/sftp/outbound` to `chmod 775` owned by `root:gateway`. What does the gateway see when it connects, and where do you look to find out why?
2. The poller runs in the `meridian` Compose network and connects to `sftp:22`. Why does a `known_hosts` produced by `ssh-keyscan -p 2222 localhost` not work for it, and why is the keyscan a weaker pin anyway?
3. Why do we build a SOAP mock with fault injection instead of testing M5 against the "real" SOAP service in a shared test environment?

<details><summary>answers</summary>

1. Authentication **succeeds**, then the connection closes immediately ("Connection closed" / "broken pipe"). It looks like a network problem. The reason is only in the **server** log: `fatal: bad ownership or modes for chroot directory component "/srv/sftp/outbound/"`. sshd requires every component of `ChrootDirectory` to be root-owned and not group- or world-writable; otherwise the chrooted user could swap in their own `/etc` or libraries and escalate. Spec section 12: chroot ownership.
2. Clients look up the key by the name and port they connect to. The keyscan writes `[localhost]:2222`, but the poller asks for `sftp` (port 22 needs no brackets), so the lookup misses and, with checking enforced, the connection is refused. The keyscan is also TOFU (trust on first use): whoever answers on that port at that moment gets pinned, with no second channel to confirm it. Reading the key from inside the container (or, at Meridian, an out-of-band fingerprint) is the trusted channel.
3. You cannot make a real shared service produce `Server.Busy` on demand, at a controlled rate, or at a precise concurrency; and you must not load-test someone else's fragile production-like system. Without deterministic faults, the bulkhead, retry and breaker tests either never exercise the failure paths or pass by luck. `/__stats` peak concurrency also turns "we never exceed 4" into a number we can assert. Fault injection makes resilience testable, and it makes the limits reproducible in CI.
</details>

---

## M3 — CSV ingestion with legacy quirks   (2026-09-26, session 3)

**Goal / requirement served:** FR-1 (ingest once `.done` exists; poll every 60 s, 5 s for the demo), FR-2 (valid rows upsert; invalid rows go to `dead_letters` with file, line, raw row and reason), section 9 (every shipment carries its shipper's `client_id`, per decision A), and the groundwork for the M9 chaos row ("restart the poller mid-way through a 20k-row file, then show exactly-once").

**What we built:**
- `migrations/0001_init.sql`: the `shipments`, `ingested_files` and `dead_letters` tables. Includes the pagination index `(client_id, updated_at, shipment_id)`, a partial unique index so there is one dead letter per (file, line), and `uuidv7()` IDs (built into PG 18).
- `src/gateway/config.py`: `Settings` (pydantic-settings) for every section 6 variable M3 needs, plus `SFTP_HOST_KEY_ALIAS`.
- `src/gateway/db.py`: an async pool factory for the API (M5+). The poller uses plain connections.
- `src/gateway/migrate.py`: `python -m gateway.migrate`. Runs ordered `NNNN_*.sql` files, each in its own transaction with its `schema_migrations` row, under an advisory lock. It refuses to run if an applied file has been edited.
- `src/gateway/ingest/model.py`: the spec's `ShipmentRow` verbatim, plus marked additions:
  - `shipper_code` (decision A);
  - weight stripping, with `ge=0, max_digits=12, decimal_places=2`;
  - `parse_export()`, a pure function from bytes to `GoodRow` or `DeadRow`, with `FileRejected` for an undecodable file or wrong header.
- `src/gateway/ingest/poller.py`: the sftp-poller: advisory-lock leader, `.done` trigger, mtime fast path, sha256 dedupe, batch plus checkpoint in one transaction, and `--once`.
- `fixtures/csv/SHPSTS_20260925_0730.csv`: the cp1252 golden file (`CAFÉ`, `PEÑA`, `£REF`, `€QT`, plus a `C=0` date). `fixtures/csv/generate.py`: a deterministic N-row generator (the M9 20k file).
- `tests/unit/test_ingest_model.py` (23 tests) and `tests/integration/test_ingest.py` (9 tests, each run against a fresh `gateway_test` database).
- `Makefile`: `migrate-local`, `poll-once`, `poll-local`. Also `.env.example` updates, README quick start, and ARCHITECTURE (differences 5–8, plus an ingest data-flow diagram).

**How it works:**
- **Quirks at the boundary:** all IBM i weirdness (padding, CYYMMDD, one-letter statuses, cp1252) is handled by Pydantic `mode="before"` validators in one module with no I/O, so it's unit-testable in milliseconds. Everything downstream sees typed `ShipmentRow`s.
- **Line numbers are physical lines,** with the header as line 1, because that's what ops opens in an editor. CPYTOIMPF never puts a newline inside a field, so one line is one record.
- **Two kinds of bad input:** a bad *row* becomes a dead letter and the batch carries on (FR-2). A bad *file* (wrong header, a byte cp1252 doesn't define) is rejected whole, with one dead letter, because no row in it can be trusted.
- **Dedupe:** the drop is read-only to us, so we can't delete or rename processed files; `ingested_files` is our memory. The authoritative key is (name, size, sha256). The (name, size, mtime) fast path avoids re-downloading every finished file every 60 s.
- **Exactly-once:** each batch commits its upserts, its dead letters and the file's `last_line` in **one** transaction. A crash rolls back the in-flight batch and its checkpoint together, and the next run resumes after the last committed line.
- **Idempotent upsert:** `ON CONFLICT … DO UPDATE … WHERE (…) IS DISTINCT FROM EXCLUDED` makes an identical re-delivery a true no-op. `updated_at` (the pagination cursor key) moves only on real change. `RETURNING (xmax = 0)` tells inserts from updates, which M7 will use to emit webhook events.
- **One active poller:** `pg_try_advisory_lock` per cycle, scoped to the session, so it is released even if the process dies. A second replica is a hot standby, not a double-ingester.
- **Failure handling:** SFTP down, Postgres down or a host-key mismatch → log, then retry next cycle. Committed batches are safe. The host key is always verified; there is no `known_hosts=None` path.

**Commands run, in order:**
1. PR #1 (M0–M2) marked ready and merged with a merge commit (`cf84fe1`); `build/p01` fast-forwarded to it. Per your instruction, M3 stops at a PR for your review.
2. `grep host_key_alias .venv/.../asyncssh/connection.py` + `known_hosts.py`: asyncssh has `host_key_alias`, and with a non-default port it looks up `[sftp]:2222` first, then falls back to `sftp`. So one pin works both on the host and in Compose.
3. `DATABASE_URL=…localhost… uv run python -m gateway.migrate` → `applied 1 migration(s): 0001_init`; again → `none pending`. `\dt` shows 4 tables; `select uuidv7()` works.
4. `parse_export()` on the golden file → 3 `GoodRow`, and `DeadRow` for line 5 `status: unknown status code 'Q'` and line 6 `shipment_id: blank key field`.
5. `ruff` + `mypy --strict` → 2 real findings, fixed (see "What broke" 1).
6. `poller --once` from the host → `status=done rows_ok=3 rows_dead=2 inserted=3`.
7. Re-drop scenarios (a), (b), (c) → no duplicates, but an mtime bug (see "What broke" 3).
8. `pytest tests/unit/test_ingest_model.py` → 23 passed. `pytest tests/integration/test_ingest.py` → 9 passed.
9. Mutation testing of the integration suite (see "What broke" 4).
10. `make check` → ruff clean, mypy clean, **81 passed**.
11. The gate (below), with the poller running in the background every 5 s. Then a Postgres stop/start while it ran, then `kill <pid>` (only the PID we started).

**Verification:** the Done-when gate, with the poller running in the background (`SFTP_POLL_INTERVAL_S=5`) and the file dropped with `make drop`:

```text
$ docker compose exec postgres psql -U gateway -d gateway -c "select count(*) from shipments; select reason from dead_letters;"
 count
-------
     3
             reason
---------------------------------
 status: unknown status code 'Q'
 shipment_id: blank key field
```

| Check | Result |
|---|---|
| Sample → 3 shipments, 2 dead letters | ✅ exactly as above |
| Re-drop of the same file (new mtime) | ✅ `skipped: this content (sha256) was already ingested`; still 3 + 2 |
| Same bytes, new name | ✅ `inserted=0 updated=0`; `max(updated_at)` unchanged |
| cp1252 golden (é/ñ/£/€) | ✅ stored as `CAFÉ-8801`, `PEÑA-8802`, `£REF-8803`, `€QT-8804` |
| 7 shipments after all drops | ✅ `count(*) = count(distinct shipment_id) = 7` |
| Crash inside batch 3 of a 2,500-row file, then resume (test; the real process-kill run is M9's) | ✅ after the crash: checkpoint 2001, 1,960 rows; after the resume: 2,450 shipments, 50 dead letters, exactly once |
| Postgres stopped for ~10 s while polling | ✅ `poll failed: OperationalError …` every 5 s, no crash; recovered on its own |
| Wrong host key | ✅ `HostKeyNotVerifiable` (test) |
| CSV without `.done` | ✅ ignored (test) |

**What broke and how we fixed it:**
1. *Static analysis found two real bugs.*
   - mypy: asyncssh types SFTP names and reads as `str | bytes`. `f"{name}.done"` on bytes would produce `"b'x.csv'.done"` and never match. Fixed by narrowing explicitly (`text()`, plus an `isinstance` check on the read).
   - ruff ASYNC240: the migrator did blocking file I/O inside the event loop. Loading moved to a sync `load()` that runs before `asyncio.run`.
   - The `assert` became a `RuntimeError`, because asserts vanish under `python -O`.
2. *A self-review catch:* the poll loop caught `OSError`, but `psycopg.OperationalError` isn't a subclass of it. A Postgres restart would have **killed** the poller, which is exactly the M9 chaos case. Added it to the handler and proved it with a live Postgres stop.
3. *The mtime fast path missed forever after a re-drop.*
   - *Symptom:* `file_id` jumped from 1 to 4.
   - *Hypothesis:* every `ON CONFLICT` consumes an identity value, so there were two unexpected conflicts.
   - *Evidence:* with debug logs, the listing showed mtime `…5675` against `…4929` stored, and the file was re-downloaded and re-hashed on every cycle.
   - *Root cause:* `make drop` rewrote an identical file, giving it a new mtime, but the stored mtime was never refreshed.
   - *Fix:* `claim_file` became one `INSERT … ON CONFLICT DO UPDATE SET mtime = EXCLUDED.mtime RETURNING …`. Verified that cycle 1 does one hash check and cycle 2 takes the fast path; the regression test asserts the refreshed mtime.
4. *My crash test was too kind.*
   - Mutation 1 moved the checkpoint into its own transaction, committed *before* the rows. That is the classic at-most-once bug, and the test still **passed**. The simulation wrapped the batch in an outer transaction, which turned the mutant's early commit into a savepoint that was rolled back too.
   - *Fix:* `DiesAfterUpsert`, a connection proxy that raises right after the upsert, with no extra transaction. What was committed stays committed, like a real kill.
   - Mutation 1 now fails with a checkpoint of 2501 against rows up to line 2001, which would have silently lost 500 shipments. Mutation 2 (removing `IS DISTINCT FROM`) fails the "no-op re-drop" test. With the real code, all tests pass.
   - Lesson (again): a test proves nothing until you have seen it fail for the right reason.

**Known limits (for later milestones or production):**
- ~~The last writer wins, including `client_id`.~~ Fixed in the PR #2 review: an owner change is now dead-lettered (`owner change refused`), and `client_id` is never updated.
- File order is by name (the names carry the IBM i timestamp). An *older* file dropped late would overwrite newer statuses. A production fix compares a source timestamp or sequence number, if the IBM i team will add one.
- Each file is held in memory while it is processed. That's about 1.2 MB for 20k rows, which is fine; a multi-GB export would need streaming.

**Cloud vs real customer environment:** at Meridian the poller reaches the DMZ SFTP host over Direct Connect or a VPN, from an allowlisted egress IP. The key and `known_hosts` come from a secret store, and the fingerprint is confirmed out of band. The IBM i team's `.done` convention is a contract to agree in writing, since they "say no to changes". The dead-letter queue is how their data-quality problems reach them without paging us. Real exports are larger and arrive on their schedule, so batch size and poll interval are tuned against their job's timing. `numeric(12,2)` and the status map come from their field definitions (DDS), not guesses.

**Check yourself:**
1. Why must the `last_line` checkpoint be updated in the *same* transaction as the batch's upserts? What goes wrong if it commits just before, or just after?
2. The poller dedupes on (name, size, sha256), yet also looks at mtime. What is each one for, and why is mtime alone not safe as the dedupe key?
3. Line 5 of a 20k-row file has status `Q`, and the file's header says `SHIPMENT_NO` instead of `SHIPMENT_ID`. What does the gateway do in each case, and why the difference?

<details><summary>answers</summary>

1. The checkpoint describes which rows are in the database, so it has to become visible at the same instant they do. If it commits *before* the rows and the process dies in between, the checkpoint claims lines the database doesn't have: the resume skips them, and they are lost (at-most-once). Mutation 1 proved this: 500 rows vanished. If it commits *after* the rows and the process dies in between, the resume re-applies a batch. The upsert makes shipments harmless to repeat, but dead letters would duplicate without the unique index, and any side effect (M7's outbox events) would fire twice (at-least-once). One transaction gives exactly-once.
2. sha256 is the identity of the *content*: the same name and size with different bytes is a new export and must be ingested, and identical bytes are a duplicate however they arrived. But computing it requires downloading the file. (name, size, mtime) is a cheap *hint* that lets us skip finished files without a download every 60 s. mtime alone is unsafe because it changes when an identical file is re-copied (our `make drop` bug), and it can stay the same while content changes (clock skew, tools that preserve mtime). So it may only ever *skip work*, never *decide* identity.
3. Status `Q` is one bad **row**. It becomes a dead letter (line 5, raw row, `status: unknown status code 'Q'`), and the other 19,999 rows are ingested (FR-2: the batch continues). A wrong header means the **file** doesn't match the format we agreed, so every column mapping is suspect. The whole file is rejected (`status = rejected`, one dead letter at line 1), and nothing from it is written. Ingesting it anyway could put order numbers into shipment IDs across 20k rows.
</details>

**What would break in production here (spec section 12):**
- **The IBM i job writes `.done` before the CSV is complete** (or the job is rewritten without the trigger). We would ingest a truncated file. The size check only catches a change *during our download*. The contract is the `.done` ordering, and a trailer row with a record count would make it verifiable.
- **The host key rotates** (the SFTP host is rebuilt). Every cycle fails with `HostKeyNotVerifiable`, and shipments go stale while the API still serves old data. It needs an alert on consecutive poll failures and the out-of-band fingerprint process, never disabling verification.
- **The drop fills with years of files.** The listing and fast-path queries grow every cycle. Ask Meridian for an archive or retention job on their side; our side can't delete (read-only by design).

---

## M4 — SOAP adapter with safe XML   (2026-09-26, session 3)

**Goal / requirement served:** FR-4 (rate quotes through Meridian's SOAP service), ADR-P01-1 (M5's retry loop needs a clean retryable/non-retryable split), and section 9's tampering row (XXE and entity expansion in SOAP responses: `defusedxml` for every parse).

**What we built:**
- `src/gateway/soap/client.py`: the spec's adapter verbatim, with three marked lab additions (see "How it works"). It builds the envelope with a Python 3.14 t-string and `render_xml`, POSTs with `SOAPAction` and a 3 s / 0.5 s-connect timeout, and parses with `defusedxml`.
- `src/gateway/resilience.py`: `RetryableError` only for now. M5 adds the bulkhead, retry and breaker to this module.
- `src/gateway/api/rate_quotes.py`: `RateQuoteRequest`, mirroring the contract. M6 adds the route.
- `tests/soap/fixtures/{success.200,client_fault.500,server_busy.500}.xml`: golden responses **recorded from the mock** by `tests/soap/record_fixtures.py`. The HTTP status is part of the file name.
- `tests/soap/test_client.py` (31 tests, no network): the golden mapping, envelope and headers, escaping, the `<x/>` gate, contract-level validation, transport failures, and hostile or malformed responses.
- `tests/soap/test_live_mock.py` (3 tests): the real round trip against the mock, so the fixtures cannot drift.
- `pyproject.toml`: `types-defusedxml==0.7.0.20260504` (typeshed stubs) as a dev dependency, so `mypy --strict` checks our defusedxml calls instead of ignoring them.

**How it works:**
- **t-strings (PEP 750) make escaping structural.** `t"…{q.origin_zip}…"` is not a string; it's a `Template` of literal parts and `Interpolation` objects. `render_xml` escapes every interpolation and passes literal parts through untouched. There's no way to forget to escape, because the interpolated values never go through an f-string.
- **Validation happens before rendering.** `RateQuoteRequest` (pattern `^[0-9]{5}$`) rejects `<x/>` before any XML exists. The escaping is defence in depth, not the only line of defence.
- **Faults are classified by `faultcode`, not HTTP status.** SOAP 1.1 sends *every* fault as HTTP 500 (the two recorded fault fixtures prove it). `Server.Busy` becomes `RetryableError`; anything else (`Client`) becomes `UpstreamRejected`, because retrying a request the server called wrong just hammers it. (Refined in the PR #2 review: all `Server*` faults, other 5xx and 429 are retryable too.)
- **Transport failures are retryable:** connect errors, timeouts and 502/503/504. This is safe **only** because `GetRateQuote` has no side effects. A call that creates something retries only with an upstream idempotency token, or not at all.
- **defusedxml** refuses DTDs and entity declarations, so an XXE (`file:///etc/passwd`) or a billion-laughs response is rejected before expansion.
- **Lab addition 1:** the `QuoteInput` Protocol types `q` without importing the API layer, so the dependencies point inwards.
- **Lab addition 2:** `render_xml` also escapes quotes, so an interpolation is safe inside an attribute too.
- **Lab addition 3:** an unparseable or hostile response becomes `UpstreamRejected`. The spec's code would let `ParseError` / `EntitiesForbidden` escape as an unclassified 500.
- **Strict request model:** `RateQuoteRequest` is `strict=True`, so `"1200"` is not a number (the contract says `type: number`); `extra="forbid"` enforces `additionalProperties: false`. It uses `[0-9]`, never `\d`, because Python's `\d` accepts `٣٠٣٠١` (Arabic-Indic digits).

**Commands run, in order:**
1. `sed -n '/M4 — SOAP adapter/,/M5 — /p' spec/…` to read the spec. Then the M5 section, for `RetryableError`'s home, and the contract's `RateQuoteRequest` and `RateQuote` schemas.
2. Read `mocks/soap/app.py`: faults are HTTP 500 with an unqualified `<faultcode>`; `origin_zip=00000` is the mock's stable Client-fault trigger.
3. `uv run python tests/soap/record_fixtures.py` recorded 3 fixtures (441, 278 and 293 bytes). It set `busy_rate 1.0` for the Busy fixture, then restored the defaults (`/__stats` confirmed `busy_rate 0.0`).
4. `uv run pytest tests/soap -q` → **31 passed** at the first run.
5. `ruff` → 6 findings: 5 long lines, and **RUF043** (`match="Server.Busy"` has an unescaped `.`; now `r"Server\.Busy"`). `mypy` → `defusedxml` has no stubs; I added `types-defusedxml` rather than an `ignore`.
6. Three mutations of `client.py` (below). Each one was caught.
7. Added the live tests, then `uv run pytest tests/soap -q` → **34 passed**. `make check` → **115 passed**, ruff and mypy clean.

**Verification:** the Done-when gate.

```text
$ uv run pytest tests/soap -q
..................................                                       [100%]
34 passed in 0.38s
```

| Gate item | Test | Result |
|---|---|---|
| Recorded success → dict | `test_success_maps_to_dict` | ✅ `{QuoteRef, TotalCharge: 974.56, Currency: USD, TransitDays: 5}` |
| Recorded `Client` fault → `UpstreamRejected` | `test_client_fault_is_upstream_rejected` | ✅ `soapenv:Client: origin ZIP not served` |
| Recorded `Server.Busy` → `RetryableError` | `test_server_busy_fault_is_retryable` | ✅ |
| `<x/>` rejected by Pydantic before any XML | `test_bad_origin_zip_rejected_before_xml[<x/>]` | ✅ the transport fails the test if called; it never is |
| XXE / billion laughs | `test_hostile_or_malformed_responses_are_rejected[xxe, billion_laughs]` | ✅ `EntitiesForbidden` → `UpstreamRejected` |
| Live mock accepts our envelope | `test_live_success` | ✅ `/__stats` ok=1, client_faults=0 |

**Mutation check** (each one applied to `client.py`, run, then restored):

| Mutation | Caught by |
|---|---|
| `render_xml` stops escaping | `test_render_xml_escapes_every_interpolation` |
| Every fault treated as retryable | `test_client_fault_is_upstream_rejected` |
| `defusedxml` replaced by `xml.etree` | `test_hostile_…[xxe]` and `[billion_laughs]` |

**What broke and how we fixed it:**
1. *A test that could not fail (self-review, before the first run).* The first version of the `<x/>` test asserted that an empty `calls` list was empty; nothing ever appended to it. I replaced it with a transport that calls `pytest.fail` if an envelope is sent, and made the test follow the API's real sequence (validate, then call).
2. *RUF043:* `pytest.raises(match="Server.Busy")` is a regex, and `.` matches any character, so `ServerXBusy` would also have passed. It is now `r"Server\.Busy"`. A small bug, but exactly the kind that hides a wrong fault code. The mapping to 502 and 503 is M6's job; M4 only classifies.
3. *mypy `import-untyped` for defusedxml.* An `ignore` would have made every defusedxml call `Any` and hidden real type errors. I installed the typeshed stubs as a pinned dev dependency instead.

**Spec observations (not changed, flagged):**
- The spec's `get_rate_quote` doesn't handle a non-XML or hostile response. The lab maps it to `UpstreamRejected` (difference 10). Proposed spec fix: add `except (ParseError, DefusedXmlException)` around `ET.fromstring`.
- The spec's `render_xml` escapes element text only (difference 9). It's safe today, because only constants go into attributes, but it's fragile.

**Cloud vs real customer environment:** Meridian's real WSDL decides the element names, namespaces and `SOAPAction`, and we'd record the golden fixtures in their test window, not from our mock, with their written OK. The endpoint sits behind their WAF, possibly with mutual TLS or WS-Security. With WS-Security, the envelope needs a signed header, and a t-string template would still render the body. The 3 s read timeout is sized against their p99 of 2.8 s; if their p99 moves, it moves too, and ADR-P01-1's contract `Retry-After` follows. In production the fixture recorder is also how you capture "what did they actually send" during an incident, with PII scrubbed.

**Check yourself:**
1. With `render_xml`, what makes it *impossible* to forget escaping one value, and why wouldn't an f-string plus a helper you call on each value give the same guarantee?
2. Both faults arrive as HTTP 500. Why is `Server.Busy` retried but `Client` not, and what would retrying `Client` faults do to Meridian at peak?
3. We already validate `origin_zip` with a regex. Why still escape, and why still use defusedxml for responses from a service we "trust"?

<details><summary>answers</summary>

1. A t-string evaluates to a `Template` that keeps the literal parts and the interpolated values *separate*. `render_xml` sees every `Interpolation` as a distinct object and escapes it, so there's no code path where a value is concatenated raw. With an f-string, interpolation happens *before* your helper sees anything: the result is one flat string, and correctness depends on every author wrapping every value in `escape(...)` every time. One forgotten call is an injection. The template makes the safe way the only way.
2. `Server.Busy` is transient: the same request may succeed a moment later, and `GetRateQuote` has no side effects, so a retry is safe. A `Client` fault says the request itself is wrong (an unsupported ZIP, a bad weight), so every retry fails the same way and burns one of Meridian's 5 concurrent slots. At peak that turns one bad shipper request into several wasted calls, and it can push other shippers into `Server.Busy`. That's the retry storm ADR-P01-1 exists to prevent. `UpstreamRejected` ends it immediately and becomes a 502 with a clear message.
3. Defence in depth. The regex protects *this* field today; the escaping protects every future field someone adds to the template without thinking (a free-text reference, a company name with `&`). A "trusted" service can still be compromised, misconfigured, or sit behind a proxy that returns an HTML error page, and XXE and billion laughs turn a parser into a file reader or a memory bomb. The rule in section 9 is "defusedxml for every parse", because trust isn't a parser setting.
</details>

**What would break in production here (spec section 12):**
- **Meridian changes the fault code** (for example `soapenv:Server.Overloaded`, or a SOAP 1.2 `Receiver` code). It falls through to `UpstreamRejected`: no retries, and shippers get 502s at peak instead of a 503 with `Retry-After`. Mitigation: a contract test against their test endpoint on every release, and an alert on the rate of `UpstreamRejected` by fault code.
- **Their p99 creeps above 3 s.** Healthy but slow calls time out, get retried and add load. It needs latency SLO monitoring on the upstream, with the timeout reviewed jointly.
- **A WSDL namespace bump** (`v2` → `v3`) makes the result lookup return `None`, so every quote becomes `UpstreamRejected: response has no GetRateQuoteResult`. The golden fixtures make this a failing test the day you re-record, not a production incident.

---

## PR #2 review — three independent review agents   (2026-09-26, session 3)

**What happened:** before merging PR #2 (M3 + M4), three review agents read the diff in parallel, each with its own focus:
1. M3 correctness and concurrency;
2. security and M4 correctness;
3. spec fidelity, gate honesty and the docs.

Each ran its own reproductions (scratch databases, local test servers) and edited nothing. I re-verified every finding before fixing it, and each fix has a regression test. The critical fixes were also mutation-checked: reverting the fix makes its test fail.

**Findings and outcomes:**

| # | Finding (reviewer) | Severity | Reproduced? | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | The same `shipment_id` twice in one batch → `CardinalityViolation`, which isn't caught → the poller crash-loops on that file forever and blocks every later file (M3) | **blocker** | yes, both by me and by the reviewer | The last line per shipment wins inside a batch; the rest are counted `superseded` | `test_same_shipment_twice_in_one_batch_last_line_wins[identical, changed]`; mutation (no dedupe) → `CardinalityViolation` |
| 2 | A NUL byte in a line → `DataError` (text) or `UntranslatableCharacter` (jsonb) → the same crash loop; a binary file's *rejection* crashed too (M3) | **blocker** | yes, both by me and by the reviewer | NUL lines become dead letters; NULs in `raw` are escaped as `\x00`; a safety net rejects a file on any Postgres `DataError` and keeps going | `test_nul_bytes_are_dead_lettered_not_fatal`, `test_binary_file_is_rejected_not_fatal`, `test_database_refusal_rejects_the_file_and_keeps_earlier_batches` |
| 3 | `splitlines()` splits on `\x0b \x0c \x1c-\x1e` and a bare `\r` → a record gets split and every later `line_no` is wrong (M3) | should-fix | yes (by the reviewer) | Split on LF only, then strip CR; a `csv.Error` becomes a dead letter | `test_odd_characters_inside_a_field_do_not_split_the_line[x0c, x0b, x1c, r]`, `test_lf_only_files_parse_too` |
| 4 | Lenient parsing: `'12609 1'`, `'12609249'` and C=2..9 accepted as dates; `1e2` and `1_000` accepted as weights (M3, and spec code) | should-fix | yes | Strict `[01][0-9]{6}` and plain-digit checks (difference 12) | new `test_cyymmdd_rejects_garbage` and `test_bad_rows_become_dead_letters` cases |
| 5 | A file could move a shipment to another shipper (BOLA) (M3) | should-fix (raised as a question in the PR) | by reading | `FOR UPDATE` owner check; an owner change becomes a dead letter; `client_id` is never updated | `test_owner_change_is_refused` |
| 6 | The advisory lock sat on a separate idle connection: if only that one dies, a second poller can start (M3) | nit | by reading | The lock is taken on the working connection | the existing lock test, renamed `…_skips_its_cycle_while_locked` |
| 7 | `ingest_bytes` inside an open transaction silently loses the per-batch checkpoints (M3) | nit | by reading | A `RuntimeError` guard | `test_ingest_refuses_a_connection_inside_a_transaction` |
| 8 | A dropped connection (`RemoteProtocolError`, `ReadError`, `WriteError`) escaped unclassified → raw 500, invisible to M5 (M4) | should-fix | yes (by the reviewer) | Catch every `httpx.TransportError` → `RetryableError` | `test_dropped_connections_are_retryable[…]`; mutation → 3 failures |
| 9 | httpx's timeout is per read, not a total → a slow-drip response took **10.1 s** (M4) | should-fix | yes (by the reviewer) | A total `asyncio.timeout(3.0)` per call | `test_slow_drip_response_hits_the_total_deadline`; mutation → failure |
| 10 | A bogus `encoding=` declaration raised `LookupError`; an unqualified result child raised `IndexError` (M4) | should-fix | yes (by the reviewer) | Also catch `LookupError`/`ValueError`; use `rpartition` for tag names | `…[bogus_encoding]`, `test_unqualified_result_child_does_not_crash` |
| 11 | HTML 500s, 500s without a Fault, generic `Server` faults and 429 were `UpstreamRejected` → the M5 breaker would never open (M4) | should-fix | yes (by the reviewer) | Those are now `RetryableError`; only `Client` faults and other 4xx are rejected (difference 16) | `test_classification_the_breaker_can_rely_on[8 cases]` |
| 12 | `endswith("Server.Busy")` matched `evil:NotServer.Busy` and missed a qualified `<soapenv:faultcode>` (M4) | nit | yes (by the reviewer) | Compare the QName's local part; accept a qualified element too | `…[spoofed_busy, qualified_busy]` |
| 13 | No response-size cap (a 300 MB body was buffered) (M4) | nit | yes (by the reviewer) | Stream, and refuse more than 1 MiB | `test_oversize_response_is_refused` |
| 14 | `render_xml` dropped `!r` and format specs and passed XML-illegal characters; `str(1e-05)` isn't `xs:decimal` (M4) | nit | yes (by the reviewer) | `convert()` + `format()`, refuse illegal characters, `xsd_decimal()` | `test_render_xml_applies_conversion_and_format_spec`, `…_refuses_xml_illegal_characters`, `test_weights_render_as_plain_decimals` |
| 15 | Defence in depth: `forbid_dtd=True`, hermetic SSH (`config=[]`), filenames logged with `%r` (log injection) (M4/M3) | nit | n/a | Applied | the XXE and billion-laughs tests now fail at the DOCTYPE (`DTDForbidden`) |
| 16 | The e2e SFTP test wrote into the shared drop, so a running lab poller would ingest it (docs reviewer) | should-fix | yes (by me) | The test uses a private `_e2e/` subdirectory, which the lab poller never lists. **My first fix (skip if the lab lock is held) did not work:** the lock is held for milliseconds per cycle, so the check missed it. The subdirectory removes the race entirely. | e2e ran 3 times alongside `make poll-local`: lab `ingested_files` stayed at 2 |
| 17 | The live SOAP tests reset the mock to hard-coded defaults, clobbering a later gate's settings. Also found: the **M2** test file left `busy_rate 1.0` behind on every run (docs reviewer, plus me) | should-fix | yes (by me) | Both fixtures snapshot and restore the actual fault settings | full suite → mock back at `busy_rate 0.0`; a custom `0.3` survived the tests |
| 18 | Docs: undocumented deviations, a separate "M4 additions" table, FR-3 where FR-4 was meant, a 502 claim that is really M6's, the crash row not labelled as a test, gate counts depending on `make mocks`, a stale `.gitkeep`, and `record_fixtures.py` failing outside the repo root | should-fix / nit | yes | ARCHITECTURE differences 9–17 in one table; the other points corrected | n/a |

**Not changed (with reason):**
- **Status regression from an older file dropped late.** It needs a source timestamp or sequence number from the IBM i job, which is a data contract with Meridian. It stays a "Known limit" in M3.
- **`RETURNING (xmax = 0)`.** It's an implementation detail, but widely used; it's now commented and pinned by a test.
- **Hashing migration files as text.** `.gitattributes` forces `*.sql eol=lf`, so autocrlf can't change the checksum.
- **The upstream `faultstring` in `UpstreamRejected`'s message.** It must not reach shippers. That's M6's job when it builds Problem Details (noted for M6).
- **README's uv 0.12.19 vs the digest's 0.12.18.** This predates the PR and is recorded with evidence in "Session environment".

**Verification after the fixes:**
- `uv run pytest tests/soap -q` → **58 passed** (M4 gate; 3 of them need `make mocks`).
- `make check` → ruff clean, `mypy --strict` clean, **160 passed**. The 22 integration and live tests need the stack (`make mocks`); without it they skip.
- The M3 gate data is unchanged: 3 shipments from the sample, the same 2 dead-letter reasons.

**Lesson:** three reviewers with different focuses found two crash loops that my own tests missed. My crash test proved exactly-once under a *clean* failure, and nobody had tried *hostile input*. "Hostile input is reachable" is the question to ask of every parser. A second lesson came from my own fix: my first e2e guard looked right and was wrong, and only running it against a live poller showed that.

---

## M5 — Resilience stack   (2026-09-26, session 3)

**Goal / requirement served:** ADR-P01-1 (a global bulkhead of 4, a circuit breaker and bounded retries, with a fast `503` on saturation) and success criterion 2 ("no shipper behaviour, including aggressive retries, can push more than 4 concurrent requests onto the SOAP service"). FR-4's error contract: `503` + `Retry-After`, `502` for a Client fault. FR-8: RFC 9457 for every error.

**What we built:**
- `src/gateway/resilience.py`: the spec's `retry_full_jitter`, `State` and `CircuitBreaker` verbatim (line-wrapped), and `Bulkhead`, written from the spec's prose. Lab additions: `CircuitOpenError.retry_after`, `CircuitBreaker.retry_after()`, `Bulkhead.in_flight` and `retry_after_seconds()`.
- `src/gateway/api/rate_quotes.py`: `QuoteService`, composing bulkhead → retry → breaker → per-attempt timeout, and `POST /v1/rate-quotes`. The route validates the contract's `Idempotency-Key` header; storing it is M6's job.
- `src/gateway/errors.py`: `ProblemError` and one exception handler per outcome. `BulkheadFull` / `CircuitOpenError` / retries exhausted → `503` + `Retry-After`; `UpstreamRejected` → `502` with a generic detail; validation → `422` with an `errors[]` list; anything else → a bare `500`.
- `src/gateway/app.py`: `create_app()` with a lifespan that owns one pooled `httpx.AsyncClient` and the resilience objects.
- `src/gateway/config.py` and `.env.example`: `SOAP_BASE_URL`, `SOAP_MAX_CONCURRENCY=4`, `SOAP_BULKHEAD_WAIT_S=0.2`, `BREAKER_FAILURE_THRESHOLD=5`, `BREAKER_RESET_AFTER_S=30`.
- `load/quotes.js`: the k6 50-VU burst, with thresholds (every response is a 201, or a 503 carrying `Retry-After`; p95 of 503s under 500 ms).
- `tests/unit/test_resilience.py` (30 tests): every breaker state, including cancellation; jitter bounds; the deadline; the bulkhead; and the composed path, including the gate in miniature.
- `tests/unit/test_api_rate_quotes.py` (12 tests): the contract mapping for each outcome, and a check that no upstream internals leak.
- `Makefile`: `api-local`, `load-quotes`. README quick-start steps. ARCHITECTURE differences 18–21.

**How it works:**
- **Composition order, outside-in: bulkhead → retry → breaker → timeout.** Each layer protects the ones inside it.
  - The **bulkhead** is a `Semaphore(4)`. It limits how many calls reach Meridian, however many shippers are waiting and however many retries each makes. A request waits at most 0.2 s for a slot, then gets a `503` `upstream-saturated` with `Retry-After: 2`.
  - It is outermost so a request **keeps its slot for its retries**. If it sat inside the retry loop, a request could be refused halfway through, after already spending attempts.
  - The **retry** makes up to 3 attempts within a 4 s budget. (As merged after the PR #3 review, each attempt is also cut off at the budget. The spec's code only checked the budget before a sleep, so the worst case was about 6.2 s.) The wait before attempt *n* is drawn uniformly from `[0, min(2 s, 0.2 s × 2^n)]`: **full jitter**. It retries only `RetryableError`; a Client fault is never retried.
  - The **breaker** sits inside the retry loop. After 5 consecutive retryable failures it opens. (After the PR #3 review, results from calls admitted before it opened no longer change its state.) While open, calls fail in about 1 ms without touching Meridian, and `CircuitOpenError` isn't retryable, so it ends the retry loop at once. After 30 s it goes half-open and admits exactly **one** trial call: success closes it, failure reopens it for another 30 s.
  - The **timeout** is M4's total 3 s deadline per attempt.
- **Why jitter matters (the 2025-06-12 Google Cloud incident the spec cites; [incident report](https://status.cloud.google.com/incidents/ow5i3PPK96RduMcb1SsW), linked from `spec/P01-P04-full-projects-file.md` Sources):** a policy change with blank fields crashed Google's Service Control binaries worldwide. When they *restarted* in the large us-central1 region, the tasks re-read their data from the Spanner table they depend on together, a "herd effect", because Service Control did not have randomized exponential backoff. That synchronized "herd" overloaded Spanner and made that region's recovery take far longer than the others. With plain exponential backoff, clients that failed together retry together (0.2 s, 0.4 s, 0.8 s…) and hit the recovering service in waves. Full jitter spreads each wave evenly across the window, so a recovering dependency sees a trickle instead of a stampede.
- **The breaker needs no lock:** no `await` separates a state check from the change it makes, so on one event loop nothing can interleave there. With threads, or free-threaded Python, it would need one.
- **`except BaseException` in the breaker matters.** When a shipper disconnects, the half-open trial's task is *cancelled*, and `CancelledError` is not an `Exception`. Without that branch, `_trial` would stay `True` and every later call would get "trial in flight" forever. The breaker would be wedged half-open until a restart.
- **Retry-After is honest:** `2` s for saturation and for retries exhausted (ADR-P01-1). For an open circuit it is the time until the trial, rounded *up* to whole seconds (RFC 9110 delay-seconds; never `0`).
- **Upstream internals stay internal:** Meridian's `faultstring` is logged, never returned. A `502` says "check ZIPs, weight, service level", not `CPF4131 member QTEMP/RQ locked`.

**Commands run, in order:**
1. Read the spec's M5 section, the contract's `Problem` schema, the `502`/`503` responses and the `Idempotency-Key` parameter (min 16, max 128, `^[\x21-\x7E]+$`).
2. Wrote `resilience.py`, `errors.py`, `api/rate_quotes.py`, `app.py` and the config. `ruff` + `mypy --strict` → clean after wrapping 4 long lines.
3. **The VM restarted** mid-milestone (`uptime` 2 min, `docker` unreachable, the live tests suddenly skipping). The disk survived: `secrets/`, `.env` and Docker's volumes were all intact. I re-ran `bash scripts/cloud-setup.sh` (`INFO: docker daemon not running; starting dockerd`) and `docker compose up -d` → 4 healthy. The host key still matched the pin, because it lives on the `sftp-hostkeys` volume. `make migrate-local` → none pending.
4. `pytest tests/unit/test_resilience.py` **hung** (see "What broke" 1). I stopped it, fixed the fixture, and got 30 passed in 1.2 s.
5. `pytest tests/unit/test_api_rate_quotes.py` → 12 passed.
6. The live gate, part 1 (breaker), then part 2 (k6). Below.
7. Mutation checks on `resilience.py` (below). `make check` → **202 passed**, ruff and mypy clean.

**Verification:** the Done-when gate, against the live API (`make api-local`) and the SOAP mock.

*Part 1: with the mock at `{"busy_rate": 1.0}`, the breaker opens after 5 failed attempts (on the 2nd call), and every later call is a `503` in under 10 ms:*

```text
healthy:  201 0.332s  total_charge=974.56 transit_days=5
call 1:   503 1.196s  type=upstream-busy  Retry-After: 2    (3 attempts)
call 2:   503 1.000s  type=circuit-open   Retry-After: 30   (attempts 4, 5 -> breaker opens)
call 3:   503 0.0017s type=circuit-open   Retry-After: 30
mock /__stats after 3 calls:  {"calls":5,"busy_faults":5}
20 more calls while open:     all 503, latency min 1.0 ms, median 1.3 ms, max 1.9 ms
mock /__stats after all:      {"calls":5, ...}          <- not one more call reached Meridian
```

The commands behind Part 1 (API running via `make api-local`):

```bash
curl -s -X POST localhost:8080/__faults -H 'Content-Type: application/json' -d '{"busy_rate":1.0}'
curl -s -X POST localhost:8080/__reset
for n in 1 2 3; do curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' -X POST localhost:8000/v1/rate-quotes -H 'Content-Type: application/json' -H "Idempotency-Key: gate-$n-aaaaaaaaaaaa" -d '{"origin_zip":"30301","dest_zip":"60601","weight_lb":1200,"service_level":"LTL_STANDARD"}'; done
curl -s localhost:8080/__stats
```

*Part 2: a 50-VU k6 burst for 20 s against a healthy mock (300 ms latency). What Meridian saw:*

```text
mock /__stats: {"calls":264,"ok":264,"busy_faults":0,"peak_concurrency":4}
k6: 4,609 requests = 264 x 201 + 4,345 x 503 (every one with Retry-After)
    (in this first run the split was derived from the mock's ok=264; since the PR #3 review, the
     script's http_reqs{status:201|503} thresholds print it: count=264 and count=4345 on the re-run)
    checks 100% (9,218/9,218); thresholds passed
    503 latency p95 205 ms (the 0.2 s bulkhead wait); 201 latency median 476 ms
```

| Gate item | Target | Measured |
|---|---|---|
| Breaker opens after 5 failed attempts | on the 2nd call | ✅ on the 2nd call; the mock saw exactly 5 calls |
| Later calls return 503 | under 10 ms | ✅ median 1.3 ms, max 1.9 ms (20 calls) |
| Peak concurrency at the mock, 50-VU burst | ≤ 4 | ✅ **4** |
| Every 503 carries `Retry-After` | 100% | ✅ 4,345/4,345 |

Throughput sanity check: 264 successes in 20 s is 13.2/s, and the theoretical ceiling is 4 slots ÷ 0.3 s = 13.3/s. The bulkhead is *fully used and never exceeded*. The other 50 − 4 = 46 VUs are shed in about 200 ms each.

**Mutation check** (each applied to `resilience.py`, run, then restored):

| Mutation | Result |
|---|---|
| Don't reset `_trial` on `BaseException` | ❌ caught by `test_cancelled_trial_does_not_wedge_half_open` |
| Plain exponential backoff (no jitter) | ❌ caught by `test_retry_recovers_from_transient_failures` |
| Drop `state is HALF_OPEN or` from the reopen condition | ✅ survived. This is an **equivalent mutant**: the spec's breaker resets `failures` only on success, so a failed trial always has `failures ≥ threshold` and reopens by count anyway. The clause is defence in depth (it matters if someone resets the count on open); no test can tell them apart, so none pretends to. |

**What broke and how we fixed it:**
1. *The unit tests hung.*
   - *Symptom:* `pytest tests/unit/test_resilience.py` never finished (a 2-minute timeout, moved to the background, stopped with `TaskStop`).
   - *Hypothesis:* a fake clock that stops time also stops asyncio.
   - *Evidence:* the fixture did `monkeypatch.setattr(resilience.time, "monotonic", clock)`. `resilience.time` *is* the global `time` module, and asyncio's event loop reads `time.monotonic()` for every timer.
   - *Root cause:* freezing the global clock froze every `asyncio.sleep` and `wait_for` in the process.
   - *Fix:* replace only the *name* `time` inside `gateway.resilience` (`SimpleNamespace(monotonic=clock)`). The spec's code is unchanged, the loop keeps real time, and the run went from hanging to 30 passed in 1.2 s.
2. *The live tests suddenly skipped.*
   - *Symptom:* "55 passed, 3 skipped" instead of 58.
   - *Evidence:* `docker compose ps` → "Cannot connect to the Docker daemon", and `uptime` 2 min.
   - *Root cause:* the VM restarted; the processes were gone and the disk was intact.
   - *Fix:* the session-start restore (setup script, compose up, host-key check, migrate).
   - *Lesson:* a skipped test is not a passing test. The gate numbers need `make mocks`, which is why the verification sections say so.
3. *An API that looked still up after a kill.*
   - *Symptom:* killing the `make api-local` PID printed "api still up" 2 s later.
   - *Hypothesis:* the uvicorn child survived its parent, so a stale API (breaker still open) would answer k6.
   - *Evidence:* `ps` showed only the *new* API's process tree (8 s old), answering 201s.
   - *Root cause:* shutdown simply took longer than 2 s.
   - *Fix:* none needed, but the final stop now kills make, uv and uvicorn by their PIDs and checks port 8000 is closed. Checking before trusting the k6 numbers is the point.
4. *A claim that was stronger than the truth (self-review).* The first docstring said the bulkhead must be outermost "so at most 4 calls reach Meridian". A bulkhead inside the retry loop would also cap at 4. The real reason is that a request keeps its slot for its retries and can't be refused halfway through. I fixed the docstring, and renamed the test to what it proves.

**Cloud vs real customer environment:** in the lab, one API process holds the only bulkhead. At Meridian there would be 2+ replicas, and a per-process `Semaphore(4)` would allow 4 *per replica*: 8 with two pods, breaking ADR-P01-1. The options are:
- size each replica's bulkhead as floor(4 ÷ replicas) and pin the replica count;
- put a shared limiter in front, for example Postgres advisory locks as tokens, or the API gateway's concurrency limit;
- route all SOAP calls through one "SOAP broker" instance.

The breaker is per process too, so each replica learns separately that Meridian is down. That's acceptable, and it's why the threshold is small. The 300 ms mock latency is optimistic: Meridian's p99 is 2.8 s, which cuts the ceiling to about 1.4 quotes/s. The 503 rate at peak is therefore a *capacity* conversation with Meridian and the shippers, and the contract already tells shippers to honour `Retry-After`.

**Check yourself:**
1. Why is the breaker *inside* the retry loop and the bulkhead *outside* it? What goes wrong with each the other way round?
2. 1,000 shippers' requests fail at the same instant and all retry with plain exponential backoff (0.2 s, 0.4 s, 0.8 s). Describe the load Meridian sees for the next 2 s, then the same with full jitter.
3. A shipper cancels a request while it is the breaker's half-open trial. What would happen without `except BaseException: self._trial = False`, and why wouldn't `except Exception` be enough?

<details><summary>answers</summary>

1. **The breaker inside the retry loop:** each attempt passes through it, so the attempt that opens it is followed by an attempt that gets `CircuitOpenError`. That error isn't retryable, so the request stops at once. With the breaker *outside*, one request's retries would all run before the breaker saw a single outcome, and an open breaker could not stop retries already in progress. **The bulkhead outside the retry loop:** a request takes one slot and keeps it for all its attempts, so the cap of 4 holds and a request is never refused halfway through. With the bulkhead *inside*, each attempt re-competes for a slot. A request that already made two attempts could lose the third to a newcomer and return a 503 after doing real work, and fairness gets worse under load.
2. **Plain exponential:** Meridian sees three synchronized spikes of about 1,000 calls each, at t = 0.2, 0.6 and 1.4 s, with silence between them. Each spike blows far past its 5-concurrent limit, so almost every call fails with `Server.Busy`, and the herd reforms on the next step. That's the 2025-06-12 Spanner overload pattern. **Full jitter:** each retry lands uniformly within `[0, 0.2]`, `[0, 0.4]`, `[0, 0.8]`, so the same 1,000 retries arrive as a smooth, spread-out flow. Meridian can recover, and the breaker and bulkhead see honest signals. (Our bulkhead would cap it at 4 anyway; jitter is what stops *uncoordinated* clients, like the shippers themselves, from stampeding.)
3. The trial's task gets `CancelledError`, which is a `BaseException`, not an `Exception`. Without that branch, `_trial` stays `True` forever. The breaker is `HALF_OPEN`, and every later call hits "half-open trial in flight" and gets a 503 **indefinitely**, even after Meridian recovers, until the process restarts. `except Exception` would miss `CancelledError` (and `KeyboardInterrupt`) for exactly that reason. Resetting the flag and re-raising is what makes cancellation safe.
</details>

**What would break in production here (spec section 12):**
- **More than one replica.** Each replica has its own `Semaphore(4)`, so two pods can send 8 concurrent calls, and Meridian starts returning `Server.Busy` to *everyone*, including its own callers. This is the most likely way to break ADR-P01-1 in production; pin the replica count or share the limiter.
- **A breaker threshold tuned for the lab.** 5 failures and 30 s suit a mock. Against real traffic with an intermittent 10% busy rate, a stretch of bad luck can open the breaker, which turns a 10% error rate into 100% for 30 s. Watch `breaker state changes` (section 8) and tune from real data.
- **Shippers who ignore `Retry-After`.** They hammer the gateway, not Meridian (the bulkhead holds), but they burn our CPU and each other's slots. The per-key `429` rate limit in the contract is the next defence.

---

## PR #3 review — three independent review agents   (2026-09-26, session 3)

**What happened:** before merging PR #3 (M5), three review agents read the diff in parallel, each with its own focus:
1. resilience correctness and concurrency;
2. API, errors and security;
3. spec fidelity, gate honesty and the docs.

Two of them independently re-ran the live gate, on their own ports (8765 and 8766, so the lab API on 8000 was untouched), and **the claims reproduced**:
- breaker open on the 2nd call, with the mock at 5 calls;
- circuit-open 503s at 1.1–2.6 ms;
- a 10 s k6 burst with the mock's `peak_concurrency` at 4.

I re-verified each finding (reproducing the most important one myself), fixed it with a regression test, and mutation-checked the fixes: every new test fails against the old code.

**Findings and outcomes:**

| # | Finding | Severity | Reproduced? | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | **The breaker accepted stale results.** A call admitted while CLOSED that succeeded *after* the breaker opened closed it at once (`OPEN 5` → `CLOSED 0`), skipping the 30 s cool-down and the single half-open trial. Stale failures pushed `opened_at` back. Any finishing call could clear the trial flag, admitting a second trial. With `busy_rate` 0.8 and 4 calls in flight, the first case happens about half the time. | should-fix (spec code) | yes, by me and by the reviewer | A generation counter (bumped on every open; stale results don't move the state) and trial ownership. Difference 22. | `test_late_success_from_before_the_open_does_not_close_it`, `test_late_failures_do_not_extend_the_open_window`, `test_a_stale_call_cannot_clear_the_trial_flag`; the spec breaker → 3 failures |
| 2 | **The 4 s deadline didn't limit how long an attempt ran:** the worst case was about 6.2 s holding a bulkhead slot (a 4:1 scaled repro gave 1.5 s against a 1.0 s budget) | should-fix (spec code) | yes (by the reviewer) | `asyncio.timeout_at(stop_at)` around each attempt → `RetryableError` (not a bare `TimeoutError`, which would become a 500). Difference 23. | `test_the_deadline_also_bounds_a_running_attempt`; mutant → hangs 10 s and fails |
| 3 | Upstream values reached a 201 unvalidated: `"1e3"`, `"usd"`, `"<script>"`, `-4`, a 23-digit day count, `"1_0"` | should-fix | yes (by the reviewer) | Strict patterns → `502 upstream-invalid-response`, which says it is *not* the shipper's fault; money normalised to 2 decimals. Difference 24. | `test_unusable_upstream_data_never_becomes_a_201[10 cases]`; mutant → 11 failures |
| 4 | No body limit: a 10.5 MB body cost 2.7 s of CPU and returned a **22 MB** 422 | should-fix | yes (by the reviewer) | `RequestGuard`: 413 over 64 KiB (chunked too); `errors[]` capped at 20. Difference 25. | `test_oversize_body_is_413_*`, `test_validation_errors_are_capped_and_pointer_escaped` |
| 5 | 404 and 405 were plain `{"detail":…}`; the contract's 415 was never returned (a text/plain body gave a 422) | should-fix | yes (by the reviewer) | A Starlette `HTTPException` handler (keeps `Allow`), and 415 in `RequestGuard` | `test_routing_errors_are_problem_details`, `test_non_json_bodies_are_415`; mutant (no guard) → 4 failures |
| 6 | Malformed JSON gave 422 with `/body/14` (a character offset); pointers weren't RFC 6901-escaped | nit | yes | 400 `malformed-json`; `~0`/`~1` escaping | `test_malformed_json_is_400`, the pointer test |
| 7 | No `instance` on 500s; `server: uvicorn`; `/openapi.json` served FastAPI's generated schema, which differs from the contract | nit | yes | `urn:uuid:` instance logged with the traceback; `--no-server-header`; `openapi_url=None`. Difference 26. | `test_500_carries_an_instance_for_support`, `test_no_generated_openapi_is_published` |
| 8 | k6 could pass vacuously (zero 503s → `p(95)=0s` ✓); the `Retry-After` check accepted any value; the 201/503 split wasn't printed | nit | yes (by the reviewer) | `http_reqs{status:201\|503}` `count>0` thresholds (they also print the split); a `^[1-9][0-9]*$` check; `--no-usage-report` (k6 was calling `stats.grafana.org`, which is blocked) | re-run: `count=264`, `count=4345` |
| 9 | Tests: `< 0.01 s` wall-clock asserts **flaked 1 run in 6** under load; `test_jitter_really_is_random` never called our code | should-fix | yes (by the reviewer) | The wall-clock asserts were removed (the call counts prove fail-fast; the live gate measures the ms); the jitter test now runs `retry_full_jitter` 200 times and checks the delays are distinct and spread | `test_retry_delays_are_spread_not_synchronised` |
| 10 | Unmarked spec edits (the `CircuitOpenError` arguments), and a docstring claiming "verbatim"; no row for Retry-After `1` | should-fix | yes | `lab:` markers; docstring and difference 21 updated | n/a |
| 11 | BUILD_LOG: the "4 s budget" claim overstated; no literal gate commands; the k6 split presented as k6 output; the Google incident with no link and "retried" instead of "restarted"; the README didn't reset stats before the burst | should-fix / nit | yes | Corrected in the M5 section and README | n/a |
| 12 | `attempts=0` raised `AssertionError('unreachable')` | nit | yes | `ValueError` | `test_attempts_must_be_positive` |

**Checked and fine (no change):**
- **The bulkhead's `wait_for(sem.acquire())`** doesn't leak permits on Python 3.14.7. The reviewer ran 300 rounds with 1,151 cancels at the wait boundary, and a deterministic same-tick test.
- **Outer cancellation** always releases the slot.
- **The composition order** is exactly bulkhead → retry → breaker → timeout.
- **The catch-all 500 handler** works under real uvicorn and leaks nothing.
- **The "equivalent mutant" claim** in M5 was correct. With difference 22 it no longer applies, since the trial now reopens by ownership.

**Not changed (with reason):**
- **A header validation error returns 422, not 400.** Both are documented statuses, and the contract's 422 covers "validation". This will be revisited with Schemathesis in M8.
- **An `UpstreamRejected` during the half-open trial leaves the breaker half-open,** so the next caller becomes the trial. That's harmless and intended: a Client fault says nothing about upstream health.
- **The per-replica bulkhead** stays per process. M8 pins a single replica (see M5 "Cloud vs real").

**Verification after the fixes:**
- **`make check`:** ruff and `mypy --strict` clean, **227 passed**.
- **Live gate re-run on the fixed code:**
  - breaker open on the 2nd call, with the mock at 5 calls;
  - 20 calls while open: median **1.2 ms**, max 1.5 ms;
  - 404 → `application/problem+json`, and no `server` header;
  - k6 50 VUs for 20 s → **264 × 201, 4,345 × 503**, all thresholds passed, mock `peak_concurrency` **4**.

**Lesson:**
- **The live gate couldn't see the stale-result bug.** It runs at `busy_rate` 1.0, where every call fails, so a late *success* never happens. Concurrency bugs hide in the mixed cases, which is where a reviewer's small `asyncio` script beats a load test.
- **A unit test that measures wall-clock time is a flaky test in waiting.** Assert behaviour (call counts) in unit tests, and measure time in the live gate.

---

## M6 — Idempotency keys   (2026-09-26, session 3)

**Goal / requirement served:** FR-4 ("`POST /v1/rate-quotes` requires `Idempotency-Key` … stores the quote for 15 minutes … `GET /v1/rate-quotes/{quote_id}` returns it") and ADR-P01-2 (idempotency state in Postgres, keyed by `(client_id, key)` with a request hash). It also covers the section 9 rows *Spoofing* (keys stored as SHA-256 hashes, scoped per shipper, a separate `ops` scope) and *Information disclosure* (BOLA: another shipper's quote is `404`).

**What we built:**
- `migrations/0002_auth_idempotency_quotes.sql`:
  - `api_keys`: a hash, `client_id`, a scope, and a `revoked_at` column;
  - the spec's `idempotency_keys`, verbatim;
  - `rate_quotes`: the exact 201 body, and `expires_at` 15 minutes out.
- `src/gateway/auth.py`:
  - the `principal` dependency: `X-API-Key` → SHA-256 → an active key → `Principal(client_id, scope)`, or a `401`;
  - `ops_principal` (→ `403`), ready for M7's dead-letter endpoints;
  - `python -m gateway.auth add`, which takes the key from `$API_KEY` (never argv, which ends up in `ps` and shell history) or generates one.
- `src/gateway/idempotency.py`: the spec's `begin()` verbatim, plus `request_hash()` (SHA-256 of canonical JSON), `complete()` and `release()`.
- `src/gateway/api/rate_quotes.py`: the route rewired as auth → hash → `begin` → (replay | call Meridian → store the quote and complete the key in one transaction). Also `GET /v1/rate-quotes/{quote_id}` and `CanonicalJSON`.
- `src/gateway/app.py`: the lifespan now also opens the Postgres pool (`DB_POOL_SIZE=10`).
- `load/idempotency_burst.py`: the M6 gate. 20 concurrent clients share one key, and each honours `409 Retry-After`.
- `Makefile` `dev-keys`; `.env.example`; README steps (the curl smoke test now sends `X-API-Key`).
- `tests/integration/test_api_rate_quotes.py`, moved from `tests/unit/` because the API now needs Postgres. It runs against a throwaway, migrated `gateway_api_test` database: 36 M5 tests plus 13 M6 tests.

**How it works:**
- **A key is a small state machine:** (absent) → `in_progress` → `completed`. The first request to `INSERT` the key owns it and calls Meridian. A duplicate that conflicts reads the row:
  - **a different body** (hash mismatch) → `422 idempotency-key-reused`: the key is bound to what it was first used for;
  - **still `in_progress`** → `409 request-in-progress` + `Retry-After: 2`;
  - **`completed`** → the stored response, replayed with `Idempotent-Replayed: true`.
- **The request hash is taken over the *validated* body, in canonical form** (sorted keys, compact). `{"weight_lb":1200}` and `{"weight_lb":1200.0}` in any key order are the same request; any real difference isn't.
- **The claim is committed before Meridian is called,** so concurrent duplicates can see it. Meridian is then called **without holding a database connection**: a 4 s upstream call mustn't pin one of the pool's 10 connections. The quote and the key's completion are stored in **one transaction**, so a replay can never point at a quote that doesn't exist.
- **`locked_until` is a 30 s lease.** If the owner dies mid-flight (OOM kill, deploy), the key would stay `in_progress` and every retry would get `409` forever. The spec's `DO UPDATE … WHERE locked_until < now()` lets a retry *take over* an abandoned key. (As first merged, `complete()` checked only `status = 'in_progress'`, which does **not** stop a slow original owner overwriting the new owner's result. The PR #4 review caught this, and the fix is a fencing token; see that section.)
- **A failed call releases the key** (difference 28). The contract promises that a `503` stored nothing, so the shipper's retry with the *same* key must reach Meridian again. Replaying a transient `503` for 24 h would be wrong. A *cancelled* request (the client went away) keeps its lease instead, and the lease expires.
- **Why the `DELETE` in `release()` can't race `begin()`'s `SELECT`:** Postgres locks the conflicting row during `INSERT … ON CONFLICT DO UPDATE` *even when the `WHERE` is false*. The duplicate's `INSERT` and `SELECT` share one transaction (the API pool isn't autocommit), so a concurrent `DELETE` waits for that transaction to end.
- **Auth runs before body validation,** so an anonymous caller gets `401`, not a `422` that describes our schema. The database only ever sees `sha256(key)`. Rotation with overlap means two active rows for one `client_id`; the old one is then revoked with `revoked_at`.
- **BOLA:** `GET` filters on `client_id` *and* `expires_at`. Another shipper's quote, an expired quote and a random UUID all produce the *same* `404` body, so a response never reveals that an ID exists.

**Commands run, in order:**
1. **The VM restarted again** (`uptime` 0 min, "Cannot connect to the Docker daemon"). I restored it: `bash scripts/cloud-setup.sh` → `docker compose up -d …` (4 healthy) → the host key still matched the pin → `make migrate-local` (none pending).
2. Read the spec's M6 section, section 9 (API keys as SHA-256 hashes, the `ops` scope), and the contract's `apiKey` scheme, `401`/`403`/`404`, `409` and the `Idempotency-Key` parameter.
3. Wrote the migration, `auth.py`, `idempotency.py`, the route and the lifespan pool. `make migrate-local` → `applied 1 migration(s): 0002_auth_idempotency_quotes`.
4. `make dev-keys` → `added shipper key for ACME: (from $API_KEY)`, `added ops key for meridian-ops`. `api_keys` holds only 32-byte hashes.
5. `make api-local`, then `uv run python load/idempotency_burst.py`. The **first run failed** (see "What broke" 1). After the fix it passed (below).
6. Moved the API tests to `tests/integration/` on a real database, then added 13 M6 tests. 2 old cases failed with `401 == 422`: the auth ordering working as designed, and they now send a key. → 49 passed.
7. Mutation checks (below). `make check` → **240 passed**, ruff and mypy clean. Stopped the API by PID and confirmed port 8000 was closed.

**Verification:** the Done-when gate, with the API running (`make api-local`) against the SOAP mock (300 ms latency):

```text
$ uv run python load/idempotency_burst.py
idempotency key:        burst-fe71d1dc-6f6a-4823-91fd-f4c786cba870
responses seen:         {'409': 19, '201': 1, '201 replayed': 19}
final status per client: {201: 20}
distinct final bodies:  1
quote_id:               9ca698b5-ae76-4956-afb9-8460e0b713cf
mock /__stats:          {'calls': 1}
GATE: PASS
```

The spec's "expected: `{"calls": 1}`" holds. All 20 clients end with the same body, and 19 of them got there "after `409` retries" and a replay, exactly as the gate describes.

**Mutation check** (each applied, run, then restored):

| Mutation | Caught by |
|---|---|
| Don't `release()` the key when the upstream call fails | `test_a_failed_call_releases_the_key_for_a_retry` |
| Plain `JSONResponse` instead of `CanonicalJSON` | `test_20_concurrent_duplicates_make_one_upstream_call`, `test_get_returns_the_stored_quote_to_its_owner_only` |
| `GET` without `client_id` in the `WHERE` (BOLA) | `test_get_returns_the_stored_quote_to_its_owner_only` |
| `begin()` without the `locked_until < now()` lease check | `test_a_crashed_owner_is_taken_over_after_its_lease[live]` and the 20-way test |

**What broke and how we fixed it:**
1. *The gate failed with "distinct final bodies: 2".*
   - *Symptom:* `calls: 1`, 19 × 409, then 19 replays, but two different bodies.
   - *Hypothesis:* the replay isn't byte-identical to the original.
   - *Evidence:* two `curl`s with one key returned the same fields in a different order. The original came from a Python dict; the replay came from `jsonb`, which stores object keys in its own order.
   - *Root cause:* `jsonb` normalises key order; the data was identical but the bytes weren't.
   - *Fix:* `CanonicalJSON` (sorted keys, compact) for the first response, the replay and the `GET`. The re-run passed.
   - *Lesson:* "identical" in a gate means bytes, because clients compare bytes, hash them and cache them.
2. *My edit script aborted half-way,* because ruff had reformatted the block I was matching. The next gate run silently tested the *unchanged* code and failed the same way. My `rep()` helper asserts that each pattern exists, which caught it. The lesson is to read the code again after an automated reformat, before editing it.
3. *`401 == 422` in two old tests.* The tests were stale, not the code: they sent no `X-API-Key`, and since M6 auth runs first. They now send a key, so they test what their names say.
4. *The VM restarted again* (the second time in this session). The restore is routine now: setup script → compose up → pin check → migrate. It took about 1 minute; the disk survived.

**Known limits:**
- **Retention.** The contract says keys are "Retained for 24 hours", but nothing purges them yet. An old completed key is replayed forever, and quotes past `expires_at` stay in the table (they're invisible, since `GET` filters on it). A periodic `DELETE … WHERE created_at < now() - interval '24 hours'` is M9 ops work.
- **The request hash covers the body only,** not the method or path. That's fine while one endpoint uses keys; a second one needs the route in the hash, or keys scoped per route.

**Cloud vs real customer environment:** at Meridian:
- Shipper API keys are issued through an onboarding process and delivered out of band. Rotation is a runbook: add the new key, the shipper switches, revoke the old one.
- The idempotency and quote tables sit on the managed Postgres (RDS or Aurora), and the 30 s lease is tuned against the p99 upstream latency of 2.8 s plus retries.
- With several gateway replicas, the Postgres claim is exactly what keeps idempotency correct across them. An in-memory cache couldn't (ADR-P01-2). It doesn't fix the per-replica *bulkhead* (M5).
- A shipper's own retry policy has to honour `409 Retry-After`. The contract documents that, and the burst script shows a well-behaved client.

**Check yourself:**
1. Two requests with the same key arrive 5 ms apart. Walk through what each one's `INSERT … ON CONFLICT` does, and what each shipper receives.
2. Why does a failed upstream call *delete* the key, while a *cancelled* request leaves it until the lease expires? What would go wrong in each case if we swapped them?
3. The same shipper sends the same key with `weight_lb: 1200` and then `weight_lb: 1300`. What does it get, and why is that safer than returning the first quote?

<details><summary>answers</summary>

1. The first `INSERT` creates the row as `in_progress` and returns it, so that request owns the key; it commits and calls Meridian. The second `INSERT` hits the primary key and runs the `DO UPDATE`. Its `WHERE` is false (the lease is still live), so no row is returned. It then `SELECT`s the row: same hash, still `in_progress` → `409` with `Retry-After: 2`. The first shipper gets `201` with the quote. The second retries after 2 s, finds `completed`, and gets the *same* `201` replayed with `Idempotent-Replayed: true`. Meridian is called once.
2. A failed call is a *known outcome*: nothing was stored, and the contract says a retry with the same key is safe. Deleting the key makes that retry a fresh attempt. If we kept it `in_progress`, the retry would get `409` for 30 s; if we stored the `503`, it would be replayed for 24 h. A *cancelled* request is an *unknown* outcome: the task was cancelled while Meridian may already be quoting. Deleting the key then would let a retry start a second upstream call alongside the first. Keeping the lease means the retry gets `409` until either the original completes or the lease runs out. In this cancelled case, the lease is what stops two concurrent calls.
3. `422 idempotency-key-reused`. The key is bound to the hash of its first body. Returning the first quote (1,200 lb) for a 1,300 lb request would silently give the shipper a wrong price that looks valid. The `422` tells them that their client reuses keys across different requests, which is a bug on their side.
</details>

**What would break in production here (spec section 12):**
- **A key-generation bug in a shipper's client** (the same key for every request): every quote after the first becomes a `422`, or worse, if their bodies match, a *replay of a stale quote* for up to 24 h. That's the client's bug, but it looks like ours. Log and alert on the replay rate per client.
- **Postgres slow or down:** no quotes at all, even though Meridian is healthy, because we can't claim keys. That's the right trade-off for correctness. `/readyz` (M8) must reflect it, and a `503` beats a duplicate booking-style side effect.
- **A lease shorter than the real upstream time:** if Meridian's p99 rose above 30 s (it's 2.8 s), a retry could take over a key whose owner is still working, and Meridian would be called twice. Since the PR #4 review, the fencing token means only the new owner's result is stored, and the stale owner can't delete the new claim. The extra call has still happened. Keep the lease above the worst-case request time (bulkhead wait + retry budget).

---

## PR #4 review — three independent review agents   (2026-09-26, session 3)

**What happened:** before merging PR #4 (M6), three review agents read the diff in parallel, each with its own focus:
1. idempotency correctness and concurrency, against its own scratch database;
2. auth, security and contract;
3. spec fidelity, gate honesty and the docs.

The gate reviewer re-ran the M6 gate **3 out of 3 times: all PASS** (`calls: 1`, 1 distinct body), and `make check` reproduced 240. The idempotency reviewer confirmed the hard parts:
- **Row lock:** the `ON CONFLICT` row lock is real, and holds for the length of the transaction.
- **No race:** a 20-worker × 300-cycle stress run on one hot key gave zero errors (`{'409': 5699, 'own': 301}`).
- **One owner:** 10 concurrent takeovers of an expired lease produced exactly one owner.
- **Byte-identical round trips:** odd floats come back from `jsonb` byte-identical.

The security reviewer confirmed tenant isolation: there was no cross-shipper replay, the 404s were identical, and revocation worked.

**Findings and outcomes:**

| # | Finding | Severity | Reproduced? | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | **The M5 gate broke:** `load/quotes.js` sent no `X-API-Key`, so since M6 every k6 request was a `401` | should-fix (a regression I introduced) | yes (by the reviewer) | The script sends `API_KEY` (default: the dev key) | the k6 re-run below: 264 × 201, peak 4 |
| 2 | **No fencing:** an owner whose lease expired could still `complete()`, and first writer wins (not "the original can't overwrite", as I had written). Its `release()` could delete the new owner's claim, so a third request could call Meridian *concurrently* | should-fix | yes (by the reviewer, with scripts) | `begin` returns the lease's `locked_until` as a token (`Owned`); `complete` and `release` require it. Difference 31. | `test_a_stale_owner_cannot_complete_after_a_takeover`, `…_cannot_release_the_new_owners_claim`; each fencing mutant fails its test |
| 3 | **Circuit-open latency regression,** found by re-running M5 after the fixes: median 7.1 ms, max 10.9 ms, against M5's "under 10 ms" (was 1.2 ms), because M6 put auth + claim + release in front of the breaker | should-fix | yes (by me) | A read-only fast path when the breaker would refuse (`peek`: replay / 409 / 422, else fail fast with no claim), plus a 10 s cache of *valid* API keys. Difference 32. | `test_circuit_open_fast_path_claims_nothing_but_still_replays`, `test_revocation_takes_effect_within_the_cache_ttl`; live: **300 calls, median 2.5 ms, max 6.0 ms, 0 at or above 10 ms** |
| 4 | `gateway.auth add` *resurrected* a revoked key, even as another tenant's `ops` key; an empty `$API_KEY` registered a random key it never showed | should-fix | yes (by the reviewer) | `ON CONFLICT DO NOTHING` with a non-zero exit; an empty key is refused; keys under 32 characters need `--allow-weak` (dev only). Difference 34. | `test_registering_an_existing_key_never_resurrects_or_moves_it`; `make dev-keys` now prints "already registered" |
| 5 | An `ops` key could create quotes as a phantom shipper | should-fix | yes (by the reviewer) | `shipper_principal` returns 403 for non-shipper keys on quote endpoints. Difference 33. | `test_ops_keys_cannot_quote` |
| 6 | A failing `release()` turned the real 503 into a 500; a starved pool (30 s wait) gave a 500 | should-fix | yes (by the reviewer) | The release is guarded and logged; the pool waits 5 s; `PoolTimeout` → `503 service-unavailable`. Difference 35. | `test_a_failed_release_does_not_hide_the_real_error`, `test_a_starved_pool_is_a_503_not_a_500` |
| 7 | **Mutation-found gaps:** dropping the spec's `request_hash = EXCLUDED.request_hash` (takeover) or `status = 'in_progress'` (`complete`) left all 49 tests green | should-fix | yes (by the reviewer) | new tests | `test_taking_over_an_expired_lease_still_checks_the_body`, `test_complete_never_overwrites_a_completed_key` |
| 8 | The 20-duplicate test depended on a 0.3 s sleep (and a pool of 5); the hash test couldn't fail; the cancellation lease wasn't tested | should-fix / nit | yes | The stub now parks on an `asyncio.Event` (deterministic, no polling); the hash test looks up `sha256(key)`; a cancellation test was added | `test_a_cancelled_request_keeps_its_lease`; 3 consecutive runs green |
| 9 | The gate script didn't *assert* "some after 409 retries" | should-fix | yes | `seen["409"] > 0` is part of `ok` | the gate re-run |
| 10 | `begin()` was called "verbatim" but had an unmarked `# type: ignore` and an inert `# fmt: skip`; the non-201 replay branch couldn't run | should-fix / nit | yes | `begin` is typed and returns `Owned` (lab markers); the unreachable branch became an explicit error | mypy now checks the replay path |
| 11 | The ARCHITECTURE differences table didn't render: blank lines split rows 22–30 off it | should-fix | yes | blank lines removed; 36 rows in one table; row 36 lists the contract gaps (24 h retention, 429) | n/a |
| 12 | BUILD_LOG: the false ownership claim, the gate output not verbatim, the lease bullet | should-fix / nit | yes | corrected in the M6 section | n/a |

**Not changed (with reason):**
- **Anonymous malformed or oversize bodies get 400/413/415 before 401.** These are generic and reveal no schema; the docstring now says so.
- **An upstream success followed by a database failure strands the key until the lease expires.** The outcome is genuinely unknown to the shipper; the lease is the recovery path.
- **An expired quote is still replayed as 201 by a completed key.** It's part of the retention gap, difference 36.
- **Unsalted SHA-256** is acceptable for 32+ character random keys, which is now enforced outside dev.

**Verification after the fixes (live, API on :8000, mock at 300 ms):**
- **M6 gate:** `responses seen: {'409': 19, '201': 1, '201 replayed': 19}`, `distinct final bodies: 1`, `mock /__stats: {'calls': 1}`, `GATE: PASS` (it now also asserts the 409s).
- **M5 breaker:** the breaker opens on the 2nd call (mock at 5 calls). 300 circuit-open calls took a median of **2.5 ms**, p99 3.9 ms and max **6.0 ms**, with 0 at or above 10 ms. The no-database baseline is a 0.7 ms median and 4.6 ms max.
- **M5 k6 (50 VUs, 20 s, with key):** **264 × 201, 4,116 × 503**, all thresholds passed, mock **`peak_concurrency` 4**. Total requests fell to 4,380 (from 4,609), because every request now does its auth and idempotency work in Postgres.
- **`make check`:** ruff and `mypy --strict` clean, **251 passed**. The 60 API tests need `make mocks`; without it they skip, with the reason printed.

**Lessons:**
- **A milestone can break an earlier milestone's gate.** M6's auth broke M5's k6 script, and M6's database path broke M5's latency promise. Re-running earlier gates after every milestone is cheap; finding out in M9 wouldn't be.
- **"Only the owner may write" needs a token that proves ownership.** A status check says the work is unfinished, not *whose* it is: that is fencing.
- **A tail-latency claim needs a sample and a baseline:** 300 calls, and a no-database control, rather than one lucky run.

---

---

## M7 — Signed webhook fan-out   (2026-09-26, session 3)

**Goal / requirement served:** FR-5 (shippers subscribe to `shipment.created`, `shipment.status_changed` and `rate_quote.completed`), FR-6 (Standard Webhooks signatures, retries with full jitter, `410` disables, dead letter after 72 h) and FR-7 (ops list and replay dead letters). It also covers the section 9 rows *SSRF* (resolve and refuse non-public addresses before every attempt), *Tampering* (HMAC over the exact bytes sent) and *Repudiation* (every replay audited).

**What we built:**
- `migrations/0003_webhooks.sql`:
  - `webhook_subscriptions`: https-only URL and `whsec_` secret as `CHECK`s, `active` or `disabled` with a reason;
  - `webhook_deliveries`: the outbox. `delivery_id` (`uuidv7()`) is the stable `webhook-id`. It has a status, attempts, `next_attempt_at`, a `window_start` for the max age, and a partial index on due rows;
  - `dead_letters.delivery_id`, with a unique index on *open* webhook dead letters;
  - `dead_letter_replays`: the audit trail.
- `src/gateway/webhooks/signing.py` and `ssrf.py`: the spec's `sign`/`verify` and `assert_public_https`, verbatim. `ssrf.guard()` adds the dev-only exact-hostname allowance (difference 37).
- `src/gateway/webhooks/outbox.py`: `enqueue(conn, events)`, one `INSERT … SELECT` that fans each event out to the client's **active** subscriptions for that type, on the caller's own transaction.
- `src/gateway/ingest/poller.py`: `apply_rows()` (owner check → upsert → outbox), shared by the poller and the dead-letter replay. The upsert now uses Postgres 18 `RETURNING old.*, new.*` to tell `created` from `status_changed` (difference 41).
- `src/gateway/api/rate_quotes.py`: `rate_quote.completed` enqueued in the transaction that stores the quote (difference 43).
- `src/gateway/api/webhooks.py`: `POST`/`GET`/`DELETE /v1/webhook-subscriptions`. The SSRF check runs at creation (`422 webhook-url-not-allowed`). A 256-bit secret is returned **once**; BOLA gives `404`.
- `src/gateway/api/dead_letters.py`: `GET /v1/dead-letters` (ops only, filter-bound opaque cursor) and `POST /v1/dead-letters/{id}:replay`:
  - a row is re-validated, then applied (`200`);
  - a webhook is re-queued (`202`);
  - a conflict gives `409`, a rejected replay `422`;
  - every attempt is audited.
- `src/gateway/webhooks/dispatcher.py`: the worker (`make dispatch-local`, or `--once`).
- `make certs` + `compose.yaml`: the sink now serves HTTPS with a lab CA that only the dispatcher trusts (difference 38).
- Tests:
  - `tests/unit/test_webhooks_signing_ssrf.py` (33): the published test vector, tampering, the replay window, rotation, agreement with the lab sink's verifier, and the SSRF table;
  - `tests/integration/test_webhooks_api.py` (19);
  - `tests/integration/test_dispatcher.py` (12);
  - 4 outbox tests in `test_ingest.py`, including exactly-once events across a crash;
  - `tests/integration/conftest.py`: the shared API test database.

**How it works:**
- **The outbox is the whole trick.** The delivery rows are inserted by the *same transaction* that upserts the shipment (or stores the quote). If the batch rolls back, the events vanish with it; if it commits, they're durable before anyone tries to send them. There's no "we changed the row but crashed before publishing" window, and no dual write to a broker.
- **Event choice comes from the database, not from a guess.** `RETURNING old.status, (old.shipment_id IS NULL) AS inserted` says exactly what the upsert did:
  - an insert → `shipment.created`;
  - a changed status → `shipment.status_changed` with `previous_status`;
  - a no-op re-ingest → no event at all.
- **The claim is a lease, not a held lock.** `FOR UPDATE SKIP LOCKED` picks due rows that no other replica is claiming, and the same `UPDATE` pushes `next_attempt_at` 60 s out; then it **commits** before any HTTP call. A dispatcher that dies mid-send leaves rows that become due again after 60 s. That's at-least-once, and receivers de-duplicate on `webhook-id`, which stays the same across retries and replays.
- **Signing is over the exact bytes sent:** canonical JSON (sorted keys, compact, UTF-8), signed as `{webhook-id}.{webhook-timestamp}.{body}`. A retry re-signs with a fresh timestamp (the 5-minute replay window) but sends identical bytes.
- **Every attempt re-checks SSRF.** DNS answers change after a subscription is created, so creation-time validation isn't enough. Redirects aren't followed (a `302` to `169.254.169.254` is just a failed attempt), and one `asyncio.timeout(5)` bounds the lookup and the request together. This defeats *slow* DNS changes, not a hostile name server that answers differently a millisecond later: that rebinding window, between our lookup and the client's own connect, stays open (difference 44, found by the PR #5 review); the production control is the egress proxy.
- **Outcomes:**
  - `2xx` → delivered, and any open dead letter for it is resolved;
  - `410` → the subscription is disabled and its pending rows cancelled, so no more requests go to an endpoint that said it's gone;
  - anything else → retry after `uniform(0, min(6 h, 30 s × 2^n))`, or, once `WEBHOOK_MAX_AGE` has passed since `window_start`, `dead` plus a `dead_letters` row.
- **Replay re-queues the *same* delivery** (same `webhook-id`, `attempts` 0, a fresh window) and answers `202`: queued isn't delivered. The dead letter is resolved by the dispatcher when the delivery really lands. The audit row commits *before* any `409`/`422` is raised, so failed replays leave a trace too.

**Commands run, in order:**
1. Read the spec's M7 section, FR-5/6/7, section 9 (SSRF, Tampering, Repudiation), and the contract's `WebhookSubscription*`, `DeadLetter*` and event schemas.
2. Wrote `signing.py` and `ssrf.py` first, with their unit tests. The published Standard Webhooks vector reproduces exactly: `sign("whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, b'{"test": 2432232314}')` → `v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=`.
3. Wrote the migration, the outbox, the poller changes and the APIs, then ran `make migrate-local` → `applied 1 migration(s): 0003_webhooks`.
4. Ran `make certs` (the lab CA and the sink certificate), then `make mocks`. "What broke" 1 happened here.
5. Wrote the dispatcher and its tests, then ran `make check` → **318 passed**, ruff and mypy clean.
6. Started three processes in the background: `make api-local`, `make poll-local` and `make dispatch-local`.
7. Set up the sink:
   - created an ACME subscription with `POST /v1/webhook-subscriptions` (`https://localhost:9000/webhooks/acme`, `shipment.created` + `shipment.status_changed`);
   - stored the returned secret in the scratchpad, never printed;
   - gave it to the sink with `POST /__secrets`.
8. **Phase 1, signatures:** dropped two generated files (300 rows, then the same shipments re-exported with new statuses).
9. **Phase 2, a 10-minute outage:**
   - `docker compose stop -t 2 webhook-sink` at 20:21:03;
   - dropped `SHPSTS_20260926_2030.csv` (12 new shipments);
   - let the dispatcher retry for about 9.5 minutes.
10. **Phase 3, max age and replay:** the sink was still down, so:
    - stopped the dispatcher by PID and restarted it with `WEBHOOK_MAX_AGE=120s make dispatch-local`;
    - dropped `SHPSTS_20260926_2035.csv` (12 more) and waited for the dead letters;
    - `docker compose start webhook-sink` at 20:34:12, then gave the sink the secret again ("What broke" 5);
    - `GET /v1/dead-letters?kind=webhook&resolved=false`, then `POST …:replay` for each dead letter with the ops key.
11. Waited for the outage's last two deliveries to reach their scheduled retries, then stopped the API, poller and dispatcher by PID. Added the `SKIP LOCKED` test ("What broke" 3), then ran `make check` again → **319 passed**, ruff and mypy clean. Port 8000 is closed.

**Verification:**
The spec's four "Done when" clauses, one at a time. All times are UTC.

**1. The sink verifies 100% of signatures.** Phase 1: 294 new shipments (+ 6 bad rows dead-lettered), then 100 status changes. Of those, ACME's share became deliveries:

```text
shipment.created|delivered|105
shipment.status_changed|delivered|23
=== SPEC GATE: docker compose logs webhook-sink --since 15m | grep -c "signature=valid"
132
--- dispatcher deliveries (UUID webhook-id):
    128 signature=valid
--- M2 sink tests (msg_ ids):
      6 signature=invalid        <- the M2 tests' deliberate forgeries
      4 signature=valid
--- db delivered: 128
```

The spec's grep says 132, but that count includes the M2 sink tests' own traffic ("What broke" 2). Counting only the dispatcher's deliveries: 128 of 128 valid, 0 invalid, matching the 128 `delivered` rows. After phases 2 and 3, every further dispatcher delivery was also `signature=valid` (below).

**2. Stopping the sink for 10 minutes shows growing, jittered retry gaps.** This is from `dispatch.log`, per delivery (4 of the 5 shown; the stats cover all 5): the attempt number, when it ran, and the drawn wait against its cap (`30 s × 2^(n-1)`):

```text
44a9b0b65329 #1@20:21:07 wait 21.5s(cap 30)  #2@20:21:30 wait 8.5s(cap 60)  #3@20:21:39 wait 109.8s(cap 120)  #4@20:23:30 wait 35.4s(cap 240)  #5@20:24:05 wait 201.2s(cap 480)  #6@20:27:27 wait 898.7s(cap 960)
018fe1857d5d #1@20:21:07 wait 0.7s(cap 30)   #2@20:21:08 wait 38.6s(cap 60)  #3@20:21:47 wait 89.3s(cap 120)   #4@20:23:16 wait 238.3s(cap 240) #5@20:27:15 wait 70.2s(cap 480)  #6@20:28:26 wait 737.7s(cap 960)
36fc87af5030 #1@20:21:07 wait 17.2s(cap 30)  #2@20:21:25 wait 1.2s(cap 60)  #3@20:21:27 wait 118.3s(cap 120)  #4@20:23:26 wait 120.1s(cap 240) #5@20:25:26 wait 295.9s(cap 480) #6@20:30:23 wait 163.4s(cap 960)
8680c5b06687 #1@20:21:07 wait 22.9s(cap 30)  #2@20:21:31 wait 10.7s(cap 60)  #3@20:21:42 wait 42.8s(cap 120)  #4@20:22:26 wait 79.1s(cap 240)  #5@20:23:45 wait 276.0s(cap 480) #6@20:28:21 wait 198.7s(cap 960)
attempt 1: n=5 min=0.7s   max=22.9s  cap=30s
attempt 2: n=5 min=1.2s   max=38.6s  cap=60s
attempt 3: n=5 min=42.8s  max=118.3s cap=120s
attempt 4: n=5 min=35.4s  max=238.3s cap=240s
attempt 5: n=5 min=60.2s  max=295.9s cap=480s
attempt 6: n=5 min=163.4s max=898.7s cap=960s
```

- **Growing:** the ceiling doubles, and the largest gap per attempt number grows from 22.9 s to 898.7 s.
- **Jittered:** all five deliveries failed together at 20:21:07, and by attempt 3 they were spread over 20 seconds. Individual gaps can *shrink* (70.2 s after 238.3 s), which is what full jitter looks like. That spread is the point: a recovering shipper isn't hit by a synchronised wave.

**3. With `WEBHOOK_MAX_AGE=120s`, the row lands in `dead_letters`.** Five deliveries dead-lettered: two outage rows (already older than 120 s, at their 7th attempt) and all three new ones (after 4–5 attempts):

```text
20:31:40 delivery=…8680c5b06687 event=shipment.created -> dead-lettered after 7 attempts (ConnectError)
20:33:18 delivery=…33c3c8947667 event=shipment.created -> dead-lettered after 4 attempts (ConnectError)
$ curl -s 'localhost:8000/v1/dead-letters?kind=webhook&resolved=false' -H 'X-API-Key: dev-ops-key'
5 open webhook dead letters
{ "kind": "webhook", "reason": "max age 120s exceeded (last: ConnectError)",
  "source": {"subscription_id": "1026be46-…", "event_type": "shipment.created", "attempts": 5},
  "resolved": false, "replay_count": 0, ... }
```

**4. A replay delivers it once the sink is back.** Five replays → `202 queued`. A second replay of the first → `409 already-resolved`, because by then it had been delivered:

```text
 dead_letter_id                       | status    | attempts | last_result | resolved | replay_count
 01a0df6a-9a34-7add-acfb-9c6ab78656c5 | delivered |        1 | HTTP 200    | t        |            1
 ... (5 of 5 the same)
 ops_client_id | outcome          | count        <- dead_letter_replays: every attempt audited
 meridian-ops  | queued           |     5
 meridian-ops  | already_resolved |     1
webhook-sink delivery id=01a0df60-…-8680c5b06687 path=/acme signature=valid answered=200
... (5 of 5 signature=valid, same webhook-id as the original attempts)
```

**And the outage deliveries that were *not* dead-lettered recovered by themselves.** Their long scheduled retries came due after the sink was back, and each was delivered on its 7th or 8th attempt with no replay:

```text
 d81256da652f | delivered | 8 | HTTP 200 | 20:34:39
 018fe1857d5d | delivered | 7 | HTTP 200 | 20:40:45
 44a9b0b65329 | delivered | 7 | HTTP 200 | 20:42:27
```

**Totals at the end:** `webhook_deliveries` holds 136 `delivered` rows, and nothing pending, dead or cancelled. The sink logged **136 `signature=valid` and 0 invalid** for dispatcher (UUID) `webhook-id`s, counted over the whole run (about 25 minutes, so wider than the spec's `--since 15m`). That's 100%, and it matches the database one for one.

**Mutation check** (each applied, run, then restored):

| Mutation | Caught by |
|---|---|
| No SSRF check at subscription creation | `test_create_refuses_non_public_targets` (all 6 cases) |
| No SSRF check at send time | `test_ssrf_is_checked_again_at_send_time` |
| Backoff sleeps the ceiling (no jitter) | `test_backoff_doubles_from_30s_to_a_6h_cap` |
| A delivery doesn't resolve its dead letter | `test_max_age_dead_letters_and_replay_delivers` |
| `410` doesn't cancel the subscription's queue | `test_410_disables_the_subscription_and_cancels_its_queue` |
| A rejected replay raises before its audit row | `test_replay_row_applies_once_it_validates` |
| The outbox ignores `status = 'active'` | `test_created_events_fan_out_to_the_owners_subscriptions_only` |
| `FOR UPDATE` without `SKIP LOCKED` | **survived at first**, see "What broke" 3; now `test_a_second_dispatcher_does_not_wait_for_rows_being_claimed` |

**What broke and how we fixed it:**
1. *The M2 sink tests had been skipping silently, and the dispatcher couldn't connect to the new HTTPS sink.*
   - *Symptom:* `ssl.SSLCertVerificationError: … CA cert does not include key usage extension`.
   - *Hypothesis:* the lab CA was wrong, not the sink cert.
   - *Evidence:* `openssl verify` accepted the chain, but Python 3.14's default context sets `VERIFY_X509_STRICT`, which requires `keyUsage` on a CA.
   - *Root cause:* our CA certificate lacked the `keyUsage` extension that strict X.509 verification requires of a CA.
   - *Fix:* the CA is generated with `keyUsage=critical,keyCertSign,cRLSign`, and the leaf with SAN, EKU `serverAuth`, SKI/AKI. After regenerating the certs, 6 of 6 sink tests passed.
   - *Lesson:* `openssl verify` isn't the client. Test with the TLS stack that will actually connect.
2. *The spec's gate grep counts more than this subscription.*
   - `docker compose logs webhook-sink --since 15m | grep -c "signature=valid"` includes the M2 sink tests' own traffic (ids `msg_…`, including 6 *deliberate* forgeries that show up as `invalid`).
   - I split the count by `webhook-id` shape: dispatcher deliveries have UUID ids.
   - Phase 1: **128 of 128 valid**, which matches the 128 `delivered` rows in the database.
3. *Removing `SKIP LOCKED` survived the concurrency test.*
   - That was a correct result, not a flaky one. With a plain `FOR UPDATE`, the second claimer *waits*. Once the first commits, Postgres re-checks the `WHERE` on the locked rows, sees the new lease, and skips them, so the claims are still disjoint.
   - Correctness comes from the lease; `SKIP LOCKED` is what stops replicas **queueing behind each other**.
   - The new test holds one claim open and requires a second dispatcher to take the other rows within 2 s. The mutant now times out.
4. *The second generated file had `rows_dead=200`.* `make_export` re-randomises `SHIPPER_CODE` per call, so re-exporting the same shipments "moved" two thirds of them to other shippers. The owner-change refusal (M3) dead-lettered them, exactly as designed. That's a test-data artifact, not a bug.
5. *A restarted sink forgets its secret.* The lab sink keeps its accepted secrets in memory (`POST /__secrets`), so after `docker compose start` it would answer `400` (signature invalid) to everything. That's just more retries for the dispatcher, but it would have muddied the gate. I set the secret again right after the restart, before any delivery was due. A real receiver keeps its secret in its own secret store.

**Known limits:**
- **Secrets are stored in plaintext** (difference 39). They have to be: HMAC needs the key itself. Production wraps them with a KMS key.
- **Rotation** is supported by the verifier (several `v1,` signatures), but the gateway signs with one secret only, and there's no API to add a second (difference 46).
- **The DNS-rebinding window** between our lookup and the client's connect is not closed (difference 44).
- **A slow endpoint holds a dispatcher slot for up to 5 s.** The batch is concurrent (50), so one slow shipper doesn't stall the others within a batch. A per-subscription concurrency cap is future work.
- **The subscription list isn't paginated,** and there's no `PATCH` (to change `event_types`, delete and recreate).

**Cloud vs real customer environment:** at Meridian:
- The dispatcher's egress goes through an allow-listing proxy (the section 3 diagram's "egress allow-list"). The SSRF guard is defence in depth, not the only control.
- Shipper endpoints have public certificates, so `WEBHOOK_CA_BUNDLE` and `WEBHOOK_DEV_ALLOW_HOSTS` stay unset. A startup check should refuse either outside dev.
- Several dispatcher replicas share the outbox. `SKIP LOCKED` plus the lease is what makes that safe and fast.
- The 72 h max age is agreed with shippers in the integration guide, together with the reference `verify()` and "de-duplicate on `webhook-id`".

**Check yourself:**
1. The poller commits a batch of 1,000 rows, and the process is killed one millisecond later, before the dispatcher runs. What happens to the events for those rows? What if it was killed one millisecond *before* the commit?
2. Why is the claim's `UPDATE` committed *before* the HTTP call, instead of holding `FOR UPDATE` until the response arrives? What does that cost us, and who pays it?
3. A shipper's DNS name resolved to a public IP when they subscribed. Now it resolves to `10.0.0.5`. What does the dispatcher do, and what would happen if we only checked at creation?

<details><summary>answers</summary>

1. After the commit, the delivery rows are durable in the same transaction as the shipments, so the dispatcher sends them when it next runs: nothing is lost. Before the commit, the shipments *and* their events roll back together. The poller re-reads the file from the last checkpoint and produces both again, exactly once. There's never an event for a change that didn't happen, or a change without its event.
2. Holding the lock would keep a transaction and a pooled connection open for up to 5 s per delivery, and a crash mid-request would leave the row locked until the connection died. Committing a 60 s lease releases everything immediately. The cost is at-least-once delivery: if the dispatcher dies after the shipper received the request but before `record()` commits, the row is sent again after the lease. The *receiver* pays, by de-duplicating on `webhook-id` (which is why it's the stable `delivery_id`, even on replay).
3. `ssrf.guard()` runs before every attempt. It resolves the name, finds a private address, and records the attempt as `failed` with `ssrf: …`, without sending a byte. It then retries with backoff until the max age, and the delivery dead-letters. With a creation-time check only, the gateway would POST signed shipment data into Meridian's own network. (What the per-attempt check does *not* stop is a name server that flips its answer between our lookup and the connect a millisecond later: TLS verification then blocks the body, but the connection is made. That's difference 44, and why production also needs the egress proxy.)
</details>

**What would break in production here (spec section 12):**
- **A shipper endpoint down for days:** its deliveries back off to one attempt per ≤6 h and dead-letter at 72 h. The outbox grows by their event rate; the partial index on due rows keeps the claim cheap. Alert on dead letters per subscription, not per delivery.
- **A shipper that answers `200` without verifying:** we can't tell, and that's their risk. The integration guide and the reference `verify()` are the mitigation. A shipper that answers `410` by mistake disables its own subscription; it needs re-subscribing, and nothing is re-sent.
- **The outbox table never shrinks:** delivered rows stay forever. Retention (delete delivered rows after N days) is M9 ops work, like the idempotency keys.

---

## PR #5 review — three independent review agents   (2026-09-26, session 3)

**What happened:** before merging PR #5 (M7), three review agents read the diff in parallel, each with its own focus:
1. outbox, dispatcher and replay correctness and concurrency, against its own scratch database;
2. security and the API contract, with the app in-process against its own database;
3. spec fidelity, gate honesty, tests and docs. This one was the only reviewer running `make check`.

The gate reviewer cross-checked every BUILD_LOG number against the live database and the sink log:
- 136 delivered;
- 5 webhook dead letters, all resolved;
- 5 `queued` replays and 1 `already_resolved`;
- dead-letter attempt counts 7/7/5/4/4.

All of it matched, the arithmetic added up, and `make check` reproduced 319. The reviewer also noticed that the VM had restarted (dockerd down, so the integration tests *skipped*) and restarted it the way `cloud-setup.sh` does.

**The headline:** *one tenant could stop webhooks for every tenant.* All three reviewers found the same must-fix from different directions. Anything other than `httpx.TransportError` escaped `attempt()`: `InvalidURL` from a URL with a control character (the API accepted it with a 201), or `DecodingError` from a receiver's bad gzip. That aborted `gather()`, left the whole batch unrecorded, and killed the process. After the 60 s lease the batch was re-sent to everyone, and it crashed again.

**Findings and outcomes:**

| # | Finding | Severity | Reproduced? | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | **One receiver kills the dispatcher:** `InvalidURL` / `DecodingError` escape `attempt()`, abort the batch, exit the process; the batch is re-sent after the lease | must-fix (all 3 reviewers) | yes (all 3) | `attempt()` never raises (any exception → `failed`); each `record()` isolated; `run()` survives any error; the URL is validated with httpx's own parser at creation and in the guard. Difference 49 | `test_one_bad_receiver_cannot_take_the_batch_down`, `test_create_refuses_urls_that_slipped_through` |
| 2 | **Cross-tenant write:** a row replay racing the poller on a *new* shipment passes `OWNERS` (no row yet), waits on the insert, then `DO UPDATE`s it: ACME's shipment got BOLT's order number and ACME a webhook carrying it. M7 created this by adding a second writer (the replay) that the poller's advisory lock does not cover | must-fix | yes (scratch DB, two connections) | The upsert never updates another owner's row (`WHERE s.client_id = EXCLUDED.client_id`); rows that come back silently are re-checked and refused as owner changes. Difference 50 | `test_a_replay_racing_the_poller_cannot_take_another_shippers_shipment` |
| 3 | **Gzip bomb:** 407 KB on the wire → 400 MB decompressed, +800 MB RSS for one attempt, still "delivered" in 4.75 s | must-fix | yes | `client.stream()`, the outcome is the status code alone, the body is never read; `Accept-Encoding: identity`. Difference 49 | `test_the_response_body_is_never_read` (0 chunks read of a 655 MB stream) |
| 4 | **Stale outcomes overwrite newer state:** after a lease expired and another dispatcher delivered, the first one's late failure flipped `delivered` → `dead` with an open dead letter; a 410 in the same batch let a sibling's failure turn `cancelled` → `dead` | should-fix | yes (both) | Every outcome write is fenced: `status = 'pending' AND next_attempt_at = <the claim's lease>`; nothing is dead-lettered when it matches no row; 410s are recorded first. Difference 49 | `test_a_stale_outcome_cannot_overwrite_a_newer_one`, `test_a_410_in_a_batch_cancels_its_failing_siblings` |
| 5 | A second replay while a dispatcher held the lease reset `next_attempt_at`: two replicas sent the same row | should-fix | yes | Only a `dead` delivery is re-queued; otherwise `409 already-queued` (audited). Difference 50 | `test_a_second_replay_does_not_break_a_live_lease` |
| 6 | A row queued in a transaction that raced a 410 was sent to the endpoint that said it's gone (`Job.subscription_status` was fetched but never used) | should-fix | yes | Such rows are cancelled at claim time, never sent | `test_a_row_queued_for_a_disabled_subscription_is_never_sent` |
| 7 | Deleting a subscription mid-flight: the dead-letter `INSERT` hit a foreign-key violation and killed the dispatcher | should-fix | yes | The fenced update matches no row, so nothing is inserted | `test_deleting_the_subscription_mid_flight_does_not_crash` |
| 8 | **SSRF gaps:** NAT64 (`64:ff9b::a9fe:a9fe` = the metadata service) and `::127.0.0.1` pass Python's `is_global`; userinfo was kept and sent as Basic auth | should-fix | yes (API said 201) | `guard()` judges NAT64 / IPv4-compatible addresses by their embedded IPv4 and refuses userinfo, on top of the spec's verbatim check. Difference 45 | `test_guard_refuses_embedded_private_ipv4` (5 cases), `test_the_specs_check_alone_misses_nat64` |
| 9 | **DNS rebinding is not closed** by per-attempt checks; my BUILD_LOG called them the defence against "the classic DNS-rebinding SSRF" | should-fix | yes (resolver flip: a TCP connect and ClientHello reached 127.0.0.1) | **Not fixed in code**: recorded as difference 44 (the production control is the egress proxy); the BUILD_LOG claims corrected | n/a (documented gap) |
| 10 | The 422 detail leaked internal DNS ("resolves to 10.1.2.3", "Name or service not known"); the lookup had no time limit (8 s against a 5 s budget) | should-fix | yes | Fixed detail, reason logged; one 5 s budget for guard + POST in the dispatcher, 2 s in the API | `test_refusals_do_not_reveal_our_dns`, `test_create_refuses_…` |
| 11 | Tampered cursors gave 500s (bad date, non-string id); the list ignored the contract's "unresolved first" order | should-fix | yes | Every cursor field type-checked (400, fixed detail); keyset over `(resolved, created_at, id)` | `test_tampered_cursors_are_400_not_500` (5 cases), `test_list_puts_unresolved_first_across_pages` |
| 12 | The replay audit named only the ops *client* ("meridian-ops" for every operator) | should-fix | n/a | `ops_key_id` (a SHA-256 prefix, never the key) in `dead_letter_replays`, migration `0004` | asserted in the lease test |
| 13 | No cap on subscriptions (300 created): fan-out amplification from one key | should-fix | yes | 25 per shipper (`422 subscription-limit-reached`), count-then-insert under an advisory lock | `test_subscriptions_per_shipper_are_capped` |
| 14 | Max age: compared the app clock with a zoned DB timestamp (wrong across DST in a non-UTC session); dead-lettering could land ~6 h after 72 h | should-fix / nit | yes (Europe/London session) | The age is checked by the database clock; the last retry is clamped to the window's end | `test_the_last_retry_lands_on_the_max_age_not_after_it` |
| 15 | Weak tests: the 410 test forced `batch_size=1`; the redirect test used its own client; no test of `rate_quote.completed`; replay with a disabled subscription / a shipper key untested | should-fix | yes | Default-size 410 test; production-client test; outbox test for quotes; replay rejection + 403 tests | `test_the_production_client_does_not_follow_redirects`, `test_a_stored_quote_emits_one_rate_quote_completed_event`, `test_replaying_for_a_disabled_subscription_is_rejected_and_audited` |
| 16 | Undocumented deviations: no signing-side rotation; 410 siblings in flight still sent; DELETE removes history (contract said "cancelled") | should-fix | yes | Differences 46, 47, 48; the contract's DELETE, 403, 409 and 422 texts now say what happens | n/a |
| 17 | Nits: `Cache-Control: no-store` on the secret; a gateway User-Agent; the README's unquoted `?`; the Makefile help promising a 120 s max age it doesn't set; `gateway-api` still "planned"; `noqa: E402` imports; "4 of 5 shown"; "136 counted over the whole run" | nit | yes | all fixed | `test_the_secret_response_is_not_cacheable` |

**Not changed (with reason), recorded as difference 51 or in "Known limits":**
- **Row replay staleness:** replaying an old line can move a status backwards. This needs a policy decision (refuse if `updated_at` is newer?), so it goes in the runbook first.
- **A quote whose `complete()` lost its lease** is returned but emits no event. A lost lease means another request owns the key now.
- **The reference `verify()` raises on malformed headers** instead of returning False. It's the spec's code verbatim; wrapping it belongs in the shipper integration guide.
- **The dev host allowance ignores the port, and the lab CA has no name constraints.** Both are lab-only, and both settings are empty in production.

**Mutation check of the fixes** (each fix reverted, the suite run, then restored): **15 of 15 caught.** The mutations were:
- the upsert ignoring the owner;
- `attempt()` letting exceptions escape;
- `post` reading the body;
- unfenced `FAILED`;
- 410s not recorded first;
- a disabled subscription still sent;
- no max-age clamp;
- replay re-queuing non-dead rows;
- unchecked cursor types;
- newest-only order;
- no embedded-IPv4 check;
- the refusal leaking its reason;
- no subscription cap;
- the audit without a key id;
- no control-character check.

Separately, the earlier `SKIP LOCKED` mutant is still caught.

**What broke while fixing:**
1. *My first cross-tenant race test hung.* I closed the replay's transaction while its task was still blocked on that same connection, so the transaction's exit waited on the task, and the task waited on the poller's uncommitted transaction, which wasn't committing yet. The fix was to let the replay own its transaction inside the task. The lesson: with one connection per task, never share a transaction across tasks.
2. *The live re-gate's first deliveries got `503`.*
   - *Cause:* the M2 sink test `test_sink_mode_simulates_outage` leaves the shared lab sink in 503 mode. Its fixture reset the sink *before* each test, never after.
   - *What the dispatcher did:* retried correctly with backoff, and every one of those requests still carried a valid signature.
   - *Fix:* the fixture now resets on teardown too.

**Verification after the fixes:**
{{REGATE}}
