"""Probes for the platform (not for shippers): GET /healthz and GET /readyz. No API key.

- /healthz: the process is up and serving. Never touches a dependency, so a slow database cannot
  make the orchestrator kill a healthy process (a liveness probe that checks dependencies turns a
  database blip into a restart storm).
- /readyz: 503 when the database or the SFTP drop is unreachable, so traffic goes elsewhere. The
  SOAP circuit is reported but does not fail readiness (contract): shipment reads still work.

The SFTP check is a TCP connect that expects the SSH banner. It needs no credentials, so the API
container never holds the SFTP private key (only the poller does). It proves the drop is reachable,
not that our key still works: the poller's own logs and the ingest-age alert cover that.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool

from gateway.config import Settings

router = APIRouter()
PROBE_TIMEOUT_S = 1.0


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


async def db_ok(pool: AsyncConnectionPool) -> bool:
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            async with pool.connection(timeout=PROBE_TIMEOUT_S) as conn:
                await conn.execute("SELECT 1")
        return True
    except Exception:  # any failure means "not ready", never a 500
        return False


async def sftp_ok(cfg: Settings) -> bool:
    writer = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            reader, writer = await asyncio.open_connection(cfg.sftp_host, cfg.sftp_port)
            banner = await reader.readline()
        return banner.startswith(b"SSH-2.0-")
    except Exception:
        return False
    finally:
        if writer is not None:
            writer.close()


@router.get("/readyz")
async def readiness(request: Request) -> JSONResponse:
    cfg: Settings = request.app.state.cfg
    db, sftp = await asyncio.gather(db_ok(request.app.state.db), sftp_ok(cfg))
    quotes = getattr(request.app.state, "quotes", None)
    circuit: Literal["closed", "half_open", "open"] = (
        quotes.breaker.state.value if quotes is not None else "closed"
    )
    ready = db and sftp
    body = {
        "status": "ready" if ready else "not_ready",
        "db": "ok" if db else "error",
        "sftp": "ok" if sftp else "error",
        "soap_circuit": circuit,
    }
    return JSONResponse(body, status_code=200 if ready else 503)
