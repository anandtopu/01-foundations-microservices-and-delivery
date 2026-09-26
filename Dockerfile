# syntax=docker/dockerfile:1
# meridian-gateway (spec M8): ONE image that runs the API or either worker, chosen by the command:
#   (default)                                uvicorn gateway.app:app   (gateway-api, :8000)
#   python -m gateway.ingest.poller          sftp-poller
#   python -m gateway.webhooks.dispatcher    webhook-dispatcher
#   python -m gateway.migrate                one-off, before a deploy (migrations are additive)
#
# Why Alpine and three stages (the gate is "under 200 MB" in `docker image ls`):
# - python:3.14-slim alone is 125 MB unpacked, and the locked runtime dependencies add ~98 MB, so a
#   Debian-slim image cannot pass. python:3.14-alpine is 52 MB, and every binary dependency in
#   uv.lock ships a musllinux wheel for CPython 3.14 (checked: psycopg-binary, cryptography,
#   grpcio, uvloop, pydantic-core, ...). Same interpreter release as the host (3.14.7).
# - The wheels' shared objects carry symbols: `strip --strip-unneeded` takes the venv from 98 MB to
#   ~78 MB (uvloop alone 15.7 MB -> 2.3 MB). Deleting files in a later layer would not shrink the
#   image, so the venv is stripped in a throwaway stage and copied once. The strip stage is Debian
#   because the lab's network allowlist blocks dl-cdn.alpinelinux.org; any GNU strip works on ELF.

# --- 1. resolve and install the locked runtime dependencies (no dev tools) ------------------------
FROM python:3.14-alpine AS deps
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY src ./src
# build_ca: OPTIONAL BuildKit secret, the CA of a TLS-inspecting egress proxy (the cloud VM). Mounted
# for this RUN only, never stored in a layer; empty elsewhere (see mocks/Dockerfile.python).
RUN --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then \
      export PIP_CERT=/run/secrets/build_ca SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    pip install --no-cache-dir uv==0.12.19 && \
    uv sync --frozen --no-dev --no-editable --no-cache

# --- 2. strip symbols and bytecode caches in a throwaway stage -----------------------------------
FROM debian:trixie-slim AS strip
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends binutils >/dev/null
COPY --from=deps /app/.venv /app/.venv
RUN find /app/.venv -name '*.so*' -type f -exec strip --strip-unneeded {} + && \
    find /app/.venv -name __pycache__ -type d -prune -exec rm -rf {} +

# --- 3. the runtime image --------------------------------------------------------------------------
FROM python:3.14-alpine
RUN adduser -D -H -u 10001 -s /sbin/nologin gateway
WORKDIR /app
COPY --from=strip /app/.venv /app/.venv
COPY migrations ./migrations
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    MIGRATIONS_DIR=/app/migrations
USER 10001
EXPOSE 8000
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
