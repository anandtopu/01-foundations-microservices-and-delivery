"""Idempotency keys in Postgres (spec M6, ADR-P01-2).

A key moves through: (absent) -> in_progress -> completed. The first request with a key "owns" it
and calls Meridian; concurrent duplicates get 409 + Retry-After; later duplicates get the stored
response replayed; the same key with a different body gets 422. `begin` is the spec's code
verbatim. `request_hash`, `complete` and `release` are written from the spec's prose.

`locked_until` is a lease: if the owner crashes mid-flight, a retry after 30 s takes the key over
instead of getting 409 forever (the `DO UPDATE ... WHERE locked_until < now()` clause).

Call `begin` on a connection that is NOT in autocommit mode (the API's pool): Postgres locks the
conflicting row even when the DO UPDATE's WHERE is false, and that lock, held until commit, is
what keeps the row from being released (deleted) between the INSERT and the SELECT below.
"""

import hashlib
import json
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from gateway.errors import ProblemError  # maps to RFC 9457 responses


def request_hash(body: dict[str, Any]) -> bytes:
    """SHA-256 of the canonical JSON of the VALIDATED body: key order, whitespace and 1200 vs
    1200.0 do not matter; any real difference in the request does."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).digest()


async def begin(conn, client_id: str, key: str, request_hash: bytes):  # type: ignore[no-untyped-def]
    cur = await conn.execute(
        """INSERT INTO idempotency_keys (client_id, key, request_hash, status)
           VALUES (%s, %s, %s, 'in_progress')
           ON CONFLICT (client_id, key) DO UPDATE
             SET locked_until = now() + interval '30 seconds'
             WHERE idempotency_keys.status = 'in_progress'
               AND idempotency_keys.locked_until < now()
               AND idempotency_keys.request_hash = EXCLUDED.request_hash
           RETURNING key""",
        (client_id, key, request_hash),
    )
    if await cur.fetchone() is not None:
        return None  # we own the key: call upstream
    cur = await conn.execute(
        "SELECT request_hash, status, response_code, response_body FROM idempotency_keys"
        " WHERE client_id = %s AND key = %s",
        (client_id, key),
    )
    stored_hash, status, code, body = await cur.fetchone()
    if stored_hash != request_hash:
        raise ProblemError(422, "idempotency-key-reused", "Key was used with a different body")
    if status == "in_progress":
        raise ProblemError(409, "request-in-progress", "Retry shortly", retry_after=2)
    return code, body  # replay the stored response  # fmt: skip


async def complete(
    conn: AsyncConnection, client_id: str, key: str, code: int, body: dict[str, Any]
) -> bool:
    """Store the response for replay. False if we no longer own the key (our lease expired and a
    retry took it over): the caller still returns its own result, but must not overwrite."""
    cur = await conn.execute(
        """UPDATE idempotency_keys
              SET status = 'completed', response_code = %s, response_body = %s
            WHERE client_id = %s AND key = %s AND status = 'in_progress'""",
        (code, Jsonb(body), client_id, key),
    )
    return cur.rowcount == 1


async def release(conn: AsyncConnection, client_id: str, key: str) -> None:
    """The upstream call failed: forget the key, so a retry with the SAME key is a fresh attempt
    (the contract's 503 promises "nothing was stored against your Idempotency-Key")."""
    await conn.execute(
        "DELETE FROM idempotency_keys WHERE client_id = %s AND key = %s AND status = 'in_progress'",
        (client_id, key),
    )
