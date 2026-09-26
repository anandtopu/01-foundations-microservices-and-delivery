"""The transactional outbox (spec M7): turn changes into webhook_deliveries rows.

Callers pass their OWN open connection/transaction: the delivery rows commit or roll back together
with the shipment upsert (or the quote insert) that caused them. That is the whole point: no event
for a change that rolled back, and no change without its event. The dispatcher delivers later.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection


@dataclass(frozen=True, slots=True)
class Event:
    client_id: str
    type: str  # contract EventType
    data: dict[str, Any]


def envelope(event: Event, at: datetime | None = None) -> dict[str, Any]:
    """The contract's ShipmentEvent / RateQuoteEvent body: {type, timestamp, data}."""
    return {
        "type": event.type,
        "timestamp": (at or datetime.now(UTC)).isoformat(),
        "data": event.data,
    }


# One statement fans every event out to the client's ACTIVE subscriptions for that event type.
FANOUT = """
INSERT INTO webhook_deliveries (subscription_id, event_type, payload)
SELECT s.subscription_id, e.event_type, e.payload::jsonb
  FROM unnest(%s::text[], %s::text[], %s::text[]) AS e(client_id, event_type, payload)
  JOIN webhook_subscriptions s
    ON s.client_id = e.client_id
   AND s.status = 'active'
   AND e.event_type = ANY (s.event_types)
"""


async def enqueue(conn: AsyncConnection, events: list[Event]) -> int:
    """Insert one delivery per (event, matching subscription); returns how many."""
    if not events:
        return 0
    now = datetime.now(UTC)
    cur = await conn.execute(
        FANOUT,
        (
            [e.client_id for e in events],
            [e.type for e in events],
            [json.dumps(envelope(e, now), separators=(",", ":"), sort_keys=True) for e in events],
        ),
    )
    return cur.rowcount
