"""Schemathesis hooks for the M8 gate (loaded by `hooks = ...` in schemathesis.toml).

Fuzzing POST /v1/webhook-subscriptions with generated URLs registered real, public internet
domains (ul2.net, 1e100.dev, ...): the SSRF guard rightly allowed them, and the dispatcher then
tried to POST signed shipper events to strangers for 72 h (PR #6 review; the 24 rows were deleted,
none was delivered). Generated subscriptions now always point at the lab sink on the Compose
network. The URL rules themselves (https only, SSRF, control characters, credentials) are covered
by tests/unit/test_webhooks_signing_ssrf.py and tests/integration/test_webhooks_api.py.
"""

from typing import Any

import schemathesis

LAB_SINK = "https://webhook-sink:9000/webhooks/fuzz"


@schemathesis.hook
def map_body(context: Any, body: Any) -> Any:
    op = context.operation
    if (
        op is not None
        and op.method.upper() == "POST"
        and op.path == "/v1/webhook-subscriptions"
        and isinstance(body, dict)
        and "url" in body
    ):
        return {**body, "url": LAB_SINK}
    return body
