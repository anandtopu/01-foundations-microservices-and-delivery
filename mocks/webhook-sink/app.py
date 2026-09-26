"""A shipper's webhook receiver (spec M2/M7). Written for the lab, not part of the spec's code.

It verifies Standard Webhooks signatures with its OWN implementation (deliberately not importing
the gateway's signing module), so a bug in the gateway's signer cannot be hidden by the same bug
in the verifier.

Every delivery is logged as `signature=valid` or `signature=invalid` (the M7 gate greps for these).

Control plane:
- POST /__secrets  {"secrets": ["whsec_..."]}  set the secret(s) to accept (two during rotation)
- POST /__mode     {"status": 503}             answer every delivery with this status
                                                (410 = unsubscribe)
- GET  /__received                             deliveries seen so far (for tests and the demo)
- POST /__reset                                forget deliveries, back to status 200
"""

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, Field

TOLERANCE_S = 300  # Standard Webhooks: reject timestamps more than 5 minutes from our clock

log = logging.getLogger("webhook-sink")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

app = FastAPI(title="Shipper webhook sink (mock)")
secrets: list[str] = [s for s in os.getenv("SINK_SECRETS", "").split(",") if s]
mode_status = 200
received: list[dict[str, Any]] = []


def verify(secret_list: list[str], msg_id: str, ts: str, body: bytes, header: str) -> bool:
    if not ts.isdigit() or abs(time.time() - int(ts)) > TOLERANCE_S:
        return False
    candidates = [c.split(",", 1)[1] for c in header.split() if c.startswith("v1,")]
    for secret in secret_list:
        key = base64.b64decode(secret.removeprefix("whsec_"))
        mac = hmac.new(key, msg_id.encode() + b"." + ts.encode() + b"." + body, hashlib.sha256)
        expected = base64.b64encode(mac.digest()).decode()
        if any(hmac.compare_digest(expected, c) for c in candidates):
            return True
    return False


@app.post("/webhooks/{path:path}")
async def receive(path: str, request: Request) -> Response:
    body = await request.body()
    msg_id = request.headers.get("webhook-id", "")
    ts = request.headers.get("webhook-timestamp", "")
    ok = bool(secrets) and verify(
        secrets, msg_id, ts, body, request.headers.get("webhook-signature", "")
    )
    received.append(
        {
            "path": path,
            "webhook_id": msg_id,
            "valid": ok,
            "at": time.time(),
            "answered": mode_status,
        }
    )
    log.info(
        "delivery id=%s path=/%s signature=%s answered=%d",
        msg_id,
        path,
        "valid" if ok else "invalid",
        mode_status,
    )
    return Response(status_code=mode_status if ok else 400)


class SecretsBody(BaseModel):
    secrets: list[str] = Field(min_length=1, max_length=2)


class ModeBody(BaseModel):
    status: int = Field(ge=200, le=599)


@app.post("/__secrets")
async def set_secrets(body: SecretsBody) -> dict[str, int]:
    global secrets
    secrets = body.secrets
    return {"secrets": len(secrets)}


@app.post("/__mode")
async def set_mode(body: ModeBody) -> dict[str, int]:
    global mode_status
    mode_status = body.status
    log.info("mode status=%d", mode_status)
    return {"status": mode_status}


@app.get("/__received")
async def get_received() -> dict[str, Any]:
    valid = sum(r["valid"] for r in received)
    return {
        "total": len(received),
        "valid": valid,
        "invalid": len(received) - valid,
        "deliveries": received,
    }


@app.post("/__reset")
async def reset() -> dict[str, int]:
    global mode_status
    received.clear()
    mode_status = 200
    return {"total": 0}


@app.get("/__health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
