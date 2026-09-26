"""Standard Webhooks signatures (spec M7): HMAC-SHA256 over
`{webhook-id}.{webhook-timestamp}.{body}`, keyed with the base64-decoded part of a `whsec_` secret.
The spec's code, verbatim.

`webhook-signature` can carry several space-separated signatures, so a secret is rotated by signing
with old and new for 24 hours, then dropping the old one. `verify` is the reference verifier we
hand to shipper integration teams: it rejects timestamps outside a 5-minute window (replay defence)
and compares in constant time.
"""

import base64
import hashlib
import hmac
import time

TOLERANCE_S = 300


def sign(secret: str, msg_id: str, ts: int, body: bytes) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    mac = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def verify(secret: str, headers: dict[str, str], body: bytes) -> bool:
    """The reference verifier you hand to shipper integration teams."""
    msg_id, ts = headers["webhook-id"], int(headers["webhook-timestamp"])
    if abs(time.time() - ts) > TOLERANCE_S:
        return False  # outside the replay window
    expected = sign(secret, msg_id, ts, body).split(",", 1)[1]
    return any(
        hmac.compare_digest(expected, candidate.split(",", 1)[1])
        for candidate in headers["webhook-signature"].split()
        if candidate.startswith("v1,")
    )
