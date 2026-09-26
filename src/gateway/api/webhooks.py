"""Webhook subscriptions (FR-5): POST / GET / DELETE /v1/webhook-subscriptions.

A shipper registers an HTTPS endpoint and the event types it wants. The URL is checked against the
SSRF guard at creation (and again by the dispatcher before every attempt, since DNS answers
change). The signing secret is generated here and returned exactly once.
"""

import base64
import secrets
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gateway.auth import Principal, shipper_principal
from gateway.errors import ProblemError
from gateway.webhooks import ssrf

EventType = Literal["shipment.created", "shipment.status_changed", "rate_quote.completed"]
Caller = Annotated[Principal, Depends(shipper_principal)]
router = APIRouter()


class WebhookSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    url: str = Field(pattern=r"^https://", max_length=2048)
    event_types: list[EventType] = Field(min_length=1)

    @field_validator("event_types")
    @classmethod
    def unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("event_types must be unique")  # contract: uniqueItems
        return v


def new_secret() -> str:
    """Standard Webhooks secret: whsec_ + base64 of 32 random bytes (256-bit HMAC key)."""
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()


def view(row: tuple[Any, ...]) -> dict[str, object]:
    sub_id, url, types, status, created_at = row
    return {
        "subscription_id": str(sub_id),
        "url": url,
        "event_types": types,
        "status": status,
        "created_at": created_at.isoformat(),
    }


@router.post("/v1/webhook-subscriptions", status_code=201)
async def create_subscription(
    body: WebhookSubscriptionRequest, request: Request, who: Caller
) -> JSONResponse:
    try:
        await ssrf.guard(body.url, request.app.state.cfg.dev_allow_hosts)
    except (ValueError, OSError) as exc:  # OSError: the name does not resolve at all
        raise ProblemError(
            422, "webhook-url-not-allowed", "Webhook URL not allowed", str(exc)
        ) from exc
    pool: AsyncConnectionPool = request.app.state.db
    sub_id, secret = uuid.uuid4(), new_secret()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO webhook_subscriptions
                   (subscription_id, client_id, url, event_types, secret)
               VALUES (%s, %s, %s, %s, %s)
               RETURNING subscription_id, url, event_types, status, created_at""",
            (sub_id, who.client_id, body.url, list(body.event_types), secret),
        )
        row = await cur.fetchone()
    if row is None:  # INSERT ... RETURNING always returns the row
        raise RuntimeError("subscription insert returned nothing")
    # The secret appears in this response and nowhere else, ever (contract).
    return JSONResponse(
        view(row) | {"secret": secret},
        status_code=201,
        headers={"Location": f"/v1/webhook-subscriptions/{sub_id}"},
    )


async def owned(request: Request, who: Principal, subscription_id: uuid.UUID) -> tuple[Any, ...]:
    pool: AsyncConnectionPool = request.app.state.db
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT subscription_id, url, event_types, status, created_at
                 FROM webhook_subscriptions WHERE subscription_id = %s AND client_id = %s""",
            (subscription_id, who.client_id),
        )
        row = await cur.fetchone()
    if row is None:  # another shipper's subscription is "not found" (BOLA)
        raise ProblemError(404, "not-found", "Not found", "No such subscription for this API key.")
    return row


@router.get("/v1/webhook-subscriptions/{subscription_id}")
async def get_subscription(
    subscription_id: uuid.UUID, request: Request, who: Caller
) -> dict[str, object]:
    return view(await owned(request, who, subscription_id))  # never the secret


@router.delete("/v1/webhook-subscriptions/{subscription_id}", status_code=204)
async def delete_subscription(
    subscription_id: uuid.UUID, request: Request, who: Caller
) -> Response:
    await owned(request, who, subscription_id)
    pool: AsyncConnectionPool = request.app.state.db
    async with pool.connection() as conn:
        # Pending deliveries go with it (ON DELETE CASCADE): nothing is sent to a deleted endpoint.
        await conn.execute(
            "DELETE FROM webhook_subscriptions WHERE subscription_id = %s AND client_id = %s",
            (subscription_id, who.client_id),
        )
    return Response(status_code=204)
