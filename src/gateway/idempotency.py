"""Idempotency keys in Postgres (spec M6, ADR-P01-2).

A key moves through: (absent) -> in_progress -> completed. The first request with a key "owns" it
and calls Meridian; concurrent duplicates get 409 + Retry-After; later duplicates get the stored
response replayed; the same key with a different body gets 422. `begin` is the spec's logic
verbatim, with the lab changes marked "lab:" (typed, and it returns a fencing token; ARCHITECTURE
difference 31). `request_hash`, `complete` and `release` are written from the spec's prose.

`locked_until` is a lease: if the owner crashes mid-flight, a retry after 30 s takes the key over
instead of getting 409 forever (the `DO UPDATE ... WHERE locked_until < now()` clause).

Call `begin` on a connection that is NOT in autocommit mode (the API's pool): Postgres locks the
conflicting row even when the DO UPDATE's WHERE is false, and that lock, held until commit, is
what keeps the row from being released (deleted) between the INSERT and the SELECT below.

Fencing (PR #4 review): the owner's `locked_until` is its token. A takeover writes a new
`locked_until`, so a stale owner (its lease expired while it worked) matches nothing in
`complete`/`release`: it can neither overwrite the new owner's result nor delete its claim, which
would let a third request call Meridian concurrently.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from gateway.errors import ProblemError  # maps to RFC 9457 responses


def request_hash(body: dict[str, Any]) -> bytes:
    """SHA-256 of the canonical JSON of the VALIDATED body: key order, whitespace and 1200 vs
    1200.0 do not matter; any real difference in the request does."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).digest()


@dataclass(frozen=True, slots=True)
class Owned:
    """lab: we own the key; `token` (the lease's locked_until) fences complete/release."""

    token: datetime


async def begin(
    conn: AsyncConnection, client_id: str, key: str, request_hash: bytes
) -> Owned | tuple[int, dict[str, Any]]:  # lab: typed (the spec's signature is untyped)
    cur = await conn.execute(
        """INSERT INTO idempotency_keys (client_id, key, request_hash, status)
           VALUES (%s, %s, %s, 'in_progress')
           ON CONFLICT (client_id, key) DO UPDATE
             SET locked_until = now() + interval '30 seconds'
             WHERE idempotency_keys.status = 'in_progress'
               AND idempotency_keys.locked_until < now()
               AND idempotency_keys.request_hash = EXCLUDED.request_hash
           RETURNING locked_until""",  # lab: was RETURNING key; the lease is our token
        (client_id, key, request_hash),
    )
    claimed = await cur.fetchone()
    if claimed is not None:
        return Owned(claimed[0])  # we own the key: call upstream (lab: was `return None`)
    cur = await conn.execute(
        "SELECT request_hash, status, response_code, response_body FROM idempotency_keys"
        " WHERE client_id = %s AND key = %s",
        (client_id, key),
    )
    row = await cur.fetchone()
    if row is None:  # lab: unreachable (the ON CONFLICT row lock keeps the row); fail loudly
        raise RuntimeError("idempotency row vanished between INSERT and SELECT")
    stored_hash, status, code, body = row
    if stored_hash != request_hash:
        raise ProblemError(422, "idempotency-key-reused", "Key was used with a different body")
    if status == "in_progress":
        raise ProblemError(409, "request-in-progress", "Retry shortly", retry_after=2)
    return code, body  # replay the stored response


async def peek(
    conn: AsyncConnection, client_id: str, key: str, request_hash: bytes
) -> tuple[int, dict[str, Any]] | None:
    """Read-only twin of `begin` for the circuit-open fast path: the same 422 / 409 / replay
    answers for an existing key, None if the key is unknown. Claims nothing."""
    cur = await conn.execute(
        "SELECT request_hash, status, response_code, response_body FROM idempotency_keys"
        " WHERE client_id = %s AND key = %s",
        (client_id, key),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    stored_hash, status, code, body = row
    if stored_hash != request_hash:
        raise ProblemError(422, "idempotency-key-reused", "Key was used with a different body")
    if status == "in_progress":
        raise ProblemError(409, "request-in-progress", "Retry shortly", retry_after=2)
    return code, body


async def complete(
    conn: AsyncConnection,
    client_id: str,
    key: str,
    token: datetime,
    code: int,
    body: dict[str, Any],
) -> bool:
    """Store the response for replay, only while we still hold the lease (`token`). False if a
    retry took the key over (our lease expired): the caller still returns its own result, but the
    new owner's result is the one replayed."""
    cur = await conn.execute(
        """UPDATE idempotency_keys
              SET status = 'completed', response_code = %s, response_body = %s
            WHERE client_id = %s AND key = %s AND status = 'in_progress' AND locked_until = %s""",
        (code, Jsonb(body), client_id, key, token),
    )
    return cur.rowcount == 1


async def release(conn: AsyncConnection, client_id: str, key: str, token: datetime) -> None:
    """The upstream call failed: forget the key, so a retry with the SAME key is a fresh attempt
    (the contract's 503 promises "nothing was stored against your Idempotency-Key"). Fenced: a
    stale owner must not delete the claim of the request that took the key over."""
    await conn.execute(
        """DELETE FROM idempotency_keys
            WHERE client_id = %s AND key = %s AND status = 'in_progress' AND locked_until = %s""",
        (client_id, key, token),
    )
