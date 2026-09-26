"""Postgres access: one async connection pool per process (psycopg 3)."""

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


def make_pool(database_url: str, *, min_size: int = 1, max_size: int = 5) -> AsyncConnectionPool:
    # open=False: the caller opens it inside the running event loop (`await pool.open(wait=True)`),
    # so a bad DSN fails at startup instead of on the first query.
    return AsyncConnectionPool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        # Wait at most 5 s for a free connection (default 30 s): a starved pool must fail fast as
        # a 503, not hold the shipper for half a minute (PR #4 review).
        timeout=5.0,
        kwargs={"autocommit": False},
        # Replace connections Postgres dropped (restart, failover) instead of handing out dead ones.
        check=AsyncConnectionPool.check_connection,
    )


async def connect(database_url: str) -> AsyncConnection:
    """A single connection, for one-shot tools (migrate) and tests."""
    return await AsyncConnection.connect(database_url)
