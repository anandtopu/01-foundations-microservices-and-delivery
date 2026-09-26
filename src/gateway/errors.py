"""RFC 9457 Problem Details for every error the API returns (FR-8, contract `Problem`).

One place turns exceptions into `application/problem+json`, so no route builds error bodies by hand.
The `type` URI is the stable, machine-readable part; `detail` is for humans and never carries
upstream internals (Meridian's faultstring is logged, not returned).
"""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.dependencies.models import Dependant
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from psycopg_pool import PoolTimeout
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.resilience import (
    BulkheadFull,
    CircuitOpenError,
    RetryableError,
    retry_after_seconds,
)
from gateway.soap.client import UpstreamInvalidResponse, UpstreamRejected

log = logging.getLogger("gateway.errors")

TYPE_BASE = "https://errors.meridian-gateway.example/"
PROBLEM = "application/problem+json"

# ADR-P01-1: "shippers see 503 Retry-After: 2 at peak instead of a dead upstream".
SATURATED_RETRY_AFTER_S = 2
MAX_BODY_BYTES = 64 * 1024  # spec section 9: 64 KB request body limit
MAX_REPORTED_ERRORS = 20  # a hostile body with 200,000 bad keys must not get a 22 MB 422 back
_HTTP_SLUGS = {404: "not-found", 405: "method-not-allowed", 413: "payload-too-large"}


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


def pointer(loc: tuple[int | str, ...]) -> str:
    """('body', 'a/b') -> '/body/a~1b' (RFC 6901 escaping)."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in loc)


class RequestGuard:
    """ASGI middleware, before any route: 413 for bodies over MAX_BODY_BYTES (counted even when
    there is no Content-Length, i.e. chunked uploads) and 415 for a non-empty body that is not
    application/json. Both answer with Problem Details (PR #3 review)."""

    def __init__(self, app: ASGIApp, max_body: int = MAX_BODY_BYTES) -> None:
        self.app, self.max_body = app, max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        length = headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_body):
            await self._too_large(scope, receive, send)
            return
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.max_body:
                await self._too_large(scope, receive, send)
                return
        media_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if body and media_type != "application/json":
            response = problem(
                415,
                "unsupported-media-type",
                "Unsupported media type",
                "Send the request body as application/json.",
            )
            await response(scope, receive, send)
            return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        detail = f"The request body must be at most {self.max_body} bytes."
        response = problem(413, "payload-too-large", "Payload too large", detail)
        await response(scope, receive, send)


_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


def allowed_methods(request: Request) -> str:
    """Every method any route serves at this path. Starlette's own Allow header lists only the
    FIRST route that matched the path: GET and DELETE on /v1/webhook-subscriptions/{id} are two
    routes, so its 405 said "Allow: GET" (RFC 9110 says: all of them; found by Schemathesis, M8).
    We ask the router itself, method by method, so included sub-routers are covered too."""
    allowed = [
        method
        for method in _METHODS
        if any(
            route.matches({**request.scope, "method": method})[0] is Match.FULL
            for route in request.app.router.routes
        )
    ]
    return ", ".join(allowed)


def declared_query(dependant: Dependant) -> set[str]:
    names = {param.alias for param in dependant.query_params}
    for sub in dependant.dependencies:
        names |= declared_query(sub)
    return names


async def reject_unknown_query(request: Request) -> None:
    """App-wide dependency: a query parameter the route does not declare is a 422, like an unknown
    body field (extra="forbid"). Otherwise `?updatedSince=...` (a typo) is silently ignored and
    the shipper gets EVERY shipment back instead of an error (found by Schemathesis, M8)."""
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return
    unknown = sorted(set(request.query_params) - declared_query(route.dependant))
    if unknown:
        raise RequestValidationError(
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("query", name),
                    "msg": "Unknown query parameter",
                }
                for name in unknown
            ]
        )


def install(app: FastAPI) -> None:
    """Register the exception handlers on the app."""

    @app.exception_handler(ProblemError)
    async def _problem(_: Request, exc: ProblemError) -> JSONResponse:
        return problem(exc.status, exc.slug, exc.title, exc.detail, retry_after=exc.retry_after)

    app.add_middleware(RequestGuard)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Routing errors (404, 405) must be Problem Details too (FR-8: every error).
        response = problem(
            exc.status_code,
            _HTTP_SLUGS.get(exc.status_code, f"http-{exc.status_code}"),
            str(exc.detail),
        )
        response.headers.update(exc.headers or {})  # keeps e.g. 405's Allow header
        if exc.status_code == 405:
            response.headers["Allow"] = allowed_methods(request)
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        found = exc.errors()
        if any(e["type"] == "json_invalid" for e in found):
            return problem(
                400, "malformed-json", "Malformed request", "The body is not valid JSON."
            )
        # Messages and locations only: FastAPI's `input` would echo the shipper's values back.
        errors = [
            {"location": pointer(tuple(e["loc"]))[:200], "message": str(e["msg"])[:200]}
            for e in found[:MAX_REPORTED_ERRORS]
        ]
        return problem(
            422,
            "validation-failed",
            "Request validation failed",
            f"{len(found)} problem(s) in the request"
            + (f"; the first {MAX_REPORTED_ERRORS} are listed" if len(found) > len(errors) else ""),
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
        # 422, not 502 (M8, Schemathesis): Meridian answered correctly that THIS request cannot
        # be quoted (e.g. an origin ZIP it does not serve). That is about the shipper's input; a 5xx
        # would tell shippers to retry and would page us for their typos. ARCHITECTURE diff. 52.
        return problem(
            422,
            "upstream-rejected",
            "Rate service rejected the request",
            "Meridian's rate service could not quote this shipment. Check the ZIP codes, "
            "weight and service level; retrying the same request will fail the same way.",
        )

    @app.exception_handler(UpstreamInvalidResponse)
    async def _invalid(_: Request, exc: UpstreamInvalidResponse) -> JSONResponse:
        log.error("upstream returned unusable data: %s", exc)
        return problem(
            502,
            "upstream-invalid-response",
            "Rate service returned an unusable quote",
            "Meridian's rate service answered with data we cannot pass on. This is not a problem "
            "with your request; try again later.",
        )

    @app.exception_handler(PoolTimeout)
    async def _pool(_: Request, exc: PoolTimeout) -> JSONResponse:
        log.error("no database connection available: %s", exc)
        return problem(
            503,
            "service-unavailable",
            "Service temporarily unavailable",
            "The gateway is overloaded. Nothing was stored; retry after the delay.",
            retry_after=SATURATED_RETRY_AFTER_S,
        )

    @app.exception_handler(Exception)
    async def _internal(_: Request, exc: Exception) -> JSONResponse:
        # instance ties the shipper's support ticket to this log line (contract: InternalError).
        instance = f"urn:uuid:{uuid.uuid4()}"
        log.exception("unhandled error instance=%s", instance)
        return problem(500, "internal-error", "Internal server error", extra={"instance": instance})
