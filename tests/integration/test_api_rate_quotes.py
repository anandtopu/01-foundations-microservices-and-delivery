"""M5/M6: POST /v1/rate-quotes maps every outcome of the resilient call path to the contract:
201 + Location, or RFC 9457 Problem Details (503 + Retry-After, 422, 502). The QuoteService is a
stub and the ASGI app runs in-process; since M6 (auth + idempotency) the API needs Postgres, so
these tests use a throwaway, migrated database (gateway_api_test). Needs `make mocks`.
"""

import asyncio
import hashlib
from collections.abc import AsyncIterator

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

from gateway.app import create_app
from gateway.auth import add_key
from gateway.config import Settings
from gateway.errors import ProblemError
from gateway.resilience import BulkheadFull, CircuitBreaker, CircuitOpenError, RetryableError
from gateway.soap.client import UpstreamRejected
from tests.integration.conftest import API_TEST_URL as TEST_URL
from tests.integration.conftest import OPS_KEY, OTHER_SHIPPER_KEY, SHIPPER_KEY

BODY = {"origin_zip": "30301", "dest_zip": "60601", "weight_lb": 1200, "service_level": "FTL"}

KEY = {"X-API-Key": SHIPPER_KEY, "Idempotency-Key": "test-key-0001-aaaa"}

pytestmark = pytest.mark.integration
POOL: AsyncConnectionPool | None = None


@pytest.fixture(autouse=True)
async def database(api_pool: AsyncConnectionPool) -> AsyncIterator[AsyncConnectionPool]:
    """The shared gateway_api_test database (conftest), exposed to api() as POOL."""
    global POOL
    POOL = api_pool
    yield api_pool


RESULT = {"QuoteRef": "MRQ-1", "TotalCharge": "974.56", "Currency": "USD", "TransitDays": "5"}


class Stub:
    def __init__(self, outcome: object, delay: float = 0.0) -> None:
        self.outcome, self.calls, self.delay = outcome, 0, delay
        self.gate: asyncio.Event | None = None  # if set, quote() waits for it (deterministic)
        self.entered = asyncio.Event()  # set as soon as a call is inside the "upstream"
        self.breaker = CircuitBreaker()  # the route checks it for the circuit-open fast path

    async def quote(self, _q: object) -> dict[str, str]:
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()  # keep the key in_progress until the test says so
        if self.delay:
            await asyncio.sleep(self.delay)
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
        (UpstreamRejected("soapenv:Client: origin ZIP not served"), 422, "upstream-rejected", None),
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
    """The M6 gate, in-process and deterministic: the first request holds the key (its upstream
    call is parked on an event) while 19 duplicates arrive and get 409 + Retry-After; afterwards
    the same key replays byte-identical responses. (Was a 0.3 s sleep: timing-dependent on a slow
    box, PR #4 review.)"""
    client, stub = api(RESULT)
    stub.gate = asyncio.Event()
    async with client:
        first = asyncio.create_task(client.post("/v1/rate-quotes", json=BODY, headers=KEY))
        await stub.entered.wait()  # the owner is inside the upstream call
        busy = await asyncio.gather(
            *(client.post("/v1/rate-quotes", json=BODY, headers=KEY) for _ in range(19))
        )
        assert {r.status_code for r in busy} == {409}
        assert {r.headers["Retry-After"] for r in busy} == {"2"}
        assert busy[0].json()["type"] == TYPE + "request-in-progress"  # the contract's example
        stub.gate.set()
        original = await first
        assert original.status_code == 201
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
        cur = await conn.execute(
            "SELECT key_hash, client_id FROM api_keys WHERE key_hash = %s",
            (hashlib.sha256(SHIPPER_KEY.encode()).digest(),),
        )
        assert await cur.fetchone() is not None  # found by its SHA-256...
        cur = await conn.execute(
            "SELECT count(*) FROM api_keys WHERE key_hash = %s", (SHIPPER_KEY.encode(),)
        )
        assert (await cur.fetchone()) == (0,)  # ...and never stored in plain text


# --- PR #4 review ---------------------------------------------------------------------------------


async def claim(key: str, body: dict[str, object] = BODY) -> object:
    from gateway.api.rate_quotes import RateQuoteRequest
    from gateway.idempotency import begin, request_hash

    fingerprint = request_hash(RateQuoteRequest.model_validate(body).model_dump(mode="json"))
    assert POOL is not None
    async with POOL.connection() as conn:
        return await begin(conn, "ACME", key, fingerprint)


async def expire_lease(key: str) -> None:
    assert POOL is not None
    async with POOL.connection() as conn:
        await conn.execute(
            "UPDATE idempotency_keys SET locked_until = now() - interval '1 second' WHERE key = %s",
            (key,),
        )


