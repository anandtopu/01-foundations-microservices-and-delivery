"""Shipment status for shippers (FR-3): GET /v1/shipments and GET /v1/shipments/{shipment_id}.

Every read is filtered by the caller's client_id IN THE QUERY (section 9, BOLA): another shipper's
shipment is "not found", with the same body as one that does not exist.

The list is keyset-paginated on (updated_at, shipment_id), served by the index
shipments_client_page (client_id, updated_at, shipment_id). A keyset cursor stays correct while the
poller keeps writing: an OFFSET would skip or repeat rows as updates move them. A shipment that is
updated while you page through moves to the end and is seen again: that is the point of an
"updated since" feed, and clients de-duplicate on shipment_id.
"""

import base64
import binascii
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from psycopg_pool import AsyncConnectionPool
from pydantic import AwareDatetime, BeforeValidator

from gateway.auth import Principal, shipper_principal
from gateway.errors import ProblemError

Caller = Annotated[Principal, Depends(shipper_principal)]
router = APIRouter()

SHIPMENT_ID = r"^[A-Za-z0-9][A-Za-z0-9-]*$"  # contract ShipmentId
# RFC 3339 date-time, as the contract says (format: date-time). Pydantic's lax datetime parsing also
# accepts "0.5" or "1700000000" as Unix timestamps, which Schemathesis caught in M8.
RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})")


def rfc3339(value: object) -> object:
    if not (isinstance(value, str) and RFC3339.fullmatch(value)):
        raise ValueError("must be an RFC 3339 date-time with an offset, e.g. 2026-09-24T09:15:00Z")
    # Two RFC 3339 values Python's datetime cannot hold (PR #6 review): a leap second (:60) is read
    # as the last microsecond of that minute, and year 0000 as the earliest representable instant.
    # Both mean the same thing for "updated since": nothing is lost.
    if value[17:19] == "60":
        value = value[:17] + "59.999999" + value[19:].lstrip("0123456789.")
    if value.startswith("0000-"):
        value = "0001-01-01T00:00:00" + value[19:].lstrip("0123456789.")
    return value


UpdatedSince = Annotated[AwareDatetime | None, BeforeValidator(rfc3339)]
COLUMNS = "shipment_id, order_no, status, ship_date, weight_lb, updated_at"


def view(row: tuple[Any, ...]) -> dict[str, object]:
    shipment_id, order_no, status, ship_date, weight_lb, updated_at = row
    return {
        "shipment_id": shipment_id,
        "order_no": order_no,
        "status": status,
        "ship_date": ship_date.isoformat(),
        "weight_lb": str(weight_lb),  # numeric(12,2) -> "1260.50": no float rounding (contract)
        "updated_at": updated_at.isoformat(),
    }


def encode_cursor(updated_at: str, shipment_id: str, since: str | None) -> str:
    blob = json.dumps({"u": updated_at, "i": shipment_id, "f": since}, sort_keys=True)
    return base64.urlsafe_b64encode(blob.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, since: str | None) -> tuple[datetime, str]:
    """Opaque to clients, bound to the filter it was issued for, and fully type-checked: a
    tampered cursor is a 400, never a 500 (the lesson of the PR #5 review)."""
    try:
        blob = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(blob, dict) or blob.get("f") != since:
            raise ValueError("cursor issued for another filter")
        at, shipment_id = blob["u"], blob["i"]
        if not (isinstance(at, str) and isinstance(shipment_id, str)):
            raise ValueError("wrong types")
        # A str is not enough: "a\u0000b" reached Postgres (DataError) and "\ud800" psycopg's
        # encoder (UnicodeEncodeError), both 500s (PR #6 review). It must be a real shipment ID.
        if len(shipment_id) > 32 or not re.fullmatch(SHIPMENT_ID, shipment_id):
            raise ValueError("not a shipment ID")
        parsed = datetime.fromisoformat(at)
        if parsed.tzinfo is None:
            raise ValueError("naive timestamp")
        return parsed, shipment_id
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise ProblemError(
            400, "invalid-cursor", "Invalid cursor", "Use a next_cursor from this same query."
        ) from exc


@router.get("/v1/shipments")
async def list_shipments(
    request: Request,
    who: Caller,
    updated_since: UpdatedSince = None,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    # Bound to the INSTANT, not the spelling: ...09:15:00Z and ...11:15:00+02:00 are one filter.
    since = updated_since.astimezone(UTC).isoformat() if updated_since else None
    after = decode_cursor(cursor, since) if cursor else (None, None)
    pool: AsyncConnectionPool = request.app.state.db
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT {COLUMNS} FROM shipments
                 WHERE client_id = %(client)s
                   AND (%(since)s::timestamptz IS NULL OR updated_at >= %(since)s)
                   AND (%(u)s::timestamptz IS NULL
                        OR (updated_at, shipment_id) > (%(u)s, %(i)s::text))
                 ORDER BY updated_at, shipment_id
                 LIMIT %(n)s""",  # noqa: S608 - COLUMNS is a constant, values are parameters
            {"client": who.client_id, "since": updated_since, "u": after[0], "i": after[1],
             "n": limit + 1},
        )  # fmt: skip
        rows = await cur.fetchall()
    page = [view(r) for r in rows[:limit]]
    last = page[-1] if page else None
    next_cursor = (
        encode_cursor(str(last["updated_at"]), str(last["shipment_id"]), since)
        if len(rows) > limit and last
        else None
    )
    return {"data": page, "next_cursor": next_cursor}


@router.get("/v1/shipments/{shipment_id}")
async def get_shipment(
    request: Request,
    who: Caller,
    shipment_id: Annotated[str, Path(min_length=1, max_length=32, pattern=SHIPMENT_ID)],
) -> dict[str, object]:
    pool: AsyncConnectionPool = request.app.state.db
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT {COLUMNS} FROM shipments WHERE shipment_id = %s AND client_id = %s",  # noqa: S608
            (shipment_id, who.client_id),
        )
        row = await cur.fetchone()
    if row is None:  # another shipper's shipment is "not found" (BOLA)
        raise ProblemError(404, "not-found", "Not found", "No such shipment for this API key.")
    return view(row)
