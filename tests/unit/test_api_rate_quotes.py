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


# --- PR #3 review ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        RESULT | {"TotalCharge": "1e3"},
        RESULT | {"TotalCharge": "-5.00"},
        RESULT | {"TotalCharge": "974.567"},
        RESULT | {"Currency": "usd"},
        RESULT | {"Currency": "<script>"},
        RESULT | {"TransitDays": "-4"},
        RESULT | {"TransitDays": "1_0"},
        RESULT | {"TransitDays": "99999999999999999999999"},
        RESULT | {"TransitDays": "٣"},
        {"QuoteRef": "MRQ-1"},  # the fields are missing entirely
    ],
    ids=[
        "exponent", "negative", "3dp", "lowercase_ccy", "markup_ccy", "negative_days",
        "underscore_days", "huge_days", "arabic_days", "missing",
    ],
)  # fmt: skip
async def test_unusable_upstream_data_never_becomes_a_201(result: dict[str, str]) -> None:
    client, _ = api(result)
    async with client:
        r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert r.status_code == 502
    assert r.json()["type"].endswith("/upstream-invalid-response")
    assert "your request" in r.json()["detail"]  # says it is NOT the shipper's fault


async def test_upstream_money_is_normalised_to_two_decimals() -> None:
    client, _ = api(RESULT | {"TotalCharge": "974.5", "QuoteRef": "bad ref with spaces"})
    async with client:
        r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert r.status_code == 201
    assert r.json()["total_charge"] == "974.50"  # contract pattern ^[0-9]+\.[0-9]{2}$
    assert "upstream_ref" not in r.json()  # optional field: omitted rather than junk


@pytest.mark.parametrize(
    ("method", "path", "status", "slug"),
    [("GET", "/nope", 404, "not-found"), ("PUT", "/v1/rate-quotes", 405, "method-not-allowed")],
)
async def test_routing_errors_are_problem_details(
    method: str, path: str, status: int, slug: str
) -> None:
    client, _ = api(RESULT)
    async with client:
        r = await client.request(method, path)
    assert r.status_code == status
    assert r.headers["Content-Type"] == "application/problem+json"
    assert r.json()["type"].endswith(f"/{slug}")
    if status == 405:
        assert r.headers["Allow"] == "POST"


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
async def test_non_json_bodies_are_415(content_type: str) -> None:
    client, stub = api(RESULT)
    async with client:
        r = await client.post(
            "/v1/rate-quotes",
            content=b'{"origin_zip":"30301"}',
            headers=KEY | {"Content-Type": content_type},
        )
    assert r.status_code == 415
    assert r.json()["type"].endswith("/unsupported-media-type")
    assert stub.calls == 0


async def test_json_with_charset_is_fine(ok: tuple[httpx.AsyncClient, Stub]) -> None:
    client, _ = ok
    r = await client.post(
        "/v1/rate-quotes",
        content=httpx.Request("POST", "/", json=BODY).content,
        headers=KEY | {"Content-Type": "application/json; charset=utf-8"},
    )
    assert r.status_code == 201


async def test_oversize_body_is_413_by_content_length(ok: tuple[httpx.AsyncClient, Stub]) -> None:
    client, stub = ok
    big = b'{"x":"' + b"a" * (64 * 1024) + b'"}'
    r = await client.post(
        "/v1/rate-quotes", content=big, headers=KEY | {"Content-Type": "application/json"}
    )
    assert r.status_code == 413
    assert r.json()["type"].endswith("/payload-too-large")
    assert stub.calls == 0


async def test_oversize_body_is_413_without_content_length_too() -> None:
    from gateway.errors import RequestGuard

    seen: list[str] = []

    async def app(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append("app reached")

    guard = RequestGuard(app, max_body=100)  # type: ignore[arg-type]
    chunks = [b"x" * 60, b"x" * 60]  # 120 bytes, chunked: no Content-Length header
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "headers": [(b"content-type", b"application/json")]}
    await guard(scope, receive, send)  # type: ignore[arg-type]
    assert sent[0]["status"] == 413
    assert seen == []


async def test_malformed_json_is_400(ok: tuple[httpx.AsyncClient, Stub]) -> None:
    client, _ = ok
    r = await client.post(
        "/v1/rate-quotes",
        content=b'{"origin_zip":',
        headers=KEY | {"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["type"].endswith("/malformed-json")


async def test_validation_errors_are_capped_and_pointer_escaped(
    ok: tuple[httpx.AsyncClient, Stub],
) -> None:
    client, _ = ok
    body: dict[str, object] = BODY | {f"k{i}": 1 for i in range(100)} | {"a/b~c": 1}
    r = await client.post("/v1/rate-quotes", json=body, headers=KEY)
    assert r.status_code == 422
    problem = r.json()
    assert len(problem["errors"]) == 20
    assert "101 problem(s)" in problem["detail"]
    r = await client.post("/v1/rate-quotes", json=BODY | {"a/b~c": 1}, headers=KEY)
    assert [e["location"] for e in r.json()["errors"]] == ["/body/a~1b~0c"]


async def test_500_carries_an_instance_for_support() -> None:
    client, _ = api(RuntimeError("secret internals"))
    async with client:
        r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert r.json()["instance"].startswith("urn:uuid:")


async def test_trial_in_flight_says_retry_after_1() -> None:
    client, _ = api(CircuitOpenError("half-open trial in flight", 1.0))
    async with client:
        r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert (r.status_code, r.headers["Retry-After"]) == (503, "1")


async def test_no_generated_openapi_is_published(ok: tuple[httpx.AsyncClient, Stub]) -> None:
    client, _ = ok
    assert (await client.get("/openapi.json")).status_code == 404  # the contract is the YAML
