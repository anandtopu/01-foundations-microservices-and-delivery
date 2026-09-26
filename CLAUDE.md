# CLAUDE.md: rules for building P01 in this repo

This repo is where **P01 (the Legacy Integration Gateway)** from the FDE Onboarding Handbook gets built. The build is meant to teach, not only to produce code. Follow `docs/CLOUD_BUILD_PROMPT.md` exactly. It is the task.

## Source of truth
- `spec/P01-legacy-integration-gateway.md` is the P01 spec (sections 1–12). If the spec and your memory disagree, the spec wins.
- `spec/ground-truth-digest.md` lists verified versions and dates as of September 2026, plus a list of stale-knowledge traps. If the spec and digest disagree with what you observe when you run something, show the evidence and propose a fix. Never change them silently.
- `spec/P01-P04-full-projects-file.md` is context only; P02–P04 will be built later.
- Relative links inside `spec/` (for example `../02-curriculum/...`) point into the handbook repo and are not present here. Ignore them.

## Environment: Claude Code cloud session
- Ubuntu 24.04 x86_64, a fresh VM per session. Docker is normally available. Postgres and Redis are preinstalled but not running.
- **Network is on the default "Trusted" allowlist.** PyPI, npm, GitHub and Docker Hub work. GHCR, quay.io and registry.k8s.io may be blocked. Prefer Docker Hub images: `postgres:18`, `debian:trixie-slim`, `python:3.14-slim`, `grafana/otel-lgtm`, `aquasec/trivy`, `anchore/grype`. If a pull or download is blocked, report the domain so the user can add it (Environment settings → Network access → Custom, keeping "include default list").
- Shell commands time out after about 2 minutes. Run long-lived processes (compose stacks, uvicorn, workers, pollers) and anything longer (k6 runs, a 20k-row ingest) in the background, and poll their logs.
- The VM is reclaimed when the session goes idle. **The only thing that survives is what you commit and push.** Commit and push at the end of every milestone, and never leave work uncommitted at a checkpoint.
- Run `bash scripts/cloud-setup.sh` at the start of every session. It is idempotent.

### If Docker is NOT usable (native fallback)
Run the same topology as local processes and document the difference in BUILD_LOG:
- Postgres: start the preinstalled server (`pg_ctlcluster`/`service postgresql start`) and create the `gateway` role and database.
- SFTP: run a second `sshd` as a non-root user on port 2222 with the spec's `sshd_config` (chroot needs root; if it isn't available, use `ForceCommand internal-sftp -R` without a chroot and record the gap).
- SOAP mock, webhook sink, API and workers: run them as `uv run` processes on fixed ports.
The gates stay the same; only the hostnames change (`localhost` instead of the compose service names). Pin the SFTP host key under the name you actually connect with.

## Safety rules (non-negotiable)
- Never kill processes by name (`pkill python`, `killall`). Stop only processes or containers you started, by PID or container name.
- Never run `docker system prune`, `docker volume prune`, or any delete outside this compose project.
- Never disable SSH host-key checking (`known_hosts=None`, `StrictHostKeyChecking=no`).
- `secrets/`, `.env` and private keys are gitignored and never printed or committed. Regenerate keys each session with `ssh-keygen`; do not store them in the repo.
- Never force-push, never rewrite `main` history, and never open PRs or change repo settings unless asked. Commit to the current branch and push.

## Working style
- Teaching protocol: brief → build one file at a time and explain it → explain each command and its expected output → run the milestone's "Done when" gate → log it in `docs/BUILD_LOG.md` → commit and push → checkpoint quiz → STOP and wait for "next".
- Conventional commits: `feat(m3): ...`, `test(m5): ...`, `docs(m2): ...`.
