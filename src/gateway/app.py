"""The gateway API: `uvicorn gateway.app:app` (or `make api-local`).

M5 wires the rate-quote route to the resilience stack. Later milestones add shipments (FR-3),
idempotency (M6), webhooks (M7), auth, /healthz and /readyz (M8).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from gateway import errors
from gateway.api import rate_quotes
from gateway.config import Settings, get_settings
from gateway.resilience import Bulkhead, CircuitBreaker


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # One pooled client for the process. Its pool is larger than the bulkhead on purpose: the
        # bulkhead, not the connection pool, is what limits concurrency towards Meridian.
        limits = httpx.Limits(max_connections=cfg.soap_max_concurrency * 2)
        async with httpx.AsyncClient(base_url=cfg.soap_base_url, limits=limits) as client:
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
        docs_url=None,  # the design-first contract is contracts/openapi.yaml (ADR-P01-4)
        redoc_url=None,
    )
    errors.install(app)
    app.include_router(rate_quotes.router)
    return app


app = create_app()
