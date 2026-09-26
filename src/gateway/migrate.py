"""Apply `migrations/NNNN_*.sql` in order, each exactly once: `python -m gateway.migrate`.

Rules (spec section 6): migrations are additive only, so rolling the image back never needs a down
migration. Each file runs in its own transaction together with its `schema_migrations` row, so a
failure leaves no half-applied file. An advisory lock makes concurrent runs (two replicas starting
at once) take turns instead of racing.
"""

import asyncio
import hashlib
import logging
import sys
from pathlib import Path

from psycopg import AsyncConnection, sql

from gateway.config import get_settings

log = logging.getLogger("gateway.migrate")

LOCK_KEY = 0x5E1D_0001  # any constant shared by every migrator of this database


async def applied(conn: AsyncConnection) -> dict[str, str]:
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version    text        PRIMARY KEY,
               checksum   text        NOT NULL,
               applied_at timestamptz NOT NULL DEFAULT now())"""
    )
    cur = await conn.execute("SELECT version, checksum FROM schema_migrations")
    return {v: c for v, c in await cur.fetchall()}


def load(migrations_dir: Path) -> list[tuple[str, str]]:
    """(version, sql) per migration file, in order. Sync on purpose: runs before the loop."""
    files = sorted(migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        raise SystemExit(f"no migrations found in {migrations_dir.resolve()}")
    return [(p.stem, p.read_text(encoding="utf-8")) for p in files]


async def migrate(database_url: str, migrations: list[tuple[str, str]]) -> list[str]:
    """Apply pending migrations; return the versions applied by this call."""
    done: list[str] = []
    async with await AsyncConnection.connect(database_url, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            seen = await applied(conn)
            for version, body in migrations:
                checksum = hashlib.sha256(body.encode()).hexdigest()
                if version in seen:
                    # An applied migration must never change: fix forward with a new file instead.
                    if seen[version] != checksum:
                        raise SystemExit(f"{version} was edited after it was applied")
                    continue
                async with conn.transaction():
                    await conn.execute(sql.SQL(body))  # trusted repo file, never user input
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (version, checksum),
                    )
                log.info("applied %s", version)
                done.append(version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return done


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = get_settings()
    done = asyncio.run(migrate(cfg.database_url, load(cfg.migrations_dir)))
    print(f"applied {len(done)} migration(s): {', '.join(done) or 'none pending'}")
    sys.exit(0)


if __name__ == "__main__":
    main()
