"""Shared fixture for the API integration tests: a migrated, throwaway gateway_api_test database
with three API keys (ACME and BOLT shippers, one ops key), emptied before every test."""

import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from gateway.auth import add_key, forget_cached_keys
from gateway.db import make_pool
from gateway.migrate import load, migrate

ADMIN_URL = "postgresql://gateway:gateway@localhost:5432/gateway"
API_TEST_URL = "postgresql://gateway:gateway@localhost:5432/gateway_api_test"
SHIPPER_KEY, OTHER_SHIPPER_KEY, OPS_KEY = "test-acme-key", "test-bolt-key", "test-ops-key"
_migrated = False


@pytest.fixture
async def api_pool() -> AsyncIterator[AsyncConnectionPool]:
    global _migrated
    with socket.socket() as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", 5432)) != 0:
            pytest.skip("postgres not running (make mocks)")
    if not _migrated:
        async with await AsyncConnection.connect(ADMIN_URL, autocommit=True) as admin:
            await admin.execute("DROP DATABASE IF EXISTS gateway_api_test WITH (FORCE)")
            await admin.execute("CREATE DATABASE gateway_api_test")
        await migrate(API_TEST_URL, load(Path(__file__).parents[2] / "migrations"))
        await add_key(API_TEST_URL, SHIPPER_KEY, "ACME", "shipper", "test")
        await add_key(API_TEST_URL, OTHER_SHIPPER_KEY, "BOLT", "shipper", "test")
        await add_key(API_TEST_URL, OPS_KEY, "meridian-ops", "ops", "test")
        _migrated = True
    async with await AsyncConnection.connect(API_TEST_URL, autocommit=True) as conn:
        await conn.execute(
            """TRUNCATE idempotency_keys, rate_quotes, webhook_deliveries, webhook_subscriptions,
                        dead_letter_replays, dead_letters, shipments, ingested_files CASCADE"""
        )
    forget_cached_keys()  # each test sees the database's current keys
    pool = make_pool(API_TEST_URL)
    await pool.open(wait=True)
    try:
        yield pool
    finally:
        await pool.close()