async def test_a_stale_owner_cannot_complete_after_a_takeover() -> None:
    """A's lease expired and B took the key over. A finishing first must not win: B's result is
    the one stored and replayed (fencing token, PR #4 review)."""
    from gateway.idempotency import Owned, complete

    a = await claim("fence-key-0000001")
    await expire_lease("fence-key-0000001")
    b = await claim("fence-key-0000001")
    assert isinstance(a, Owned) and isinstance(b, Owned) and a.token != b.token
    assert POOL is not None
    async with POOL.connection() as conn:
        assert await complete(conn, "ACME", "fence-key-0000001", a.token, 201, {"q": "A"}) is False
        assert await complete(conn, "ACME", "fence-key-0000001", b.token, 201, {"q": "B"}) is True
        cur = await conn.execute(
            "SELECT response_body FROM idempotency_keys WHERE key = 'fence-key-0000001'"
        )
        assert await cur.fetchone() == ({"q": "B"},)


async def test_a_stale_owner_cannot_release_the_new_owners_claim() -> None:
    """Without fencing, A's failed call deleted B's claim and a third request C could then call
    Meridian while B was still running (PR #4 review, reproduced)."""
    from gateway.idempotency import Owned, release

    a = await claim("fence-key-0000002")
    await expire_lease("fence-key-0000002")
    b = await claim("fence-key-0000002")
    assert isinstance(a, Owned) and isinstance(b, Owned)
    assert POOL is not None
    async with POOL.connection() as conn:
        await release(conn, "ACME", "fence-key-0000002", a.token)
    with pytest.raises(ProblemError) as info:
        await claim("fence-key-0000002")  # C: B still owns it
    assert info.value.status == 409


async def test_taking_over_an_expired_lease_still_checks_the_body() -> None:
    """Mutation-found gap (PR #4 review): dropping `request_hash = EXCLUDED.request_hash` from the
    takeover left every test green; it would store body B's quote under body A's hash."""
    await claim("fence-key-0000003")
    await expire_lease("fence-key-0000003")
    with pytest.raises(ProblemError) as info:
        await claim("fence-key-0000003", BODY | {"weight_lb": 999})
    assert info.value.status == 422


async def test_complete_never_overwrites_a_completed_key() -> None:
    """Mutation-found gap: without `status = 'in_progress'` in complete(), all tests passed."""
    from gateway.idempotency import Owned, complete

    owned = await claim("fence-key-0000004")
    assert isinstance(owned, Owned)
    assert POOL is not None
    async with POOL.connection() as conn:
        assert await complete(conn, "ACME", "fence-key-0000004", owned.token, 201, {"q": 1})
        assert not await complete(conn, "ACME", "fence-key-0000004", owned.token, 201, {"q": 2})
        cur = await conn.execute(
            "SELECT response_body FROM idempotency_keys WHERE key = 'fence-key-0000004'"
        )
        assert await cur.fetchone() == ({"q": 1},)


async def test_a_failed_release_does_not_hide_the_real_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    from gateway import idempotency

    async def broken_release(*_args: object) -> None:
        raise psycopg.OperationalError("database went away")

    monkeypatch.setattr(idempotency, "release", broken_release)
    client, _ = api(RetryableError("Server.Busy"))
    async with client:
        r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert r.status_code == 503  # the real outcome, not a 500 from the cleanup
    assert r.json()["type"] == TYPE + "upstream-busy"


async def test_a_cancelled_request_keeps_its_lease() -> None:
    """Unknown outcome (the task was cancelled mid-call): keep the key in_progress, so a retry gets
    409 until the lease expires instead of starting a second upstream call."""
    client, stub = api(RESULT)
    stub.gate = asyncio.Event()  # never set: the upstream call hangs until cancelled
    async with client:
        task = asyncio.create_task(client.post("/v1/rate-quotes", json=BODY, headers=KEY))
        await stub.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert r.status_code == 409


async def test_ops_keys_cannot_quote() -> None:
    client, stub = api(RESULT)
    async with client:
        post = await client.post("/v1/rate-quotes", json=BODY, headers=with_key("k" * 16, OPS_KEY))
        get = await client.get(
            "/v1/rate-quotes/00000000-0000-4000-8000-000000000000", headers={"X-API-Key": OPS_KEY}
        )
    assert post.status_code == get.status_code == 403
    assert post.json()["type"] == TYPE + "forbidden"
    assert stub.calls == 0


