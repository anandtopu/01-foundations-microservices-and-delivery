#!/usr/bin/env bash
# Idempotent environment setup for a Claude Code cloud session (Ubuntu 24.04, x86_64).
# Use it either as the cloud environment's setup script, or have Claude run it: bash scripts/cloud-setup.sh
# Every step checks before installing, so it is safe to re-run after the VM is reclaimed.
set -euo pipefail

log() { printf '\n==> %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
SUDO=""; if [ "$(id -u)" -ne 0 ] && have sudo; then SUDO="sudo"; fi

log "OS / arch"
uname -srm; (. /etc/os-release && echo "$PRETTY_NAME") || true

log "uv"
if ! have uv; then curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; fi
# The VM image ships an old uv (0.8.x) whose download list stops at Python 3.14.0rc2.
# Upgrade it, because an old uv silently installs a release candidate instead of 3.14.7.
# Not `uv self update` (GitHub API, rate-limited on shared egress) nor astral.sh (not on the Trusted allowlist):
# install the pinned uv wheel from PyPI with the old uv itself.
UV_VERSION="0.12.19"
if [ "$(uv --version | cut -d' ' -f2)" != "$UV_VERSION" ]; then
  uv tool install --force "uv==${UV_VERSION}" || echo "WARN: could not upgrade uv to ${UV_VERSION} from PyPI"
  hash -r
fi
uv --version

log "Python 3.14 (managed by uv; downloads from GitHub, which is on the default allowlist)"
uv python install 3.14
py314=$(uv python find 3.14)
echo "$py314"
if "$py314" -c 'import sys; sys.exit(sys.version_info.releaselevel != "final")'; then
  "$py314" -V
else
  echo "WARN: $("$py314" -V) is a pre-release; upgrade uv and re-run so .python-version resolves to a final 3.14.x"
fi

log "Node (for npx @redocly/cli)"
node --version || echo "WARN: node missing"

log "OpenSSH client + ssh-keygen (for the SFTP lab)"
if ! have ssh-keygen || ! have sftp; then $SUDO apt-get update -y && $SUDO apt-get install -y openssh-client; fi

log "jq, curl"
if ! have jq; then $SUDO apt-get update -y && $SUDO apt-get install -y jq; fi

log "k6 (load tests) - release binary from GitHub"
if ! have k6; then
  K6_VERSION="v2.3.0"
  tmp=$(mktemp -d)
  if curl -fsSL "https://github.com/grafana/k6/releases/download/${K6_VERSION}/k6-${K6_VERSION}-linux-amd64.tar.gz" -o "$tmp/k6.tgz"; then
    tar -xzf "$tmp/k6.tgz" -C "$tmp" && $SUDO install -m 0755 "$tmp"/k6-*/k6 /usr/local/bin/k6
  else
    echo "WARN: k6 download failed (check the release tag or network allowlist); load tests will be skipped until installed"
  fi
fi
have k6 && k6 version || true

log "Container runtime"
if have docker; then
  # The cloud VM has dockerd installed but not running. Start it ourselves (idempotent: only if the socket is dead).
  if ! docker info >/dev/null 2>&1 && have dockerd && [ "$(id -u)" -eq 0 ]; then
    echo "INFO: docker daemon not running; starting dockerd in the background (log: /tmp/dockerd.log)"
    nohup dockerd >/tmp/dockerd.log 2>&1 &
    for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
  fi
  docker version --format 'client {{.Client.Version}} / server {{.Server.Version}}' || echo "WARN: docker CLI present but daemon not reachable"
  docker compose version || echo "WARN: docker compose plugin missing"
else
  echo "WARN: docker not found - use the NATIVE fallback described in CLAUDE.md"
fi

log "Native PostgreSQL (fallback if containers are unavailable)"
if have psql; then psql --version; else echo "INFO: psql not installed (only needed for the native fallback)"; fi

log "Done. Next: uv sync (after M0 creates pyproject.toml)."
