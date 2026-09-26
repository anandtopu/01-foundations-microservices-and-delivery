"""Stand-in for Meridian's SOAP 1.1 RateQuoteService (spec M2).

Written for the lab; not part of the spec's code.

Reproduces the behaviour the gateway must survive:
- at most `max_concurrency` (default 5) requests in flight; above that,
  HTTP 500 + soapenv:Server.Busy
- `busy_rate`: a random fraction of calls also fail with Server.Busy (a sick upstream)
- `latency_ms`: every call is slow (Meridian's p99 is 2.8 s)
- HTTP 500 + soapenv:Client for requests it considers wrong (the same status code as "busy")

Control plane (never exposed by the real service):
- GET  /__stats   calls, in-flight and PEAK concurrency as seen on arrival, fault counters
- POST /__faults  partial update, e.g. {"latency_ms": 3000, "busy_rate": 0.3, "max_concurrency": 5}
- POST /__reset   zero the counters (keeps the fault settings)
"""

import asyncio
import logging
import os
import random
import uuid
from decimal import Decimal
from xml.sax.saxutils import escape

from defusedxml import ElementTree as ET
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, Field

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
RQ_NS = "urn:meridian:ratequote:v2"
XML = "text/xml; charset=utf-8"

log = logging.getLogger("soap-mock")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


class Faults(BaseModel):
    latency_ms: int = Field(default=int(os.getenv("SOAP_MOCK_LATENCY_MS", "300")), ge=0, le=60_000)
    busy_rate: float = Field(default=float(os.getenv("SOAP_MOCK_BUSY_RATE", "0")), ge=0, le=1)
    max_concurrency: int = Field(default=int(os.getenv("SOAP_MOCK_MAX_CONCURRENCY", "5")), ge=1)


class FaultsPatch(BaseModel):
    latency_ms: int | None = Field(default=None, ge=0, le=60_000)
    busy_rate: float | None = Field(default=None, ge=0, le=1)
    max_concurrency: int | None = Field(default=None, ge=1)


class Stats:
    def __init__(self) -> None:
        self.calls = self.inflight = self.peak_concurrency = 0
        self.ok = self.busy_faults = self.client_faults = 0


app = FastAPI(title="Meridian RateQuoteService (mock)")
faults = Faults()
stats = Stats()


def fault(code: str, message: str) -> Response:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{SOAP_NS}"><soapenv:Body><soapenv:Fault>'
        f"<faultcode>{escape(code)}</faultcode><faultstring>{escape(message)}</faultstring>"
        "</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
    )
    # SOAP 1.1 uses HTTP 500 for every fault: "bad request" and "busy" look identical at HTTP level.
    return Response(body, status_code=500, media_type=XML)


def price(origin: str, dest: str, weight: Decimal, level: str) -> tuple[Decimal, int]:
    distance = abs(int(origin[:3]) - int(dest[:3])) + 50
    rate = {
        "LTL_STANDARD": Decimal("0.0021"),
        "LTL_EXPEDITED": Decimal("0.0034"),
        "FTL": Decimal("0.0015"),
    }[level]
    days = {"LTL_STANDARD": 5, "LTL_EXPEDITED": 2, "FTL": 3}[level] + distance // 400
    total = (Decimal(distance) * weight * rate + Decimal("85.00")).quantize(Decimal("0.01"))
    return total, days


@app.post("/RateQuoteService")
async def rate_quote(request: Request) -> Response:
    stats.calls += 1
    stats.inflight += 1  # counted on ARRIVAL, before any decision: this is what the gateway sent us
    stats.peak_concurrency = max(stats.peak_concurrency, stats.inflight)
    log.info("arrival inflight=%d peak=%d", stats.inflight, stats.peak_concurrency)
    try:
        if stats.inflight > faults.max_concurrency:
            stats.busy_faults += 1
            return fault("soapenv:Server.Busy", "Too many concurrent requests")
        await asyncio.sleep(faults.latency_ms / 1000)
        if random.random() < faults.busy_rate:  # noqa: S311 - fault injection, not crypto
            stats.busy_faults += 1
            return fault("soapenv:Server.Busy", "Service temporarily unavailable")
        try:
            root = ET.fromstring(await request.body())
            req = root.find(f".//{{{RQ_NS}}}GetRateQuote")
            if req is None:
                raise ValueError("no GetRateQuote element")
            origin = (req.findtext(f"{{{RQ_NS}}}OriginZip") or "").strip()
            dest = (req.findtext(f"{{{RQ_NS}}}DestZip") or "").strip()
            weight = Decimal((req.findtext(f"{{{RQ_NS}}}WeightLb") or "").strip())
            level = (req.findtext(f"{{{RQ_NS}}}ServiceLevel") or "").strip()
            if not (origin.isdigit() and len(origin) == 5 and dest.isdigit() and len(dest) == 5):
                raise ValueError("ZIP codes must be 5 digits")
            if origin == "00000":
                raise ValueError("origin ZIP not served")  # a stable way to trigger a Client fault
            if not 0 < weight <= 45000 or level not in {"LTL_STANDARD", "LTL_EXPEDITED", "FTL"}:
                raise ValueError("weight or service level out of range")
        except Exception as exc:
            stats.client_faults += 1
            return fault("soapenv:Client", str(exc))
        total, days = price(origin, dest, weight, level)
        stats.ok += 1
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<soapenv:Envelope xmlns:soapenv="{SOAP_NS}" xmlns:rq="{RQ_NS}"><soapenv:Body>'
            "<rq:GetRateQuoteResponse><rq:GetRateQuoteResult>"
            f"<rq:QuoteRef>MRQ-{uuid.uuid4().hex[:10].upper()}</rq:QuoteRef>"
            f"<rq:TotalCharge>{total}</rq:TotalCharge><rq:Currency>USD</rq:Currency>"
            f"<rq:TransitDays>{days}</rq:TransitDays>"
            "</rq:GetRateQuoteResult></rq:GetRateQuoteResponse>"
            "</soapenv:Body></soapenv:Envelope>"
        )
        return Response(body, status_code=200, media_type=XML)
    finally:
        stats.inflight -= 1


@app.get("/__stats")
async def get_stats() -> dict[str, object]:
    return {**vars(stats), "faults": faults.model_dump()}


@app.post("/__faults")
async def set_faults(patch: FaultsPatch) -> dict[str, object]:
    global faults
    faults = faults.model_copy(update=patch.model_dump(exclude_none=True))
    log.info("faults=%s", faults.model_dump())
    return faults.model_dump()


@app.post("/__reset")
async def reset() -> dict[str, object]:
    global stats
    live = stats.inflight  # requests still running will decrement the NEW object in their finally
    stats = Stats()
    stats.inflight = stats.peak_concurrency = live
    return vars(stats)


@app.get("/__health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
