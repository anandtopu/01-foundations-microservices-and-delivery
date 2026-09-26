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