async def test_a_starved_pool_is_a_503_not_a_500() -> None:
    from psycopg_pool import PoolTimeout

    client, _ = api(PoolTimeout("couldn't get a connection after 5.00 sec"))
    async with client:
        r = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert (r.status_code, r.headers["Retry-After"]) == (503, "2")
    assert r.json()["type"] == TYPE + "service-unavailable"


async def test_registering_an_existing_key_never_resurrects_or_moves_it() -> None:
    from gateway.auth import KeyExists

    await add_key(TEST_URL, "test-resurrect-key", "BOLT", "shipper", "leaked")
    assert POOL is not None
    async with POOL.connection() as conn:
        await conn.execute("UPDATE api_keys SET revoked_at = now() WHERE label = 'leaked'")
    with pytest.raises(KeyExists):
        await add_key(TEST_URL, "test-resurrect-key", "ACME", "ops", "leaked")
    async with POOL.connection() as conn:
        cur = await conn.execute(
            "SELECT client_id, scope, revoked_at IS NOT NULL FROM api_keys WHERE label = 'leaked'"
        )
        assert await cur.fetchone() == ("BOLT", "shipper", True)  # still dead, still BOLT's


async def test_circuit_open_fast_path_claims_nothing_but_still_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaker open: a new key fails fast with 503 and leaves no row behind; a completed key is
    still replayed; a different body still gets 422 (PR #4 review, M5 latency regression)."""
    from gateway import idempotency

    client, stub = api(RESULT)
    async with client:
        done = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
        monkeypatch.setattr(stub.breaker, "open_for", lambda: 12.3)
        claimed: list[object] = []
        monkeypatch.setattr(idempotency, "begin", lambda *a: claimed.append(a))
        fresh = await client.post(
            "/v1/rate-quotes", json=BODY, headers=with_key("fresh-key-0000001")
        )
        replay = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
        reused = await client.post("/v1/rate-quotes", json=BODY | {"weight_lb": 7}, headers=KEY)
    assert (fresh.status_code, fresh.headers["Retry-After"]) == (503, "13")
    assert fresh.json()["type"] == TYPE + "circuit-open"
    assert claimed == [] and stub.calls == 1  # no claim written, upstream untouched
    assert (replay.status_code, replay.content) == (201, done.content)
    assert reused.status_code == 422
    assert POOL is not None
    async with POOL.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM idempotency_keys WHERE key = 'fresh-key-0000001'"
        )
        assert await cur.fetchone() == (0,)


async def test_revocation_takes_effect_within_the_cache_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented trade-off of the valid-key cache: a key revoked while cached keeps working
    until its entry expires (CACHE_TTL_S), then gets 401."""
    from gateway import auth

    await add_key(TEST_URL, "test-cached-revoked-key", "ACME", "shipper", "cached")
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    headers = {"X-API-Key": "test-cached-revoked-key"}
    unknown = "/v1/rate-quotes/00000000-0000-4000-8000-000000000000"
    client, _ = api(RESULT)
    async with client:
        assert (await client.get(unknown, headers=headers)).status_code == 404  # valid: cached
        assert POOL is not None
        async with POOL.connection() as conn:
            await conn.execute("UPDATE api_keys SET revoked_at = now() WHERE label = 'cached'")
        assert (await client.get(unknown, headers=headers)).status_code == 404  # still cached
        clock[0] += auth.CACHE_TTL_S + 0.1
        assert (await client.get(unknown, headers=headers)).status_code == 401  # expired: gone


async def test_a_stored_quote_emits_one_rate_quote_completed_event(
    ok: tuple[httpx.AsyncClient, Stub],
) -> None:
    """M7 outbox (difference 43): the event rides in the quote's own transaction, once; the
    idempotent replay of the same key emits nothing more (PR #5 review: no test covered it)."""
    client, _ = ok
    assert POOL is not None
    async with POOL.connection() as conn:
        await conn.execute(
            """INSERT INTO webhook_subscriptions
                   (subscription_id, client_id, url, event_types, secret)
               VALUES (gen_random_uuid(), 'ACME', 'https://93.184.216.34/h',
                       '{rate_quote.completed}', 'whsec_dGVzdA==')"""
        )
    first = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    replay = await client.post("/v1/rate-quotes", json=BODY, headers=KEY)
    assert (first.status_code, replay.headers["Idempotent-Replayed"]) == (201, "true")
    async with POOL.connection() as conn:
        cur = await conn.execute("SELECT event_type, payload FROM webhook_deliveries")
        rows = await cur.fetchall()
    assert len(rows) == 1
    event_type, payload = rows[0]
    assert event_type == "rate_quote.completed"
    assert payload["data"]["quote_id"] == first.json()["quote_id"]
