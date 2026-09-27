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


# --- PR #5 review regressions ---------------------------------------------------------------------


async def test_one_bad_receiver_cannot_take_the_batch_down(api_pool: AsyncConnectionPool) -> None:
    """A URL httpx refuses (InvalidURL) and a receiver that makes httpx raise a non-transport
    error (DecodingError) used to escape attempt(), abort gather(), leave the whole batch unrecorded
    and kill the dispatcher; after the lease, the batch was re-sent and it crashed again."""
    await seed(api_pool, url="https://hooks.test/bad\x01url")
    await seed(api_pool, url="https://hooks.test/boom")
    await seed(api_pool, url="https://hooks.test/ok")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/boom":
            raise httpx.DecodingError("bad gzip")
        return httpx.Response(204)

    async with http(handler) as client:
        assert await run_once(api_pool, client) == 3  # did not raise
    async with api_pool.connection() as conn:
        cur = await conn.execute(
            """SELECT s.url, d.status, d.attempts, d.last_result
                 FROM webhook_deliveries d JOIN webhook_subscriptions s USING (subscription_id)"""
        )
        rows = {url.rsplit("/", 1)[1]: rest for url, *rest in await cur.fetchall()}
    assert rows["ok"] == ["delivered", 1, "HTTP 204"]
    assert rows["boom"] == ["pending", 1, "DecodingError"]
    status, attempts, result = rows["bad\x01url"]
    assert (status, attempts) == ("pending", 1) and result.startswith("ssrf:")


async def test_the_response_body_is_never_read(api_pool: AsyncConnectionPool) -> None:
    """A gzip bomb (400 MB from 400 KB) was decompressed into memory before; now the outcome is the
    status code alone, and we ask for no compression at all."""
    await seed(api_pool)
    chunks_read = 0

    class Bomb(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            nonlocal chunks_read
            for _ in range(10_000):
                chunks_read += 1
                yield b"\0" * 65536

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Bomb())

    async with http(handler) as client:
        await run_once(api_pool, client)
    assert chunks_read == 0
    assert seen[0].headers["accept-encoding"] == "identity"
    assert await one(api_pool, "SELECT status FROM webhook_deliveries") == ("delivered",)


async def test_a_stale_outcome_cannot_overwrite_a_newer_one(api_pool: AsyncConnectionPool) -> None:
    """Dispatcher A's lease expires; B re-claims, delivers and records. A's late failure (past max
    age) used to flip the row delivered -> dead and open a dead letter for a delivered event."""
    from gateway.webhooks.dispatcher import Outcome, record

    await seed(api_pool)
    cfg = Settings(**(CFG.model_dump() | {"webhook_max_age": "120s"}))
    async with await AsyncConnection.connect(API_TEST_URL) as conn:
        await conn.execute("UPDATE webhook_deliveries SET window_start = now() - interval '3 min'")
        await conn.commit()
        (job_a,) = await claim(conn, 10)
        await conn.execute("UPDATE webhook_deliveries SET next_attempt_at = now()")  # A's lease...
        await conn.commit()  # ...expires while A is still waiting on a slow endpoint
        (job_b,) = await claim(conn, 10)
        assert await record(conn, job_b, Outcome("delivered", "HTTP 200"), cfg) == "delivered"
        assert "stale" in await record(conn, job_a, Outcome("failed", "HTTP 503"), cfg)
    assert await one(api_pool, "SELECT status FROM webhook_deliveries") == ("delivered",)
    assert await one(api_pool, "SELECT count(*) FROM dead_letters") == (0,)


async def test_a_410_in_a_batch_cancels_its_failing_siblings(api_pool: AsyncConnectionPool) -> None:
    """Default batch size: the siblings are sent concurrently with the 410 (documented), but their
    failures must not retry or dead-letter rows for a gone endpoint."""
    sub = await seed(api_pool, n=4)
    async with api_pool.connection() as conn:
        await conn.execute("UPDATE webhook_deliveries SET window_start = now() - interval '3 min'")
    cfg = Settings(**(CFG.model_dump() | {"webhook_max_age": "120s"}))
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(410 if calls == 1 else 503)

    async with http(handler) as client:
        await run_once(api_pool, client, cfg)
    assert await one(
        api_pool, "SELECT status FROM webhook_subscriptions WHERE subscription_id = %s", sub
    ) == ("disabled",)
    assert await one(api_pool, "SELECT array_agg(DISTINCT status) FROM webhook_deliveries") == (
        ["cancelled"],
    )
    assert await one(api_pool, "SELECT count(*) FROM dead_letters") == (0,)


async def test_a_row_queued_for_a_disabled_subscription_is_never_sent(
    api_pool: AsyncConnectionPool,
) -> None:
    """The outbox INSERT can race a 410 (it saw the subscription active before the 410 committed):
    such a row is cancelled at claim time, not sent to the endpoint that said it is gone."""
    sub = await seed(api_pool)
    async with api_pool.connection() as conn:
        await conn.execute(
            "UPDATE webhook_subscriptions SET status = 'disabled' WHERE subscription_id = %s",
            (sub,),
        )
    sent: list[httpx.Request] = []
    async with http(lambda r: sent.append(r) or httpx.Response(200)) as client:  # type: ignore[func-returns-value]
        await run_once(api_pool, client)
    assert sent == []
    assert await one(api_pool, "SELECT status FROM webhook_deliveries") == ("cancelled",)


