"""M7: webhook subscriptions (FR-5) and the ops dead-letter API (FR-7), against a real database
(conftest's gateway_api_test). Needs `make mocks`."""

import asyncio
import base64
import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from gateway.app import create_app
from gateway.config import Settings
from tests.integration.conftest import OPS_KEY, OTHER_SHIPPER_KEY, SHIPPER_KEY

pytestmark = pytest.mark.integration
TYPE = "https://errors.meridian-gateway.example/"
ACME, BOLT, OPS = ({"X-API-Key": k} for k in (SHIPPER_KEY, OTHER_SHIPPER_KEY, OPS_KEY))
PUBLIC_URL = "https://93.184.216.34/hooks/acme"  # a public IP literal: no DNS needed


@pytest.fixture
async def client(api_pool: AsyncConnectionPool) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(webhook_dev_allow_hosts=""))
    app.state.db = api_pool
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as c:
        c.pool = api_pool  # type: ignore[attr-defined]
        yield c


async def create(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await client.post(
        "/v1/webhook-subscriptions",
        json={"url": PUBLIC_URL, "event_types": ["shipment.created"]} | body,
        headers=ACME,
    )


# --- subscriptions -------------------------------------------------------------------------------


async def test_create_returns_the_secret_once(client: httpx.AsyncClient) -> None:
    r = await create(client)
    assert r.status_code == 201
    sub = r.json()
    assert r.headers["Location"] == f"/v1/webhook-subscriptions/{sub['subscription_id']}"
    assert sub["status"] == "active"
    assert sub["secret"].startswith("whsec_")
    assert len(base64.b64decode(sub["secret"].removeprefix("whsec_"))) == 32  # 256-bit key
    again = await client.get(r.headers["Location"], headers=ACME)
    assert again.status_code == 200
    assert "secret" not in again.json()  # never again


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://10.1.2.3/",
        "https://[::1]/",
        "https://localhost:9000/webhooks/acme",  # no dev allowance configured in this app
        "https://no-such-host.invalid/hook",  # does not resolve at all
    ],
)
async def test_create_refuses_non_public_targets(client: httpx.AsyncClient, url: str) -> None:
    r = await create(client, url=url)
    assert r.status_code == 422
    assert r.json()["type"] == TYPE + "webhook-url-not-allowed"


@pytest.mark.parametrize(
    "body",
    [
        {"url": "http://93.184.216.34/"},  # contract pattern ^https://
        {"event_types": []},
        {"event_types": ["shipment.created", "shipment.created"]},  # uniqueItems
        {"event_types": ["shipment.deleted"]},
        {"extra": 1},
    ],
    ids=["http", "empty", "duplicate", "unknown_type", "extra_field"],
)
async def test_create_validates_the_contract(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    assert (await create(client, **body)).status_code == 422


async def test_subscriptions_are_private_and_shipper_only(client: httpx.AsyncClient) -> None:
    location = (await create(client)).headers["Location"]
    assert (await client.get(location, headers=BOLT)).status_code == 404  # BOLA
    assert (await client.delete(location, headers=BOLT)).status_code == 404
    assert (await client.get(location, headers=OPS)).status_code == 403  # ops keys don't subscribe
    assert (await client.get(location, headers=ACME)).status_code == 200


async def test_delete_removes_pending_deliveries(client: httpx.AsyncClient) -> None:
    sub = (await create(client)).json()
    pool: AsyncConnectionPool = client.pool  # type: ignore[attr-defined]
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO webhook_deliveries (subscription_id, event_type, payload)"
            " VALUES (%s, 'x', '{}')",
            (sub["subscription_id"],),
        )
    r = await client.delete(f"/v1/webhook-subscriptions/{sub['subscription_id']}", headers=ACME)
    assert r.status_code == 204
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM webhook_deliveries")
        assert await cur.fetchone() == (0,)  # nothing is sent to a deleted endpoint


# --- dead letters --------------------------------------------------------------------------------


