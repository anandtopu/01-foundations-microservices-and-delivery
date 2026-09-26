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
