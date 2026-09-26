"""M5/M6: POST /v1/rate-quotes maps every outcome of the resilient call path to the contract:
201 + Location, or RFC 9457 Problem Details (503 + Retry-After, 502, 422). The QuoteService is a
stub and the ASGI app runs in-process; since M6 (auth + idempotency) the API needs Postgres, so
these tests use a throwaway, migrated database (gateway_api_test). Needs `make mocks`.
"""

import asyncio
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from gateway.app import create_app
from gateway.auth import add_key
from gateway.config import Settings
from gateway.db import make_pool
from gateway.migrate import load, migrate
from gateway.resilience import BulkheadFull, CircuitOpenError, RetryableError
from gateway.soap.client import UpstreamRejected

BODY = {"origin_zip": "30301", "dest_zip": "60601", "weight_lb": 1200, "service_level": "FTL"}
ADMIN_URL = "postgresql://gateway:gateway@localhost:5432/gateway"
TEST_URL = "postgresql://gateway:gateway@localhost:5432/gateway_api_test"
SHIPPER_KEY, OTHER_SHIPPER_KEY, OPS_KEY = "test-acme-key", "test-bolt-key", "test-ops-key"
KEY = {"X-API-Key": SHIPPER_KEY, "Idempotency-Key": "test-key-0001-aaaa"}

pytestmark = pytest.mark.integration
POOL: AsyncConnectionPool | None = None
_migrated = False


@pytest.fixture(autouse=True)
async def database() -> AsyncIterator[AsyncConnectionPool]:
    """A migrated gateway_api_test database (created once per run), emptied before every test."""
    global POOL, _migrated
    with socket.socket() as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", 5432)) != 0:
            pytest.skip("postgres not running (make mocks)")
    if not _migrated:
        async with await AsyncConnection.connect(ADMIN_URL, autocommit=True) as admin:
            await admin.execute("DROP DATABASE IF EXISTS gateway_api_test WITH (FORCE)")
            await admin.execute("CREATE DATABASE gateway_api_test")
        await migrate(TEST_URL, load(Path(__file__).parents[2] / "migrations"))
        await add_key(TEST_URL, SHIPPER_KEY, "ACME", "shipper", "test")
        await add_key(TEST_URL, OTHER_SHIPPER_KEY, "BOLT", "shipper", "test")
        await add_key(TEST_URL, OPS_KEY, "meridian-ops", "ops", "test")
        _migrated = True
    async with await AsyncConnection.connect(TEST_URL, autocommit=True) as conn:
        await conn.execute("TRUNCATE idempotency_keys, rate_quotes")
    POOL = make_pool(TEST_URL)
    await POOL.open(wait=True)
    try:
        yield POOL
    finally:
        await POOL.close()


RESULT = {"QuoteRef": "MRQ-1", "TotalCharge": "974.56", "Currency": "USD", "TransitDays": "5"}


class Stub:
    def __init__(self, outcome: object, delay: float = 0.0) -> None:
        self.outcome, self.calls, self.delay = outcome, 0, delay

    async def quote(self, _q: object) -> dict[str, str]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)  # keep the key in_progress while duplicates arrive
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome  # type: ignore[return-value]


def api(outcome: object, delay: float = 0.0) -> tuple[httpx.AsyncClient, Stub]:
    app = create_app(Settings())
    stub = Stub(outcome, delay)
    app.state.quotes = stub  # the ASGI transport does not run the lifespan...
    app.state.db = POOL  # ...so the test provides what it would have opened
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
        (BODY, {"X-API-Key": SHIPPER_KEY}, "/header/Idempotency-Key"),
        (BODY, {"X-API-Key": SHIPPER_KEY, "Idempotency-Key": "short"}, "/header/Idempotency-Key"),
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


# --- M6: API keys and idempotency (ADR-P01-2) ---------------------------------------------------

TYPE = "https://errors.meridian-gateway.example/"


def with_key(key: str, api_key: str = SHIPPER_KEY) -> dict[str, str]:
    return {"X-API-Key": api_key, "Idempotency-Key": key}


async def test_20_concurrent_duplicates_make_one_upstream_call() -> None:
    """The M6 gate, in-process: one call upstream; the duplicates that arrive while it is in
    flight get 409 + Retry-After; afterwards the same key replays byte-identical responses."""
    client, stub = api(RESULT, delay=0.3)
    async with client:
        first = await asyncio.gather(
            *(client.post("/v1/rate-quotes", json=BODY, headers=KEY) for _ in range(20))
        )
        statuses = sorted(r.status_code for r in first)
        assert statuses == [201] + [409] * 19
        busy = next(r for r in first if r.status_code == 409)
        assert busy.headers["Retry-After"] == "2"
        assert busy.json()["type"] == TYPE + "request-in-progress"  # the contract's own example
        original = next(r for r in first if r.status_code == 201)
        replays = await asyncio.gather(
            *(client.post("/v1/rate-quotes", json=BODY, headers=KEY) for _ in range(19))
        )
    assert stub.calls == 1
    assert {r.content for r in replays} == {original.content}  # byte-identical, not just equal
    assert all(r.headers["Idempotent-Replayed"] == "true" for r in replays)
    assert "Idempotent-Replayed" not in original.headers
    assert {r.headers["Location"] for r in replays} == {original.headers["Location"]}


async def test_same_key_with_a_different_body_is_422() -> None:
    client, stub = api(RESULT)
    async with client:
        await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
        r = await client.post("/v1/rate-quotes", json=BODY | {"weight_lb": 1300}, headers=KEY)
    assert r.status_code == 422
    assert r.json()["type"] == TYPE + "idempotency-key-reused"
    assert stub.calls == 1