async def row_dead_letter(pool: AsyncConnectionPool, raw: str, reason: str) -> str:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO ingested_files (file_name, size_bytes, sha256, status)
               VALUES (%s, 1, %s, 'done') RETURNING file_id""",
            (f"f-{uuid.uuid4()}.csv", uuid.uuid4().bytes * 2),
        )
        (file_id,) = await cur.fetchone()  # type: ignore[misc]
        cur = await conn.execute(
            """INSERT INTO dead_letters (kind, reason, source, file_id, line_no)
               VALUES ('row', %s, %s, %s, 5) RETURNING dead_letter_id""",
            (reason, Jsonb({"file_name": "f.csv", "line_no": 5, "raw": raw}), file_id),
        )
        (dl_id,) = await cur.fetchone()  # type: ignore[misc]
    return str(dl_id)


async def audit(pool: AsyncConnectionPool, dl_id: str) -> list[tuple[str, str]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT ops_client_id, outcome FROM dead_letter_replays WHERE dead_letter_id = %s"
            " ORDER BY replay_id",
            (dl_id,),
        )
        return await cur.fetchall()


async def test_dead_letters_are_ops_only(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/dead-letters", headers=ACME)).status_code == 403
    assert (await client.get("/v1/dead-letters")).status_code == 401
    assert (await client.get("/v1/dead-letters", headers=OPS)).status_code == 200


async def test_list_filters_and_paginates_with_a_bound_cursor(client: httpx.AsyncClient) -> None:
    pool: AsyncConnectionPool = client.pool  # type: ignore[attr-defined]
    ids = [
        await row_dead_letter(pool, f'"S{i}","O","ACME","Q",1260924,1.00', "q") for i in range(5)
    ]
    first = (await client.get("/v1/dead-letters?kind=row&limit=2", headers=OPS)).json()
    assert len(first["data"]) == 2 and first["next_cursor"]
    seen = [d["dead_letter_id"] for d in first["data"]]
    cursor = first["next_cursor"]
    while cursor:
        page = (
            await client.get(f"/v1/dead-letters?kind=row&limit=2&cursor={cursor}", headers=OPS)
        ).json()
        seen += [d["dead_letter_id"] for d in page["data"]]
        cursor = page["next_cursor"]
    assert sorted(seen) == sorted(ids)  # every item exactly once
    item = first["data"][0]
    assert set(item) >= {"dead_letter_id", "kind", "reason", "source", "created_at", "resolved"}
    other_filter = await client.get(
        f"/v1/dead-letters?kind=webhook&cursor={first['next_cursor']}", headers=OPS
    )
    assert other_filter.status_code == 400  # a cursor is bound to its filter
    assert (await client.get("/v1/dead-letters?cursor=%25%25", headers=OPS)).status_code == 400


async def test_replay_row_applies_once_it_validates(client: httpx.AsyncClient) -> None:
    pool: AsyncConnectionPool = client.pool  # type: ignore[attr-defined]
    good = await row_dead_letter(pool, '"SHPR1","ORD-1","ACME","P",1260924,  10.00', "was bad")
    bad = await row_dead_letter(pool, '"SHPR2","ORD-2","ACME","Q",1260924,  10.00', "status Q")
    ok = await client.post(f"/v1/dead-letters/{good}:replay", headers=OPS)
    assert (ok.status_code, ok.json()["outcome"]) == (200, "applied")
    still_bad = await client.post(f"/v1/dead-letters/{bad}:replay", headers=OPS)
    assert still_bad.status_code == 422
    assert "unknown status code 'Q'" in still_bad.json()["detail"]
    again = await client.post(f"/v1/dead-letters/{good}:replay", headers=OPS)
    assert again.status_code == 409  # already resolved
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT client_id, status FROM shipments WHERE shipment_id = 'SHPR1'"
        )
        assert await cur.fetchone() == ("ACME", "picked")
    # Every attempt audited, including the failures (section 9, Repudiation).
    assert await audit(pool, good) == [
        ("meridian-ops", "applied"),
        ("meridian-ops", "already_resolved"),
    ]
    assert await audit(pool, bad) == [("meridian-ops", "rejected: status: unknown status code 'Q'")]


async def test_replay_webhook_requeues_the_same_delivery(client: httpx.AsyncClient) -> None:
    pool: AsyncConnectionPool = client.pool  # type: ignore[attr-defined]
    sub = (await create(client)).json()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO webhook_deliveries
                   (subscription_id, event_type, payload, status, attempts)
               VALUES (%s, 'shipment.created', '{}', 'dead', 9) RETURNING delivery_id""",
            (sub["subscription_id"],),
        )
        (delivery_id,) = await cur.fetchone()  # type: ignore[misc]
        cur = await conn.execute(
            """INSERT INTO dead_letters (kind, reason, source, delivery_id)
               VALUES ('webhook', 'max age exceeded', %s, %s) RETURNING dead_letter_id""",
            (Jsonb({"subscription_id": sub["subscription_id"], "attempts": 9}), delivery_id),
        )
        (dl_id,) = await cur.fetchone()  # type: ignore[misc]
    r = await client.post(f"/v1/dead-letters/{dl_id}:replay", headers=OPS)
    assert (r.status_code, r.json()["outcome"]) == (202, "queued")
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, attempts, delivery_id FROM webhook_deliveries WHERE delivery_id = %s",
            (delivery_id,),
        )
        assert await cur.fetchone() == ("pending", 0, delivery_id)  # same webhook-id
        cur = await conn.execute(
            "SELECT resolved_at IS NULL, replay_count FROM dead_letters WHERE dead_letter_id = %s",
            (dl_id,),
        )
        assert await cur.fetchone() == (True, 1)  # resolved only once actually delivered


