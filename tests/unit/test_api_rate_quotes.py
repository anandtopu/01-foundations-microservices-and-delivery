"""M5: POST /v1/rate-quotes maps every outcome of the resilient call path to the contract:
201 + Location, or RFC 9457 Problem Details (503 + Retry-After, 502, 422). No network: the
QuoteService is replaced by a stub, and the ASGI app is called in-process.
"""

from collections.abc import AsyncIterator

import httpx
import pytest

from gateway.app import create_app
from gateway.config import Settings
from gateway.resilience import BulkheadFull, CircuitOpenError, RetryableError
from gateway.soap.client import UpstreamRejected

BODY = {"origin_zip": "30301", "dest_zip": "60601", "weight_lb": 1200, "service_level": "FTL"}
KEY = {"Idempotency-Key": "test-key-0001-aaaa"}
RESULT = {"QuoteRef": "MRQ-1", "TotalCharge": "974.56", "Currency": "USD", "TransitDays": "5"}


class Stub:
    def __init__(self, outcome: object) -> None:
        self.outcome, self.calls = outcome, 0

    async def quote(self, _q: object) -> dict[str, str]:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome  # type: ignore[return-value]


def api(outcome: object) -> tuple[httpx.AsyncClient, Stub]:
    app = create_app(Settings())
    stub = Stub(outcome)
    app.state.quotes = stub  # the ASGI transport does not run the lifespan
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://gw.test"), stub


@pytest.fixture
async def ok() -> AsyncIterator[tuple[httpx.AsyncClient, Stub]]:
    client, stub = api(RESULT)
    async with client:
        yield client, stub


async def test_created_quote_matches_the_contract(ok: tuple[httpx.AsyncClient, Stub]) -> None:
    client, _ = ok
    r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert r.status_code == 201
    body = r.json()
    assert r.headers["Location"] == f"/v1/rate-quotes/{body['quote_id']}"
    assert body["total_charge"] == "974.56"  # a string, as the contract requires
    assert (body["currency"], body["transit_days"], body["upstream_ref"]) == ("USD", 5, "MRQ-1")
    assert body["request"] == BODY | {"weight_lb": 1200.0}


@pytest.mark.parametrize(
    ("error", "status", "slug", "retry_after"),
    [
        (BulkheadFull("all 4 upstream slots busy"), 503, "upstream-saturated", "2"),
        (CircuitOpenError("open", retry_after=17.2), 503, "circuit-open", "18"),
        (RetryableError("Server.Busy"), 503, "upstream-busy", "2"),
        (UpstreamRejected("soapenv:Client: origin ZIP not served"), 502, "upstream-rejected", None),
    ],
    ids=["bulkhead_full", "circuit_open", "retries_exhausted", "client_fault"],
)
async def test_failures_are_problem_details(
    error: Exception, status: int, slug: str, retry_after: str | None
) -> None:
    client, _ = api(error)
    async with client:
        r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert r.status_code == status
    assert r.headers["Content-Type"] == "application/problem+json"
    problem = r.json()
    assert problem["type"] == f"https://errors.meridian-gateway.example/{slug}"
    assert problem["status"] == status
    assert r.headers.get("Retry-After") == retry_after


async def test_upstream_fault_text_never_reaches_the_shipper() -> None:
    client, _ = api(UpstreamRejected("soapenv:Client: AS400 CPF4131 member QTEMP/RQ locked"))
    async with client:
        r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert "CPF4131" not in r.text and "QTEMP" not in r.text


@pytest.mark.parametrize(
    ("body", "headers", "location"),
    [
        (BODY | {"origin_zip": "<x/>"}, KEY, "/body/origin_zip"),
        (BODY | {"weight_lb": "1200"}, KEY, "/body/weight_lb"),
        (BODY | {"extra": 1}, KEY, "/body/extra"),
        (BODY, {}, "/header/Idempotency-Key"),
        (BODY, {"Idempotency-Key": "short"}, "/header/Idempotency-Key"),
    ],
    ids=["bad_zip", "string_weight", "unknown_field", "missing_key", "short_key"],
)
async def test_invalid_requests_are_422_and_never_call_upstream(
    body: dict[str, object], headers: dict[str, str], location: str
) -> None:
    client, stub = api(RESULT)
    async with client:
        r = await client.post("/v1/rate-quotes", json=body, headers=headers)
    assert r.status_code == 422
    assert r.headers["Content-Type"] == "application/problem+json"
    assert location in [e["location"] for e in r.json()["errors"]]
    assert stub.calls == 0


async def test_unexpected_errors_are_a_bare_500_problem() -> None:
    client, _ = api(RuntimeError("secret internals"))
    async with client:
        r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert r.status_code == 500
    assert r.json()["type"].endswith("/internal-error")
    assert "secret" not in r.text
