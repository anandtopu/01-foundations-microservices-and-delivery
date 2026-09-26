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
import time
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header, Request
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from gateway.config import get_settings
from gateway.errors import ProblemError, reject_unknown_query

Scope = Literal["shipper", "ops"]


@dataclass(frozen=True, slots=True)
class Principal:
    client_id: str
    scope: Scope
    key_id: str = ""  # first 16 hex chars of sha256(key): names WHICH key acted, in audit trails


def key_hash(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


# A short-lived cache of VALID keys: hot callers skip one database round trip per request (it kept
# the M5 "circuit-open 503 in under 10 ms" promise after M6 put auth in front of the breaker).
# Trade-off: a revoked key keeps working for up to CACHE_TTL_S. Unknown keys are never cached, so
# a flood of bad keys cannot grow it; the size cap bounds it anyway.
CACHE_TTL_S = 10.0
CACHE_MAX = 1000
_cache: dict[bytes, tuple[float, Principal]] = {}


def forget_cached_keys() -> None:
    """Drop the cache (tests; an ops "revoke now" hook would call this on every replica)."""
    _cache.clear()


def _unauthorized(detail: str) -> ProblemError:
    return ProblemError(401, "unauthorized", "Missing or unknown API key", detail)


async def principal(
    request: Request, x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None
) -> Principal:
    """FastAPI dependency: who is calling. Runs before schema validation, so an anonymous caller
    gets 401, not a 422 that describes our schema. (A body that is not JSON at all, or too large,
    is refused earlier with a generic 400/413/415, which reveals nothing.)"""
    if not x_api_key:
        raise _unauthorized("Send your API key in the X-API-Key header.")
    digest = key_hash(x_api_key)
    now = time.monotonic()
    cached = _cache.get(digest)
    if cached is not None and cached[0] > now:
        reject_unknown_query(request)
        return cached[1]
    pool: AsyncConnectionPool = request.app.state.db
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT client_id, scope FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL",
            (digest,),
        )
        row = await cur.fetchone()
    if row is None:
        _cache.pop(digest, None)
        raise _unauthorized("This API key is not valid.")
    who = Principal(client_id=row[0], scope=row[1], key_id=digest.hex()[:16])
    if len(_cache) >= CACHE_MAX:
        _cache.clear()  # crude but bounded; a real LRU is not worth it at this scale
    _cache[digest] = (now + CACHE_TTL_S, who)
    reject_unknown_query(request)  # after auth: 401 before any 422 (PR #6 review)
    return who


async def shipper_principal(who: Annotated[Principal, Depends(principal)]) -> Principal:
    """For shipper endpoints: an ops key gets 403. Ops keys are the most privileged kind; a leaked
    one must not also act as a phantom shipper spending Meridian's slots (PR #4 review)."""
    if who.scope != "shipper":
        raise ProblemError(403, "forbidden", "Forbidden", "This endpoint needs a shipper API key.")
    return who


async def ops_principal(who: Annotated[Principal, Depends(principal)]) -> Principal:
    """For ops-only endpoints (FR-7): a valid shipper key gets 403, not 401."""
    if who.scope != "ops":
        raise ProblemError(403, "forbidden", "Forbidden", "This endpoint needs an ops API key.")
    return who


class KeyExists(Exception):
    """This key's hash is already registered (possibly revoked, or for another client)."""


MIN_KEY_LENGTH = 32  # unsalted SHA-256 is only safe for high-entropy keys


async def add_key(database_url: str, key: str, client_id: str, scope: Scope, label: str) -> None:
    """Register a NEW key. Never re-activates a revoked key or moves a key to another client or
    scope: a leaked, revoked key must stay dead (PR #4 review)."""
    async with await AsyncConnection.connect(database_url) as conn:
        cur = await conn.execute(
            """INSERT INTO api_keys (key_hash, client_id, scope, label) VALUES (%s, %s, %s, %s)
               ON CONFLICT (key_hash) DO NOTHING""",
            (key_hash(key), client_id, scope, label),
        )
        await conn.commit()
    if cur.rowcount != 1:
        raise KeyExists("this key is already registered (it may be revoked); use a new key")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage gateway API keys")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="register a NEW key; the key comes from $API_KEY")
    add.add_argument("--client", required=True)
    add.add_argument("--scope", choices=["shipper", "ops"], default="shipper")
    add.add_argument("--label", default="")
    add.add_argument(
        "--allow-weak",
        action="store_true",
        help=f"accept a $API_KEY shorter than {MIN_KEY_LENGTH} characters (dev keys only)",
    )
    args = parser.parse_args()
    supplied = "API_KEY" in os.environ
    key = os.environ["API_KEY"] if supplied else secrets.token_urlsafe(32)
    if supplied and not key:
        sys.exit("API_KEY is set but empty: refusing to register an unknown random key instead")
    if supplied and len(key) < MIN_KEY_LENGTH and not args.allow_weak:
        sys.exit(f"API_KEY is shorter than {MIN_KEY_LENGTH} characters (use --allow-weak for dev)")
    try:
        asyncio.run(add_key(get_settings().database_url, key, args.client, args.scope, args.label))
    except KeyExists as exc:
        sys.exit(str(exc))
    shown = "(from $API_KEY)" if supplied else f"{key}  <- shown once; store it now"
    print(f"added {args.scope} key for {args.client}: {shown}", file=sys.stderr)


if __name__ == "__main__":
    main()
