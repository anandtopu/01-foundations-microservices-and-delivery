"""POST /v1/rate-quotes (FR-4).

M4: the request model. M5: the resilient call path. M6: API-key auth, idempotency keys (ADR-P01-2),
quote storage and GET /v1/rate-quotes/{quote_id}.

The model mirrors `RateQuoteRequest` in contracts/openapi.yaml and is the first line of defence:
nothing reaches the SOAP envelope unless it passed here.
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from gateway import idempotency
from gateway.auth import Principal, principal
from gateway.errors import ProblemError
from gateway.resilience import Bulkhead, CircuitBreaker, retry_full_jitter
from gateway.soap.client import UpstreamInvalidResponse, get_rate_quote

# [0-9], never \d: in Python regexes \d also matches other scripts' digits ("٣٠٣٠١"), which the
# contract's ECMA-262 pattern does not, and which Meridian's AS/400 would not understand.
ZIP = r"^[0-9]{5}$"
QUOTE_TTL = timedelta(minutes=15)  # FR-4: stored for 15 minutes
log = logging.getLogger("gateway.rate_quotes")

# What we accept from Meridian before it goes into a 201 (contract: RateQuote). Plain digits only:
# int() and Decimal() alone would accept "-4", "1_0", "1e3" and non-ASCII digits (PR #3 review).
_MONEY = re.compile(r"[0-9]{1,10}(\.[0-9]{1,2})?")
_CURRENCY = re.compile(r"[A-Z]{3}")
_DAYS = re.compile(r"[0-9]{1,3}")
_REF = re.compile(r"[\x21-\x7E]{1,64}")


def contract_fields(result: dict[str, str]) -> tuple[str, str, int, str | None]:
    """(total_charge "974.50", currency, transit_days, upstream_ref) or UpstreamInvalidResponse."""
    total = result.get("TotalCharge", "")
    currency = result.get("Currency", "")
    days = result.get("TransitDays", "")
    if not (_MONEY.fullmatch(total) and _CURRENCY.fullmatch(currency) and _DAYS.fullmatch(days)):
        raise UpstreamInvalidResponse(
            f"unusable GetRateQuoteResult: TotalCharge={total!r} Currency={currency!r} "
            f"TransitDays={days!r}"
        )
    ref = result.get("QuoteRef", "")
    return f"{Decimal(total):.2f}", currency, int(days), ref if _REF.fullmatch(ref) else None


class RateQuoteRequest(BaseModel):
    # extra="forbid": the contract says additionalProperties: false.
    # strict=True: "1200" (a string) is not a number; the contract says type: number.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    origin_zip: str = Field(pattern=ZIP)
    dest_zip: str = Field(pattern=ZIP)
    weight_lb: float = Field(gt=0, le=45000, allow_inf_nan=False)
    service_level: Literal["LTL_STANDARD", "LTL_EXPEDITED", "FTL"]


@dataclass
class QuoteService:
    """The resilient call path, composed outside-in (spec M5):

        bulkhead(4)  ->  retry, full jitter  ->  circuit breaker  ->  per-attempt timeout (3 s)

    - the bulkhead caps calls to Meridian at `bulkhead.size`, whatever the retry count. It is
      OUTERMOST so a request takes one slot and keeps it for its retries: inside the loop, a
      request could be refused (BulkheadFull) halfway through, after spending attempts, and the
      shipper would get a 503 for work already done;
    - the breaker is inside the retry loop, so once it opens, CircuitOpenError (not retryable)
      ends that request's retries at once;
    - the per-attempt timeout is get_rate_quote's own total deadline.
    """

    client: httpx.AsyncClient
    bulkhead: Bulkhead
    breaker: CircuitBreaker
    attempts: int = 3
    deadline: float = 4.0

    async def quote(self, q: RateQuoteRequest) -> dict[str, str]:
        async with self.bulkhead.slot():
            return await retry_full_jitter(
                lambda: self.breaker.call(lambda: get_rate_quote(self.client, q)),
                attempts=self.attempts,
                deadline=self.deadline,
            )


router = APIRouter()

IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=16, max_length=128, pattern=r"^[\x21-\x7E]+$")
]


Caller = Annotated[Principal, Depends(principal)]


class CanonicalJSON(JSONResponse):
    """Sorted keys, compact separators. A replay is read back from jsonb, which stores object keys
    in its own order, so without this the replayed bytes differ from the original response even
    though the data is identical (found by the M6 gate: 2 distinct bodies for 20 clients)."""

    def render(self, content: object) -> bytes:
        return json.dumps(
            content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()


def quote_response(body: dict[str, object], *, replayed: bool = False) -> JSONResponse:
    headers = {"Location": f"/v1/rate-quotes/{body['quote_id']}"}
    if replayed:
        headers["Idempotent-Replayed"] = "true"
    return CanonicalJSON(body, status_code=201, headers=headers)


@router.post("/v1/rate-quotes", status_code=201)
async def create_rate_quote(
    body: RateQuoteRequest, request: Request, who: Caller, idempotency_key: IdempotencyKey
) -> JSONResponse:
    pool: AsyncConnectionPool = request.app.state.db
    service: QuoteService = request.app.state.quotes
    fingerprint = idempotency.request_hash(body.model_dump(mode="json"))

    # 1. Claim the key, or learn that someone already has (409 / 422 / replay). Committed at once:
    #    the claim must be visible to concurrent duplicates BEFORE we call Meridian.
    async with pool.connection() as conn:
        stored = await idempotency.begin(conn, who.client_id, idempotency_key, fingerprint)
    if stored is not None:
        code, stored_body = stored
        return (
            quote_response(stored_body, replayed=True)
            if code == 201
            else CanonicalJSON(stored_body, status_code=code)
        )

    # 2. We own the key. Call Meridian WITHOUT holding a database connection: a 4 s upstream call
    #    must not pin one of the pool's few connections.
    try:
        total, currency, days, ref = contract_fields(await service.quote(body))
    except Exception:
        # Nothing was stored against the key, so the shipper may retry with the SAME key
        # (contract, 503). A cancelled request (client gone) keeps its lease instead, which
        # expires in 30 s and is then taken over by the retry.
        async with pool.connection() as conn:
            await idempotency.release(conn, who.client_id, idempotency_key)
        raise

    now = datetime.now(UTC)
    quote: dict[str, object] = {
        "quote_id": str(uuid.uuid4()),
        "request": body.model_dump(mode="json"),
        "total_charge": total,
        "currency": currency,
        "transit_days": days,
        "created_at": now.isoformat(),
        "expires_at": (now + QUOTE_TTL).isoformat(),
    }
    if ref:  # optional in the contract: omit rather than send junk
        quote["upstream_ref"] = ref

    # 3. Store the quote and the replayable response in ONE transaction: a replay can never point
    #    at a quote that does not exist.
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO rate_quotes (quote_id, client_id, body, created_at, expires_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (quote["quote_id"], who.client_id, Jsonb(quote), now, now + QUOTE_TTL),
        )
        if not await idempotency.complete(conn, who.client_id, idempotency_key, 201, quote):
            log.warning("idempotency key %r was taken over while we worked", idempotency_key)
    return quote_response(quote)


@router.get("/v1/rate-quotes/{quote_id}")
async def get_rate_quote_by_id(quote_id: uuid.UUID, request: Request, who: Caller) -> JSONResponse:
    pool: AsyncConnectionPool = request.app.state.db
    async with pool.connection() as conn:
        cur = await conn.execute(
            # client_id in the WHERE clause: another shipper's quote is "not found" (BOLA).
            """SELECT body FROM rate_quotes
                WHERE quote_id = %s AND client_id = %s AND expires_at > now()""",
            (quote_id, who.client_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise ProblemError(404, "not-found", "Not found", "No such quote for this API key.")
    return CanonicalJSON(row[0])
