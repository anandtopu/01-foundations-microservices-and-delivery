"""M8: shipments (FR-3) and the health probes, against conftest's gateway_api_test database.
Needs `make mocks` (the readiness test also probes the SFTP container on localhost:2222)."""

import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

from gateway.app import create_app
from gateway.config import Settings
from tests.integration.conftest import OPS_KEY, OTHER_SHIPPER_KEY, SHIPPER_KEY

pytestmark = pytest.mark.integration
TYPE = "https://errors.meridian-gateway.example/"
ACME, BOLT, OPS = ({"X-API-Key": k} for k in (SHIPPER_KEY, OTHER_SHIPPER_KEY, OPS_KEY))
T0 = datetime(2026, 9, 24, 9, 15, tzinfo=UTC)


def app_client(pool: AsyncConnectionPool, **cfg: object) -> httpx.AsyncClient:
    app = create_app(Settings(**cfg))  # type: ignore[arg-type]
    app.state.db = pool
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://gw.test")


@pytest.fixture
async def client(api_pool: AsyncConnectionPool) -> AsyncIterator[httpx.AsyncClient]:
    """7 ACME shipments one minute apart (two share a timestamp: the tie-break is shipment_id),
    and one BOLT shipment."""
    rows = [(f"SHP{i:03d}", "ACME", T0 + timedelta(minutes=min(i, 5))) for i in range(7)]
    rows.append(("SHPBOLT", "BOLT", T0))
    async with api_pool.connection() as conn:
        for sid, owner, at in rows:
            await conn.execute(
                """INSERT INTO shipments (shipment_id, client_id, order_no, status, ship_date,
                                          weight_lb, source_file, updated_at)
                   VALUES (%s, %s, 'ORD-' || %s, 'in_transit', '2026-09-24', 1260.5, 'f', %s)""",
                (sid, owner, sid, at),
            )
    async with app_client(api_pool) as c:
        yield c


async def all_pages(client: httpx.AsyncClient, query: str) -> list[str]:
    seen: list[str] = []
    url = f"/v1/shipments?{query}"
    while url:
        r = await client.get(url, headers=ACME)
        assert r.status_code == 200, r.text
        page = r.json()
        assert len(page["data"]) <= 200
        seen += [s["shipment_id"] for s in page["data"]]
        url = page["next_cursor"] and f"/v1/shipments?{query}&cursor={page['next_cursor']}"
    return seen


async def test_list_pages_through_own_shipments_in_order(client: httpx.AsyncClient) -> None:
    assert await all_pages(client, "limit=2") == [f"SHP{i:03d}" for i in range(7)]  # no BOLT
    assert await all_pages(client, "limit=200") == [f"SHP{i:03d}" for i in range(7)]


async def test_updated_since_filters_and_binds_the_cursor(client: httpx.AsyncClient) -> None:
    since = (T0 + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    assert await all_pages(client, f"updated_since={since}&limit=1") == ["SHP005", "SHP006"]
    first = (await client.get("/v1/shipments?limit=1", headers=ACME)).json()
    other = await client.get(
        f"/v1/shipments?updated_since={since}&cursor={first['next_cursor']}", headers=ACME
    )
    assert (other.status_code, other.json()["type"]) == (400, TYPE + "invalid-cursor")


@pytest.mark.parametrize(
    "blob",
    [
        {"u": "not-a-date", "i": "SHP001", "f": None},
        {"u": "2026-09-24T09:15:00+00:00", "i": 5, "f": None},
        {"u": "2026-09-24T09:15:00", "i": "SHP001", "f": None},
        [1, 2],
    ],
    ids=["bad_date", "int_id", "naive", "list"],
)
async def test_tampered_cursors_are_400(client: httpx.AsyncClient, blob: object) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps(blob).encode()).decode().rstrip("=")
    r = await client.get(f"/v1/shipments?cursor={cursor}", headers=ACME)
    assert (r.status_code, r.json()["type"]) == (400, TYPE + "invalid-cursor")


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=201",
        "updated_since=yesterday",
        "updated_since=2026-09-24T09:15:00",  # no offset
        "updated_since=0.5",  # lax pydantic read this as a Unix timestamp (Schemathesis, M8)
        "updated_since=1727170500",
        "updated_since=2026-09-24",  # a date, not a date-time
    ],
)
async def test_bad_parameters_are_422(client: httpx.AsyncClient, query: str) -> None:
    r = await client.get(f"/v1/shipments?{query}", headers=ACME)
    assert (r.status_code, r.json()["type"]) == (422, TYPE + "validation-failed")


async def test_get_returns_the_contract_shape_to_its_owner_only(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/shipments/SHP003", headers=ACME)
    assert r.status_code == 200
    assert r.json() == {
        "shipment_id": "SHP003",
        "order_no": "ORD-SHP003",
        "status": "in_transit",
        "ship_date": "2026-09-24",
        "weight_lb": "1260.50",  # a string: no float rounding
        "updated_at": (T0 + timedelta(minutes=3)).isoformat(),
    }
    theirs = await client.get("/v1/shipments/SHPBOLT", headers=ACME)
    unknown = await client.get("/v1/shipments/NOPE", headers=ACME)
    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json()  # BOLA: indistinguishable from "does not exist"
    assert (await client.get("/v1/shipments/SHPBOLT", headers=BOLT)).status_code == 200


async def test_scopes_and_path_validation(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/shipments")).status_code == 401
    assert (await client.get("/v1/shipments", headers=OPS)).status_code == 403
    assert (await client.get("/v1/shipments/-bad", headers=ACME)).status_code == 422
    assert (await client.get(f"/v1/shipments/{'A' * 33}", headers=ACME)).status_code == 422


# --- probes ---------------------------------------------------------------------------------------


async def test_liveness_needs_no_key_and_no_dependency(api_pool: AsyncConnectionPool) -> None:
    async with app_client(api_pool) as c:
        r = await c.get("/healthz")
    assert (r.status_code, r.json()) == (200, {"status": "ok"})


async def test_readiness_reports_each_dependency(api_pool: AsyncConnectionPool) -> None:
    async with app_client(api_pool, sftp_host="localhost", sftp_port=2222) as c:
        ready = await c.get("/readyz")
    assert (ready.status_code, ready.json()) == (
        200,
        {"status": "ready", "db": "ok", "sftp": "ok", "soap_circuit": "closed"},
    )
    async with app_client(api_pool, sftp_host="localhost", sftp_port=1) as c:
        down = await c.get("/readyz")  # nothing listens on port 1
    assert (down.status_code, down.json()["status"], down.json()["sftp"]) == (
        503,
        "not_ready",
        "error",
    )


@pytest.mark.parametrize(
    ("path", "allow"),
    [
        ("/v1/webhook-subscriptions/e3e70682-c209-1cac-a29f-6fbed82c07cd", "GET, DELETE"),
        ("/v1/shipments", "GET"),
        ("/v1/rate-quotes", "POST"),
    ],
)
async def test_405_lists_every_method_of_the_path(
    api_pool: AsyncConnectionPool, path: str, allow: str
) -> None:
    """Starlette's Allow named only the first route matching the path ("GET"), though DELETE is
    served there too (RFC 9110 section 15.5.6; found by Schemathesis in M8)."""
    async with app_client(api_pool) as c:
        r = await c.request("OPTIONS", path, headers=ACME)
    assert (r.status_code, r.headers["Allow"]) == (405, allow)
    assert r.headers["content-type"] == "application/problem+json"
