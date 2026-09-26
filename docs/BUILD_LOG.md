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
