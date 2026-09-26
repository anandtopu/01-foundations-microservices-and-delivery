"""POST /v1/rate-quotes (FR-4).

M4: the request model. M5: the resilient call path and a first route. M6 adds idempotency (the
Idempotency-Key is validated here but not yet stored), quote storage and GET /v1/rate-quotes/{id}.

The model mirrors `RateQuoteRequest` in contracts/openapi.yaml and is the first line of defence:
nothing reaches the SOAP envelope unless it passed here.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from gateway.resilience import Bulkhead, CircuitBreaker, retry_full_jitter
from gateway.soap.client import UpstreamInvalidResponse, get_rate_quote

# [0-9], never \d: in Python regexes \d also matches other scripts' digits ("٣٠٣٠١"), which the
# contract's ECMA-262 pattern does not, and which Meridian's AS/400 would not understand.
ZIP = r"^[0-9]{5}$"
QUOTE_TTL = timedelta(minutes=15)  # FR-4: stored for 15 minutes

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


@router.post("/v1/rate-quotes", status_code=201)
async def create_rate_quote(
    body: RateQuoteRequest, request: Request, response: Response, idempotency_key: IdempotencyKey
) -> dict[str, object]:
    service: QuoteService = request.app.state.quotes
    total, currency, transit_days, upstream_ref = contract_fields(await service.quote(body))
    now = datetime.now(UTC)
    quote_id = uuid.uuid4()
    response.headers["Location"] = f"/v1/rate-quotes/{quote_id}"
    quote: dict[str, object] = {
        "quote_id": str(quote_id),
        "request": body.model_dump(),
        "total_charge": total,
        "currency": currency,
        "transit_days": transit_days,
        "created_at": now.isoformat(),
        "expires_at": (now + QUOTE_TTL).isoformat(),
    }
    if upstream_ref:  # optional in the contract: omit rather than send junk
        quote["upstream_ref"] = upstream_ref
    return quote