async def test_replay_unknown_dead_letter_is_404(client: httpx.AsyncClient) -> None:
    r = await client.post(f"/v1/dead-letters/{uuid.uuid4()}:replay", headers=OPS)
    assert r.status_code == 404


# --- PR #5 review regressions ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://93.184.216.34/hook\x01x",  # 201 before, then crashed the dispatcher forever
        "https://1.1.1.1\t/",
        "https://[64:ff9b::a9fe:a9fe]/",  # the metadata service via NAT64
        "https://[::127.0.0.1]/",
        "https://user:pw@93.184.216.34/",
    ],
    ids=["ctrl", "tab", "nat64_metadata", "ipv4_compatible", "userinfo"],
)
async def test_create_refuses_urls_that_slipped_through(
    client: httpx.AsyncClient, url: str
) -> None:
    r = await create(client, url=url)
    assert (r.status_code, r.json()["type"]) == (422, TYPE + "webhook-url-not-allowed")


async def test_refusals_do_not_reveal_our_dns(client: httpx.AsyncClient) -> None:
    """The detail used to say "resolves to non-public address 10.1.2.3" or "[Errno -2] Name or
    service not known": a shipper could map internal names. Every refusal now reads the same."""
    details = {
        (await create(client, url=url)).json()["detail"]
        for url in ("https://10.1.2.3/", "https://no-such-host.invalid/", "https://localhost/")
    }
    assert len(details) == 1
    assert "10.1.2.3" not in details.pop()


async def test_the_secret_response_is_not_cacheable(client: httpx.AsyncClient) -> None:
    assert (await create(client)).headers["Cache-Control"] == "no-store"


async def test_subscriptions_per_shipper_are_capped(client: httpx.AsyncClient) -> None:
    from gateway.api.webhooks import MAX_SUBSCRIPTIONS

    for _ in range(MAX_SUBSCRIPTIONS):
        assert (await create(client)).status_code == 201
    r = await create(client)
    assert (r.status_code, r.json()["type"]) == (422, TYPE + "subscription-limit-reached")
    other = await client.post(
        "/v1/webhook-subscriptions",
        json={"url": PUBLIC_URL, "event_types": ["shipment.created"]},
        headers=BOLT,
    )
    assert other.status_code == 201  # the cap is per shipper


async def test_list_puts_unresolved_first_across_pages(client: httpx.AsyncClient) -> None:
    """Contract: "Unresolved first, newest first within that" (it was newest first only)."""
    pool: AsyncConnectionPool = client.pool  # type: ignore[attr-defined]
    ids = [
        await row_dead_letter(pool, f'"S{i}","O","ACME","Q",1260924,1.00', "q") for i in range(5)
    ]
    async with pool.connection() as conn:  # resolve the two NEWEST
        await conn.execute(
            "UPDATE dead_letters SET resolved_at = now() WHERE dead_letter_id = ANY(%s)",
            (ids[3:],),
        )
    seen: list[tuple[bool, str]] = []
    url = "/v1/dead-letters?limit=2"
    while url:
        page = (await client.get(url, headers=OPS)).json()
        seen += [(d["resolved"], d["dead_letter_id"]) for d in page["data"]]
        url = page["next_cursor"] and f"/v1/dead-letters?limit=2&cursor={page['next_cursor']}"
    assert seen == [
        (False, ids[2]),
        (False, ids[1]),
        (False, ids[0]),
        (True, ids[4]),
        (True, ids[3]),
    ]


@pytest.mark.parametrize(
    "blob",
    [
        {"r": False, "c": "not-a-date", "i": str(uuid.uuid4())},
        {"r": False, "c": "2026-09-26T20:00:00+00:00", "i": 5},
        {"r": "no", "c": "2026-09-26T20:00:00+00:00", "i": str(uuid.uuid4())},
        {"r": False, "c": "2026-09-26T20:00:00", "i": str(uuid.uuid4())},  # naive
        [1, 2],
    ],
    ids=["bad_date", "int_id", "str_flag", "naive_date", "list"],
)
async def test_tampered_cursors_are_400_not_500(client: httpx.AsyncClient, blob: object) -> None:
    import json

    if isinstance(blob, dict):
        blob |= {"f": {"kind": None, "resolved": None}}
    cursor = base64.urlsafe_b64encode(json.dumps(blob).encode()).decode().rstrip("=")
    r = await client.get(f"/v1/dead-letters?cursor={cursor}", headers=OPS)
    assert (r.status_code, r.json()["type"]) == (400, TYPE + "invalid-cursor")


