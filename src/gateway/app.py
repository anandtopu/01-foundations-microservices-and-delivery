"""The gateway API: `uvicorn gateway.app:app` (or `make api-local`).

M5 wires the rate-quote route to the resilience stack; M6 adds the Postgres pool, API-key auth
and idempotency; M7 adds webhook subscriptions and the ops dead-letter API; M8 adds shipments
(FR-3) and the /healthz and /readyz probes.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI

from gateway import errors
from gateway.api import dead_letters, health, rate_quotes, shipments, webhooks
from gateway.config import Settings, get_settings
from gateway.db import make_pool
from gateway.resilience import Bulkhead, CircuitBreaker


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # One pooled client for the process. Its pool is larger than the bulkhead on purpose: the
        # bulkhead, not the connection pool, is what limits concurrency towards Meridian.
        limits = httpx.Limits(max_connections=cfg.soap_max_concurrency * 2)
        pool = make_pool(cfg.database_url, max_size=cfg.db_pool_size)
        await pool.open(wait=True, timeout=10)  # a bad DSN fails at startup, not on request 1
        app.state.db = pool
        async with pool, httpx.AsyncClient(base_url=cfg.soap_base_url, limits=limits) as client:
            app.state.quotes = rate_quotes.QuoteService(
                client=client,
                bulkhead=Bulkhead(cfg.soap_max_concurrency, cfg.soap_bulkhead_wait_s),
                breaker=CircuitBreaker(cfg.breaker_failure_threshold, cfg.breaker_reset_after_s),
            )
            yield

    app = FastAPI(
        title="Meridian Legacy Integration Gateway",
        version="0.1.0",
        lifespan=lifespan,
        dependencies=[Depends(errors.reject_unknown_query)],  # unknown query params: 422 (M8)
        # The design-first contract is contracts/openapi.yaml (ADR-P01-4). FastAPI's generated
        # schema differs from it, so do not publish one at all (PR #3 review).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.cfg = cfg
    errors.install(app)
    app.include_router(health.router)
    app.include_router(shipments.router)
    app.include_router(rate_quotes.router)
    app.include_router(webhooks.router)
    app.include_router(dead_letters.router)
    return app


app = create_app()
