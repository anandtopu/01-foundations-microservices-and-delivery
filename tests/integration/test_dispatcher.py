"""M7: the webhook dispatcher against a real database (conftest's gateway_api_test) and an
in-process HTTP transport. Needs `make mocks`."""

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from gateway.config import Settings
from gateway.webhooks import dispatcher
from gateway.webhooks.dispatcher import backoff_s, claim, dispatch_once
from gateway.webhooks.signing import verify
from tests.integration.conftest import API_TEST_URL

pytestmark = pytest.mark.integration
SECRET = "whsec_c2VjcmV0LWtleS1mb3ItdGVzdHMtMDAwMDAwMDA="  # noqa: S105 - test key
CFG = Settings(
    database_url=API_TEST_URL, webhook_dev_allow_hosts="hooks.test", webhook_max_age="72h"
)


async def seed(pool: AsyncConnectionPool, url: str = "https://hooks.test/acme", n: int = 1) -> str:
    sub_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO webhook_subscriptions
                   (subscription_id, client_id, url, event_types, secret)
               VALUES (%s, 'ACME', %s, '{shipment.created}', %s)""",
            (sub_id, url, SECRET),
        )
        for i in range(n):
            await conn.execute(
                """INSERT INTO webhook_deliveries (subscription_id, event_type, payload)
                   VALUES (%s, 'shipment.created', %s)""",
                (sub_id, json.dumps({"type": "shipment.created", "data": {"i": i, "é": "ñ"}})),
            )
    return sub_id


def http(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def one(pool: AsyncConnectionPool, sql: str, *args: object) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchone()


async def run_once(
    pool: AsyncConnectionPool, client: httpx.AsyncClient, cfg: Settings = CFG
) -> int:
    async with await AsyncConnection.connect(API_TEST_URL) as conn:
        return await dispatch_once(conn, client, cfg)


async def test_2xx_is_delivered_and_correctly_signed(api_pool: AsyncConnectionPool) -> None:
    await seed(api_pool)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    async with http(handler) as client:
        assert await run_once(api_pool, client) == 1
    (request,) = seen
    headers = {
        k: request.headers[k] for k in ("webhook-id", "webhook-timestamp", "webhook-signature")
    }
    assert verify(SECRET, headers, request.content)  # the reference verifier accepts it
    assert json.loads(request.content)["data"]["é"] == "ñ"
    status, attempts, result = await one(
        api_pool, "SELECT status, attempts, last_result FROM webhook_deliveries"
    )
    assert (status, attempts, result) == ("delivered", 1, "HTTP 204")
    assert headers["webhook-id"] == str(
        (await one(api_pool, "SELECT delivery_id FROM webhook_deliveries"))[0]
    )


async def test_410_disables_the_subscription_and_cancels_its_queue(
    api_pool: AsyncConnectionPool,
) -> None:
    sub = await seed(api_pool, n=3)
    calls = 0

    def gone(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(410)

    CFG_ONE = Settings(**(CFG.model_dump() | {"webhook_batch_size": 1}))
    async with http(gone) as client:
        await run_once(api_pool, client, CFG_ONE)
        assert await run_once(api_pool, client, CFG_ONE) == 0  # nothing left to send
    assert calls == 1  # one 410 is enough: the other two were never sent
    assert await one(
        api_pool, "SELECT status FROM webhook_subscriptions WHERE subscription_id = %s", sub
    ) == ("disabled",)
    assert await one(
        api_pool, "SELECT count(*) FROM webhook_deliveries WHERE status = 'cancelled'"
    ) == (3,)


@pytest.mark.parametrize(
    "response",
    [
        lambda _r: httpx.Response(500),
        lambda _r: httpx.Response(302, headers={"Location": "https://169.254.169.254/"}),
        lambda _r: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    ],
    ids=["500", "redirect_not_followed", "refused"],
)
async def test_failures_are_retried_with_full_jitter(
    api_pool: AsyncConnectionPool, response: Callable[[httpx.Request], httpx.Response]
) -> None:
    await seed(api_pool)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return response(request)

    async with http(handler) as client:
        await run_once(api_pool, client)
    assert requests == ["https://hooks.test/acme"]  # exactly one request: no redirect followed
    status, attempts, delay = await one(
        api_pool,
        """SELECT status, attempts, extract(epoch FROM next_attempt_at - last_attempt_at)
             FROM webhook_deliveries""",
    )
    assert (status, attempts) == ("pending", 1)
    assert 0 <= float(delay) <= 30.5  # first retry: uniform in [0, 30 s]


async def test_a_slow_endpoint_hits_the_total_timeout(api_pool: AsyncConnectionPool) -> None:
    await seed(api_pool)

    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    cfg = Settings(**(CFG.model_dump() | {"webhook_timeout_s": 0.2}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        await run_once(api_pool, client, cfg)
    assert (await one(api_pool, "SELECT last_result FROM webhook_deliveries"))[
        0
    ] == "timeout after 0.2 s"


def test_backoff_doubles_from_30s_to_a_6h_cap() -> None:
    for attempts, ceiling in [(0, 30), (1, 60), (4, 480), (9, 15360), (10, 21600), (30, 21600)]:
        draws = [backoff_s(attempts, CFG) for _ in range(300)]
        assert all(0 <= d <= ceiling for d in draws)
        assert max(draws) > ceiling * 0.8  # really spread up to the ceiling...
        assert len(set(draws)) == 300  # ...and never synchronised


async def test_ssrf_is_checked_again_at_send_time(api_pool: AsyncConnectionPool) -> None:
    """A subscription row pointing inside the network (e.g. DNS changed after creation, or a row
    written around the API) never gets a request."""
    await seed(api_pool, url="https://127.0.0.1/steal")
    sent: list[httpx.Request] = []
    async with http(lambda r: sent.append(r) or httpx.Response(200)) as client:  # type: ignore[func-returns-value]
        await run_once(api_pool, client)
    assert sent == []
    assert (await one(api_pool, "SELECT last_result FROM webhook_deliveries"))[0].startswith(
        "ssrf:"
    )


async def test_max_age_dead_letters_and_replay_delivers(api_pool: AsyncConnectionPool) -> None:
    """The M7 gate in miniature: past WEBHOOK_MAX_AGE a failing delivery becomes a webhook dead
    letter; ops replay re-queues the SAME delivery; once delivered, the dead letter is resolved."""
    await seed(api_pool)
    async with api_pool.connection() as conn:
        await conn.execute(
            "UPDATE webhook_deliveries SET window_start = now() - interval '3 minutes'"
        )
    cfg = Settings(**(CFG.model_dump() | {"webhook_max_age": "120s"}))
    async with http(lambda _r: httpx.Response(503)) as client:
        await run_once(api_pool, client, cfg)
    status, attempts = await one(api_pool, "SELECT status, attempts FROM webhook_deliveries")
    assert (status, attempts) == ("dead", 1)
    reason, source, resolved = await one(
        api_pool,
        "SELECT reason, source, resolved_at IS NOT NULL FROM dead_letters WHERE kind = 'webhook'",
    )
    assert reason == "max age 120s exceeded (last: HTTP 503)"
    assert (source["event_type"], source["attempts"], resolved) == ("shipment.created", 1, False)

    from gateway.api.dead_letters import replay_webhook

    delivery_id = (await one(api_pool, "SELECT delivery_id FROM webhook_deliveries"))[0]
    async with api_pool.connection() as conn:
        assert await replay_webhook(conn, delivery_id) == "queued"
    async with http(lambda _r: httpx.Response(200)) as client:
        await run_once(api_pool, client, cfg)
    assert await one(api_pool, "SELECT status FROM webhook_deliveries") == ("delivered",)
    assert await one(api_pool, "SELECT resolved_at IS NOT NULL FROM dead_letters") == (True,)


async def test_concurrent_dispatchers_never_claim_the_same_row(
    api_pool: AsyncConnectionPool,
) -> None:
    await seed(api_pool, n=40)
    async with (
        await AsyncConnection.connect(API_TEST_URL) as a,
        await AsyncConnection.connect(API_TEST_URL) as b,
    ):
        first, second = await asyncio.gather(claim(a, 25), claim(b, 25))
    ids_a, ids_b = {j.delivery_id for j in first}, {j.delivery_id for j in second}
    assert not ids_a & ids_b  # SKIP LOCKED: disjoint
    assert len(ids_a | ids_b) == 40  # and between them, everything due


async def test_a_crashed_dispatchers_claim_expires(
    api_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At-least-once: a dispatcher that claimed a row and died leaves a 60 s lease, after which
    another dispatcher sends it (receivers de-duplicate on webhook-id)."""
    await seed(api_pool)
    async with await AsyncConnection.connect(API_TEST_URL) as conn:
        assert len(await claim(conn, 10)) == 1  # ...and then the process dies
        assert await claim(conn, 10) == []  # still leased
        await conn.execute("UPDATE webhook_deliveries SET next_attempt_at = now() - interval '1 s'")
        await conn.commit()
        assert len(await claim(conn, 10)) == 1  # lease expired: claimable again
    assert dispatcher.CLAIM_LEASE_S > CFG.webhook_timeout_s


async def test_a_second_dispatcher_does_not_wait_for_rows_being_claimed(
    api_pool: AsyncConnectionPool,
) -> None:
    """SKIP LOCKED, not just FOR UPDATE: while one replica's claim is still open, another takes the
    other due rows at once instead of queueing behind the lock (disjointness alone would also hold
    with a plain FOR UPDATE, because the lease is re-checked after the wait)."""
    await seed(api_pool, n=10)
    async with (
        await AsyncConnection.connect(API_TEST_URL) as a,
        await AsyncConnection.connect(API_TEST_URL) as b,
    ):
        async with a.transaction():  # replica A has locked 4 rows and not committed yet
            await a.execute(dispatcher.CLAIM, {"n": 4, "lease": dispatcher.CLAIM_LEASE_S})
            async with asyncio.timeout(2):
                taken = await claim(b, 50)
        assert len(taken) == 6
