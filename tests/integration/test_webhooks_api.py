"""M7: webhook subscriptions (FR-5) and the ops dead-letter API (FR-7), against a real database
(conftest's gateway_api_test). Needs `make mocks`."""

import base64
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