async def test_the_hash_ignores_key_order_and_number_spelling() -> None:
    client, stub = api(RESULT)
    reordered = (
        b'{"service_level":"FTL","weight_lb":1200.0,"dest_zip":"60601","origin_zip":"30301"}'
    )
    async with client:
        a = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
        b = await client.post(
            "/v1/rate-quotes", content=reordered, headers=KEY | {"Content-Type": "application/json"}
        )
    assert (a.status_code, b.status_code, stub.calls) == (201, 201, 1)
    assert b.headers["Idempotent-Replayed"] == "true"


async def test_keys_are_scoped_per_shipper() -> None:
    client, stub = api(RESULT)
    async with client:
        a = await client.post("/v1/rate-quotes", json=BODY, headers=with_key("shared-key-000001"))
        b = await client.post(
            "/v1/rate-quotes", json=BODY, headers=with_key("shared-key-000001", OTHER_SHIPPER_KEY)
        )
    assert (a.status_code, b.status_code, stub.calls) == (201, 201, 2)
    assert a.json()["quote_id"] != b.json()["quote_id"]


async def test_a_failed_call_releases_the_key_for_a_retry() -> None:
    """Contract, 503: "Nothing was stored against your Idempotency-Key, so retrying with the same
    key is safe." The retry must reach upstream again, not replay the 503."""
    client, stub = api(RetryableError("Server.Busy"))
    async with client:
        r1 = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
        stub.outcome = RESULT
        r2 = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert (r1.status_code, r2.status_code, stub.calls) == (503, 201, 2)
    assert "Idempotent-Replayed" not in r2.headers


@pytest.mark.parametrize(
    ("lease", "expected"), [("-1 second", 201), ("+30 seconds", 409)], ids=["expired", "live"]
)
async def test_a_crashed_owner_is_taken_over_after_its_lease(lease: str, expected: int) -> None:
    """A worker died mid-flight and left the key in_progress. Once its 30 s lease has passed, a
    retry takes the key over instead of getting 409 forever (spec M6)."""
    from gateway.api.rate_quotes import RateQuoteRequest
    from gateway.idempotency import request_hash

    fingerprint = request_hash(RateQuoteRequest.model_validate(BODY).model_dump(mode="json"))
    assert POOL is not None
    async with POOL.connection() as conn:
        await conn.execute(
            """INSERT INTO idempotency_keys (client_id, key, request_hash, status, locked_until)
               VALUES ('ACME', %s, %s, 'in_progress', now() + %s::interval)""",
            (KEY["Idempotency-Key"], fingerprint, lease),
        )
    client, stub = api(RESULT)
    async with client:
        r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert r.status_code == expected
    assert stub.calls == (1 if expected == 201 else 0)


@pytest.mark.parametrize(
    "headers",
    [{"Idempotency-Key": "k" * 16}, with_key("k" * 16, "not-a-real-key")],
    ids=["missing", "unknown"],
)
async def test_calls_without_a_valid_key_are_401_before_validation(headers: dict[str, str]) -> None:
    client, stub = api(RESULT)
    async with client:
        r = await client.post("/v1/rate-quotes", json={"garbage": True}, headers=headers)
    assert r.status_code == 401  # not 422: an anonymous caller learns nothing about our schema
    assert r.json()["type"] == TYPE + "unauthorized"
    assert stub.calls == 0


async def test_revoked_keys_stop_working() -> None:
    await add_key(TEST_URL, "test-revoked-key", "ACME", "shipper", "old")
    assert POOL is not None
    async with POOL.connection() as conn:
        await conn.execute("UPDATE api_keys SET revoked_at = now() WHERE label = 'old'")
    client, _ = api(RESULT)
    async with client:
        r = await client.post(
            "/v1/rate-quotes", json=BODY, headers=with_key("k" * 16, "test-revoked-key")
        )
    assert r.status_code == 401


async def test_get_returns_the_stored_quote_to_its_owner_only() -> None:
    client, _ = api(RESULT)
    async with client:
        created = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
        location = created.headers["Location"]
        mine = await client.get(location, headers={"X-API-Key": SHIPPER_KEY})
        theirs = await client.get(location, headers={"X-API-Key": OTHER_SHIPPER_KEY})
        unknown = await client.get(
            "/v1/rate-quotes/00000000-0000-4000-8000-000000000000",
            headers={"X-API-Key": SHIPPER_KEY},
        )
    assert mine.status_code == 200
    assert mine.content == created.content  # the same document, byte for byte
    # BOLA (section 9): another shipper's quote is "not found", never "forbidden"
    assert theirs.status_code == unknown.status_code == 404
    assert theirs.content == unknown.content


async def test_expired_quotes_are_gone() -> None:
    client, _ = api(RESULT)
    async with client:
        created = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
        assert POOL is not None
        async with POOL.connection() as conn:
            await conn.execute("UPDATE rate_quotes SET expires_at = now() - interval '1 second'")
        r = await client.get(created.headers["Location"], headers={"X-API-Key": SHIPPER_KEY})
    assert r.status_code == 404


async def test_api_keys_are_stored_only_as_hashes() -> None:
    assert POOL is not None
    async with POOL.connection() as conn:
        cur = await conn.execute("SELECT key_hash FROM api_keys")
        hashes = [bytes(r[0]) for r in await cur.fetchall()]
    assert all(len(h) == 32 for h in hashes)
    assert not any(SHIPPER_KEY.encode() in h for h in hashes)