async def webhook_dead_letter(client: httpx.AsyncClient) -> tuple[str, str, str]:
    pool: AsyncConnectionPool = client.pool  # type: ignore[attr-defined]
    sub = (await create(client)).json()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO webhook_deliveries (subscription_id, event_type, payload, status)
               VALUES (%s, 'shipment.created', '{}', 'dead') RETURNING delivery_id""",
            (sub["subscription_id"],),
        )
        (delivery_id,) = await cur.fetchone()  # type: ignore[misc]
        cur = await conn.execute(
            """INSERT INTO dead_letters (kind, reason, source, delivery_id)
               VALUES ('webhook', 'max age', '{}', %s) RETURNING dead_letter_id""",
            (delivery_id,),
        )
        (dl_id,) = await cur.fetchone()  # type: ignore[misc]
    return str(dl_id), str(delivery_id), sub["subscription_id"]


async def test_a_second_replay_does_not_break_a_live_lease(client: httpx.AsyncClient) -> None:
    """Replay -> a dispatcher claims it (lease) -> replay again. The second replay used to reset
    next_attempt_at, so a second dispatcher sent the same row concurrently."""
    from psycopg import AsyncConnection

    from gateway.webhooks.dispatcher import claim
    from tests.integration.conftest import API_TEST_URL

    dl_id, _, _ = await webhook_dead_letter(client)
    assert (await client.post(f"/v1/dead-letters/{dl_id}:replay", headers=OPS)).status_code == 202
    async with await AsyncConnection.connect(API_TEST_URL) as conn:
        assert len(await claim(conn, 10)) == 1  # dispatcher A holds the lease
        again = await client.post(f"/v1/dead-letters/{dl_id}:replay", headers=OPS)
        assert (again.status_code, again.json()["type"]) == (409, TYPE + "already-queued")
        assert await claim(conn, 10) == []  # dispatcher B gets nothing
    pool: AsyncConnectionPool = client.pool  # type: ignore[attr-defined]
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT outcome, ops_key_id FROM dead_letter_replays WHERE dead_letter_id = %s"
            " ORDER BY replay_id",
            (dl_id,),
        )
        rows = await cur.fetchall()
    key_id = hashlib.sha256(OPS_KEY.encode()).hexdigest()[:16]
    assert rows == [("queued", key_id), ("already_queued", key_id)]  # WHICH key, not just client


async def test_replaying_for_a_disabled_subscription_is_rejected_and_audited(
    client: httpx.AsyncClient,
) -> None:
    pool: AsyncConnectionPool = client.pool  # type: ignore[attr-defined]
    dl_id, _, sub = await webhook_dead_letter(client)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE webhook_subscriptions SET status = 'disabled' WHERE subscription_id = %s",
            (sub,),
        )
    r = await client.post(f"/v1/dead-letters/{dl_id}:replay", headers=OPS)
    assert (r.status_code, r.json()["detail"]) == (422, "the subscription is disabled")
    assert await audit(pool, dl_id) == [("meridian-ops", "rejected: the subscription is disabled")]
    assert (await client.post(f"/v1/dead-letters/{dl_id}:replay", headers=ACME)).status_code == 403


async def test_a_replay_racing_the_poller_cannot_take_another_shippers_shipment(
    client: httpx.AsyncClient,
) -> None:
    """The poller's batch inserts S1 for ACME (uncommitted); an ops replay of a BOLT line for S1
    passes its OWNERS check (no row yet), waits on the insert, then used to take the DO UPDATE
    path: ACME's shipment got BOLT's data, and ACME a webhook carrying it."""
    from psycopg import AsyncConnection

    from gateway.api.dead_letters import replay_row
    from tests.integration.conftest import API_TEST_URL

    acme = {"raw": '"SRACE","ACME-ORD","ACME","P",1260924,  10.00', "line_no": 1}
    bolt = {"raw": '"SRACE","BOLT-SECRET","BOLT","D",1260924,  99.00', "line_no": 1}
    async with (
        await AsyncConnection.connect(API_TEST_URL) as poller,
        await AsyncConnection.connect(API_TEST_URL) as ops,
    ):

        async def ops_replay() -> str:
            async with ops.transaction():
                return await replay_row(ops, uuid.uuid4(), bolt)

        async with poller.transaction():
            assert await replay_row(poller, uuid.uuid4(), acme) == "applied"
            racing = asyncio.create_task(ops_replay())
            await asyncio.sleep(0.3)  # the replay is now blocked on the poller's insert
            assert not racing.done()
        outcome = await asyncio.wait_for(racing, 5)  # the poller committed; the replay proceeds
    assert outcome == "rejected: owner change refused: 'ACME' -> 'BOLT'"
    pool: AsyncConnectionPool = client.pool  # type: ignore[attr-defined]
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT client_id, order_no, status FROM shipments WHERE shipment_id = 'SRACE'"
        )
        assert await cur.fetchone() == ("ACME", "ACME-ORD", "picked")