async def test_deleting_the_subscription_mid_flight_does_not_crash(
    api_pool: AsyncConnectionPool,
) -> None:
    """DELETE cascades the delivery away while it is in flight; recording a failure past max age
    used to hit a foreign-key violation on the dead-letter INSERT and kill the dispatcher."""
    from gateway.webhooks.dispatcher import Outcome, record

    sub = await seed(api_pool)
    cfg = Settings(**(CFG.model_dump() | {"webhook_max_age": "120s"}))
    async with await AsyncConnection.connect(API_TEST_URL) as conn:
        await conn.execute("UPDATE webhook_deliveries SET window_start = now() - interval '3 min'")
        await conn.commit()
        (job,) = await claim(conn, 10)
        await conn.execute("DELETE FROM webhook_subscriptions WHERE subscription_id = %s", (sub,))
        await conn.commit()
        assert "stale" in await record(conn, job, Outcome("failed", "HTTP 503"), cfg)
    assert await one(api_pool, "SELECT count(*) FROM dead_letters") == (0,)


async def test_the_last_retry_lands_on_the_max_age_not_after_it(
    api_pool: AsyncConnectionPool,
) -> None:
    """A 6 h backoff drawn at hour 71 used to push dead-lettering to ~hour 77."""
    await seed(api_pool)
    async with api_pool.connection() as conn:
        await conn.execute("UPDATE webhook_deliveries SET window_start = now() - interval '110 s'")
    cfg = Settings(**(CFG.model_dump() | {"webhook_max_age": "120s", "webhook_base_delay_s": 600}))
    async with http(lambda _r: httpx.Response(503)) as client:
        await run_once(api_pool, client, cfg)
    status, late = await one(
        api_pool,
        """SELECT status, next_attempt_at > window_start + interval '120 s'
             FROM webhook_deliveries""",
    )
    assert (status, late) == ("pending", False)


def test_the_production_client_does_not_follow_redirects() -> None:
    """The other tests build their own client, so they could not catch a change here."""
    client = dispatcher.http_client(CFG)
    assert client.follow_redirects is False
    assert client.timeout.read == CFG.webhook_timeout_s


# --- M9: the worker loop, the entry point, isolation and the lab CA ----------------------------


async def test_run_once_and_main_deliver_everything_due(
    api_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`python -m gateway.webhooks.dispatcher --once`: drain, publish the backlog gauge, exit."""
    import sys

    await seed(api_pool, n=3)
    monkeypatch.setattr(
        dispatcher, "http_client", lambda _cfg: http(lambda _r: httpx.Response(204))
    )
    await dispatcher.run(CFG, once=True)
    assert await one(
        api_pool, "SELECT count(*) FROM webhook_deliveries WHERE status = 'delivered'"
    ) == (3,)
    await seed(api_pool, n=1)
    monkeypatch.setattr(sys, "argv", ["dispatcher", "--once"])
    monkeypatch.setattr(dispatcher, "get_settings", lambda: CFG)
    await asyncio.to_thread(dispatcher.main)  # asyncio.run in its own thread: a fresh loop
    assert await one(
        api_pool, "SELECT count(*) FROM webhook_deliveries WHERE status = 'delivered'"
    ) == (4,)


async def test_run_logs_and_keeps_going_when_the_database_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead dispatcher stops webhooks for every tenant, so outside --once any error is logged
    and the next cycle tries again; with --once it surfaces."""
    import logging

    down = Settings(database_url="postgresql://gateway:gateway@127.0.0.1:1/none",
                    webhook_poll_interval_s=0.01)  # fmt: skip
    monkeypatch.setattr(
        dispatcher, "http_client", lambda _cfg: http(lambda _r: httpx.Response(204))
    )
    with pytest.raises(Exception):  # noqa: B017 - psycopg's OperationalError, whatever its subclass
        await dispatcher.run(down, once=True)
    retried = asyncio.Event()

    class Count(logging.Handler):
        failures = 0

        def emit(self, record: logging.LogRecord) -> None:
            if "dispatch failed" in record.getMessage():
                Count.failures += 1
                if Count.failures >= 2:
                    retried.set()

    handler = Count()
    logging.getLogger("gateway.webhooks").addHandler(handler)
    task = asyncio.create_task(dispatcher.run(down, once=False))
    try:
        async with asyncio.timeout(10):
            await retried.wait()
    finally:
        task.cancel()
        logging.getLogger("gateway.webhooks").removeHandler(handler)
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_one_failed_record_does_not_lose_the_rest_of_the_batch(
    api_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(api_pool, n=3)
    real = dispatcher.record
    calls = 0

    async def flaky(*args: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("a bug while recording one row")
        return await real(*args)

    monkeypatch.setattr(dispatcher, "record", flaky)
    async with http(lambda _r: httpx.Response(204)) as client:
        assert await run_once(api_pool, client) == 3
    assert await one(
        api_pool, "SELECT count(*) FROM webhook_deliveries WHERE status = 'delivered'"
    ) == (2,)


def test_the_lab_ca_is_trusted_only_when_configured() -> None:
    """The dispatcher adds the lab CA to the system store only when WEBHOOK_CA_BUNDLE is set."""
    from pathlib import Path

    ca = Path(__file__).resolve().parents[2] / "secrets" / "webhook-ca.crt"
    if not ca.exists():
        pytest.skip("no lab CA (make certs)")
    ssl_plain = dispatcher.tls_context(CFG)
    ssl_lab = dispatcher.tls_context(Settings(**(CFG.model_dump() | {"webhook_ca_bundle": ca})))
    assert len(ssl_lab.get_ca_certs()) == len(ssl_plain.get_ca_certs()) + 1
