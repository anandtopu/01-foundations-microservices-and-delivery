"""API-key authentication (contract `apiKey`: header X-API-Key; spec section 9).

Keys are stored as SHA-256 hashes, so the database never holds a usable key. A key maps to one
`client_id` (the shipper, the same value as shipments.client_id) and a scope: `shipper` or `ops`.

    uv run python -m gateway.auth add --client ACME --scope shipper --label "ACME prod 2026-09"

reads the key from the API_KEY environment variable (never from argv, which lands in shell history
and `ps`), or generates a random one and prints it once.
"""

import argparse
import asyncio
import hashlib
import os
import secrets
import sys
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header, Request
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from gateway.config import get_settings
from gateway.errors import ProblemError

Scope = Literal["shipper", "ops"]


@dataclass(frozen=True, slots=True)
class Principal:
    client_id: str
    scope: Scope


def key_hash(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def _unauthorized(detail: str) -> ProblemError:
    return ProblemError(401, "unauthorized", "Missing or unknown API key", detail)


async def principal(
    request: Request, x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None
) -> Principal:
    """FastAPI dependency: who is calling. Runs before body validation, so an anonymous caller
    gets 401, not a 422 that describes our schema."""
    if not x_api_key:
        raise _unauthorized("Send your API key in the X-API-Key header.")
    pool: AsyncConnectionPool = request.app.state.db
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT client_id, scope FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL",
            (key_hash(x_api_key),),
        )
        row = await cur.fetchone()
    if row is None:
        raise _unauthorized("This API key is not valid.")
    return Principal(client_id=row[0], scope=row[1])


async def ops_principal(who: Annotated[Principal, Depends(principal)]) -> Principal:
    """For ops-only endpoints (FR-7): a valid shipper key gets 403, not 401."""
    if who.scope != "ops":
        raise ProblemError(403, "forbidden", "Forbidden", "This endpoint needs an ops API key.")
    return who


async def add_key(database_url: str, key: str, client_id: str, scope: Scope, label: str) -> None:
    async with await AsyncConnection.connect(database_url) as conn:
        await conn.execute(
            """INSERT INTO api_keys (key_hash, client_id, scope, label) VALUES (%s, %s, %s, %s)
               ON CONFLICT (key_hash) DO UPDATE
                 SET client_id = EXCLUDED.client_id, scope = EXCLUDED.scope,
                     label = EXCLUDED.label, revoked_at = NULL""",
            (key_hash(key), client_id, scope, label),
        )
        await conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage gateway API keys")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add (or re-activate) a key; the key comes from $API_KEY")
    add.add_argument("--client", required=True)
    add.add_argument("--scope", choices=["shipper", "ops"], default="shipper")
    add.add_argument("--label", default="")
    args = parser.parse_args()
    key = os.environ.get("API_KEY") or secrets.token_urlsafe(32)
    asyncio.run(add_key(get_settings().database_url, key, args.client, args.scope, args.label))
    shown = key if "API_KEY" not in os.environ else "(from $API_KEY)"
    print(f"added {args.scope} key for {args.client}: {shown}", file=sys.stderr)


if __name__ == "__main__":
    main()
