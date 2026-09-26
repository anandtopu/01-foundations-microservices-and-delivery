"""Dead letters for ops (FR-7): GET /v1/dead-letters and POST /v1/dead-letters/{id}:replay.

Ops keys only (403 for shipper keys). Every replay ATTEMPT is audited, successful or not (spec
section 9, "Repudiation"): the outcome is decided inside the transaction, the audit row commits, and
only then is a 409 / 422 raised, so a failed replay still leaves its trace.

- `row` dead letter: the raw CSV line is re-validated (e.g. after a new status code was mapped); if
  it now passes, it goes through the SAME path as the poller (owner check, upsert, outbox): 200.
- `webhook` dead letter: the delivery is re-queued with a fresh retry window: 202. It is marked
  resolved when the dispatcher actually delivers it.
"""

import base64
import binascii
import json
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from gateway.auth import Principal, ops_principal
from gateway.errors import ProblemError
from gateway.ingest.model import COLUMNS, GoodRow, parse_export
from gateway.ingest.poller import apply_rows

Ops = Annotated[Principal, Depends(ops_principal)]
router = APIRouter()

_SOURCE_KEYS = ("file_name", "line_no", "raw", "subscription_id", "event_type", "attempts")


def encode_cursor(created_at: str, dead_letter_id: str, filters: dict[str, Any]) -> str:
    """Opaque, and bound to the filter it was issued for (contract: a cursor from another filter
    is a 400)."""
    blob = json.dumps({"c": created_at, "i": dead_letter_id, "f": filters}, sort_keys=True)
    return base64.urlsafe_b64encode(blob.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, filters: dict[str, Any]) -> tuple[str, str]:
    try:
        blob = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if blob["f"] != filters:
            raise ValueError("cursor was issued for another filter")
        return str(blob["c"]), str(uuid.UUID(blob["i"]))
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise ProblemError(400, "invalid-cursor", "Invalid cursor", str(exc)) from exc


def view(row: tuple[Any, ...]) -> dict[str, Any]:
    dl_id, kind, reason, source, created_at, resolved, replay_count, last_replayed_at = row
    return {
        "dead_letter_id": str(dl_id),
        "kind": kind,
        "reason": reason,
        "source": {k: source[k] for k in _SOURCE_KEYS if k in source},
        "created_at": created_at.isoformat(),
        "resolved": resolved,
        "replay_count": replay_count,
        "last_replayed_at": last_replayed_at.isoformat() if last_replayed_at else None,
    }


@router.get("/v1/dead-letters")
async def list_dead_letters(
    request: Request,
    who: Ops,
    kind: Literal["row", "webhook"] | None = None,
    resolved: bool | None = None,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    filters = {"kind": kind, "resolved": resolved}
    after = decode_cursor(cursor, filters) if cursor else (None, None)
    pool: AsyncConnectionPool = request.app.state.db
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT dead_letter_id, kind, reason, source, created_at,
                      resolved_at IS NOT NULL, replay_count, last_replayed_at
                 FROM dead_letters
                WHERE (%(kind)s::text IS NULL OR kind = %(kind)s)
                  AND (%(resolved)s::boolean IS NULL OR (resolved_at IS NOT NULL) = %(resolved)s)
                  AND (%(c)s::timestamptz IS NULL
                       OR (created_at, dead_letter_id) < (%(c)s::timestamptz, %(i)s::uuid))
                ORDER BY created_at DESC, dead_letter_id DESC
                LIMIT %(n)s""",
            {"kind": kind, "resolved": resolved, "c": after[0], "i": after[1], "n": limit + 1},
        )
        rows = await cur.fetchall()
    page = [view(r) for r in rows[:limit]]
    more = len(rows) > limit
    last = page[-1] if page else None
    next_cursor = (
        encode_cursor(last["created_at"], last["dead_letter_id"], filters)
        if more and last
        else None
    )
    return {"data": page, "next_cursor": next_cursor}


async def replay_row(conn: AsyncConnection, dl_id: uuid.UUID, source: dict[str, Any]) -> str:
    """Re-validate the raw line; if it passes now, apply it like the poller would."""
    header = ",".join(COLUMNS).encode()
    raw = str(source.get("raw", "")).encode("cp1252", errors="replace")
    results = list(parse_export(header + b"\r\n" + raw + b"\r\n"))
    if len(results) != 1 or not isinstance(results[0], GoodRow):
        reason = getattr(results[0], "reason", "no data line") if results else "no data line"
        return f"rejected: {reason}"
    row = GoodRow(int(source.get("line_no", 0)), results[0].row, results[0].raw)
    _, refused, _ = await apply_rows(conn, [row], f"replay:{dl_id}")
    if refused:
        return f"rejected: {refused[0].reason}"
    return "applied"


async def replay_webhook(conn: AsyncConnection, delivery_id: uuid.UUID | None) -> str:
    """Re-queue the SAME delivery (same webhook-id, so the receiver can de-duplicate) with a
    fresh retry window. Resolved later, by the dispatcher, when it is actually delivered."""
    if delivery_id is None:
        return "rejected: the delivery no longer exists (subscription deleted)"
    cur = await conn.execute(
        """UPDATE webhook_deliveries d
              SET status = 'pending', attempts = 0, next_attempt_at = now(),
                  window_start = now(), last_result = NULL
             FROM webhook_subscriptions s
            WHERE d.delivery_id = %s AND s.subscription_id = d.subscription_id
              AND s.status = 'active'""",
        (delivery_id,),
    )
    return "queued" if cur.rowcount == 1 else "rejected: the subscription is disabled"


@router.post("/v1/dead-letters/{dead_letter_id}:replay")
async def replay_dead_letter(dead_letter_id: uuid.UUID, request: Request, who: Ops) -> JSONResponse:
    pool: AsyncConnectionPool = request.app.state.db
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """SELECT kind, source, resolved_at IS NOT NULL, delivery_id
                 FROM dead_letters WHERE dead_letter_id = %s FOR UPDATE""",
            (dead_letter_id,),
        )
        found = await cur.fetchone()
        if found is None:
            raise ProblemError(404, "not-found", "Not found", "No such dead letter.")
        kind, source, resolved, delivery_id = found
        if resolved:
            outcome = "already_resolved"
        elif kind == "row":
            outcome = await replay_row(conn, dead_letter_id, source)
        else:
            outcome = await replay_webhook(conn, delivery_id)
        await conn.execute(
            """INSERT INTO dead_letter_replays (dead_letter_id, ops_client_id, outcome)
               VALUES (%s, %s, %s)""",
            (dead_letter_id, who.client_id, outcome),
        )
        if outcome != "already_resolved":
            await conn.execute(
                """UPDATE dead_letters
                      SET replay_count = replay_count + 1, last_replayed_at = now(),
                          resolved_at = CASE WHEN %s THEN now() ELSE resolved_at END
                    WHERE dead_letter_id = %s""",
                (outcome == "applied", dead_letter_id),
            )
    # The audit row is committed; now report the outcome.
    if outcome == "already_resolved":
        raise ProblemError(409, "already-resolved", "Already resolved", "Nothing to replay.")
    if outcome.startswith("rejected: "):
        raise ProblemError(
            422, "replay-rejected", "Replay rejected", outcome.removeprefix("rejected: ")
        )
    result = {"dead_letter_id": str(dead_letter_id), "kind": kind, "outcome": outcome}
    return JSONResponse(result, status_code=200 if outcome == "applied" else 202)
