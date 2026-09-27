"""M9: the section 8 metrics are recorded where the spec says. One in-memory SDK reader for the
whole process (the global MeterProvider can be set once); the API's proxy instruments created at
import time forward to it. Needs `make mocks` for the dispatcher and ingest parts."""

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from gateway.config import Settings
from gateway.resilience import Bulkhead, CircuitBreaker, RetryableError
from gateway.webhooks import dispatcher
from tests.integration.conftest import API_TEST_URL

pytestmark = pytest.mark.integration
READER = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[READER]))


def points(name: str) -> list[Any]:
    data = READER.get_metrics_data()
    found: list[Any] = []
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    found += list(m.data.data_points)
    return found


async def test_circuit_state_follows_every_transition() -> None:
    seen: list[int] = []
    breaker = CircuitBreaker(failure_threshold=1, reset_after=0.05)
    seen.append(points("gateway.circuit.state")[-1].value)  # closed at construction

    async def boom() -> None:
        raise RetryableError("Server.Busy")

    async def ok() -> str:
        seen.append(points("gateway.circuit.state")[-1].value)  # during the half-open trial
        return "ok"

    with pytest.raises(RetryableError):
        await breaker.call(boom)
    seen.append(points("gateway.circuit.state")[-1].value)  # open
    await asyncio.sleep(0.06)
    assert await breaker.call(ok) == "ok"
    seen.append(points("gateway.circuit.state")[-1].value)  # closed again
    assert seen == [0, 2, 1, 0]


async def test_upstream_inflight_never_exceeds_the_bulkhead() -> None:
    bulkhead = Bulkhead(size=4, acquire_timeout=1.0)
    peak = 0

    async def call() -> None:
        nonlocal peak
        async with bulkhead.slot():
            peak = max(peak, points("gateway.upstream.inflight")[-1].value)
            await asyncio.sleep(0.02)

    await asyncio.gather(*(call() for _ in range(12)))
    assert peak == 4
    assert points("gateway.upstream.inflight")[-1].value == 0  # all released


async def test_delivery_age_and_dead_letter_backlog(api_pool: AsyncConnectionPool) -> None:
    sub = str(uuid.uuid4())
    async with api_pool.connection() as conn:
        await conn.execute(
            """INSERT INTO webhook_subscriptions
                   (subscription_id, client_id, url, event_types, secret)
               VALUES (%s, 'ACME', 'https://hooks.test/a', '{shipment.created}', %s)""",
            (sub, "whsec_" + "A" * 43 + "="),
        )
        await conn.execute(
            """INSERT INTO webhook_deliveries (subscription_id, event_type, payload, created_at)
               VALUES (%s, 'shipment.created', %s, now() - interval '42 seconds')""",
            (sub, json.dumps({"type": "shipment.created"})),
        )
        cur = await conn.execute(
            """INSERT INTO ingested_files (file_name, size_bytes, sha256, status)
               VALUES ('t.csv', 1, %s, 'done') RETURNING file_id""",
            (uuid.uuid4().bytes * 2,),
        )
        (file_id,) = await cur.fetchone()  # type: ignore[misc]
        await conn.execute(
            """INSERT INTO dead_letters (kind, reason, source, file_id, line_no)
               VALUES ('row', 'q', '{}', %s, 2), ('row', 'q', '{}', %s, 3)""",
            (file_id, file_id),
        )
    cfg = Settings(database_url=API_TEST_URL, webhook_dev_allow_hosts="hooks.test")
    transport = httpx.MockTransport(lambda _r: httpx.Response(204))
    async with (
        httpx.AsyncClient(transport=transport) as client,
        await AsyncConnection.connect(API_TEST_URL) as conn,
    ):
        assert await dispatcher.dispatch_once(conn, client, cfg) == 1
        await dispatcher.publish_dead_letters(conn)
    (age,) = points("gateway.webhook.delivery.age")
    assert age.count >= 1 and 42 <= age.max < 60  # event -> 2xx, from the outbox row's time
    backlog = {p.attributes["kind"]: p.value for p in points("gateway.dead_letters.open")}
    assert backlog == {"row": 2, "webhook": 0}  # kinds with none still report 0
