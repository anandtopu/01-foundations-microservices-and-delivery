"""RFC 9457 Problem Details for every error the API returns (FR-8, contract `Problem`).

One place turns exceptions into `application/problem+json`, so no route builds error bodies by hand.
The `type` URI is the stable, machine-readable part; `detail` is for humans and never carries
upstream internals (Meridian's faultstring is logged, not returned).
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from gateway.resilience import (
    BulkheadFull,
    CircuitOpenError,
    RetryableError,
    retry_after_seconds,
)
from gateway.soap.client import UpstreamRejected

log = logging.getLogger("gateway.errors")

TYPE_BASE = "https://errors.meridian-gateway.example/"
PROBLEM = "application/problem+json"

# ADR-P01-1: "shippers see 503 Retry-After: 2 at peak instead of a dead upstream".
SATURATED_RETRY_AFTER_S = 2


class ProblemError(Exception):
    """Raise from a route to return a specific problem (M6: 409 in progress, 422 key reused)."""

    def __init__(
        self,
        status: int,
        slug: str,
        title: str,
        detail: str | None = None,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.status, self.slug, self.title, self.detail = status, slug, title, detail
        self.retry_after = retry_after


def problem(
    status: int,
    slug: str,
    title: str,
    detail: str | None = None,
    *,
    retry_after: int | None = None,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {"type": TYPE_BASE + slug, "title": title, "status": status}
    if detail:
        body["detail"] = detail
    body |= extra or {}
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(body, status_code=status, media_type=PROBLEM, headers=headers)


def install(app: FastAPI) -> None:
    """Register the exception handlers on the app."""

    @app.exception_handler(ProblemError)
    async def _problem(_: Request, exc: ProblemError) -> JSONResponse:
        return problem(exc.status, exc.slug, exc.title, exc.detail, retry_after=exc.retry_after)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"location": "/" + "/".join(str(p) for p in e["loc"]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return problem(
            422,
            "validation-failed",
            "Request validation failed",
            f"{len(errors)} problem(s) in the request",
            extra={"errors": errors},
        )

    @app.exception_handler(BulkheadFull)
    async def _saturated(_: Request, exc: BulkheadFull) -> JSONResponse:
        return problem(
            503,
            "upstream-saturated",
            "Rate service at capacity",
            "All upstream slots are busy. Nothing was stored; retry after the delay.",
            retry_after=SATURATED_RETRY_AFTER_S,
        )

    @app.exception_handler(CircuitOpenError)
    async def _circuit(_: Request, exc: CircuitOpenError) -> JSONResponse:
        return problem(
            503,
            "circuit-open",
            "Rate service temporarily unavailable",
            "The upstream rate service is failing; calls are paused. Retry after the delay.",
            retry_after=retry_after_seconds(exc.retry_after),
        )

    @app.exception_handler(RetryableError)
    async def _busy(_: Request, exc: RetryableError) -> JSONResponse:
        log.warning("upstream still failing after retries: %s", exc)
        return problem(
            503,
            "upstream-busy",
            "Rate service busy",
            "The upstream rate service stayed busy after bounded retries. Retry after the delay.",
            retry_after=SATURATED_RETRY_AFTER_S,
        )

    @app.exception_handler(UpstreamRejected)
    async def _rejected(_: Request, exc: UpstreamRejected) -> JSONResponse:
        log.warning("upstream rejected the request: %s", exc)  # faultstring stays in our logs
        return problem(
            502,
            "upstream-rejected",
            "Rate service rejected the request",
            "Meridian's rate service could not quote this shipment. Check the ZIP codes, "
            "weight and service level; retrying the same request will fail the same way.",
        )

    @app.exception_handler(Exception)
    async def _internal(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return problem(500, "internal-error", "Internal server error")
