"""M6 gate: 20 concurrent identical requests with ONE Idempotency-Key.

    uv run python load/idempotency_burst.py            # against http://localhost:8000

Expected (spec M6): Meridian's mock sees exactly one call, and all 20 clients end with the same
201 body; the ones that arrive while the first is in flight get 409 + Retry-After and retry,
like a well-behaved shipper would. Resets the mock's counters first, so /__stats counts only this.
"""

import asyncio
import json
import os
import sys
import uuid
from collections import Counter

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
MOCK_URL = os.environ.get("MOCK_URL", "http://localhost:8080")
API_KEY = os.environ.get("API_KEY", "dev-shipper-key")
CLIENTS = 20
BODY = {"origin_zip": "30301", "dest_zip": "60601", "weight_lb": 1200, "service_level": "FTL"}


async def shipper(client: httpx.AsyncClient, key: str, seen: Counter[str]) -> tuple[int, str]:
    """One client: POST, and on 409 wait Retry-After seconds and try again (at most 10 times)."""
    for _ in range(10):
        r = await client.post(
            "/v1/rate-quotes",
            json=BODY,
            headers={"X-API-Key": API_KEY, "Idempotency-Key": key},
        )
        seen[f"{r.status_code}{' replayed' if r.headers.get('Idempotent-Replayed') else ''}"] += 1
        if r.status_code != 409:
            return r.status_code, r.text
        await asyncio.sleep(int(r.headers["Retry-After"]))
    return 0, "gave up after 10 x 409"


async def main() -> int:
    key = f"burst-{uuid.uuid4()}"
    async with httpx.AsyncClient(base_url=MOCK_URL) as mock:
        await mock.post("/__reset")
    seen: Counter[str] = Counter()
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        results = await asyncio.gather(*(shipper(client, key, seen) for _ in range(CLIENTS)))
    async with httpx.AsyncClient(base_url=MOCK_URL) as mock:
        stats = (await mock.get("/__stats")).json()

    statuses = Counter(code for code, _ in results)
    bodies = {text for _, text in results}
    print(f"idempotency key:        {key}")
    print(f"responses seen:         {dict(seen)}")
    print(f"final status per client: {dict(statuses)}")
    print(f"distinct final bodies:  {len(bodies)}")
    print(f"quote_id:               {json.loads(next(iter(bodies))).get('quote_id', '?')}")
    print(f"mock /__stats:          {{'calls': {stats['calls']}}}")
    # The spec's gate: one upstream call, 20 identical bodies, "some after 409 retries".
    ok = (
        stats["calls"] == 1
        and statuses == Counter({201: CLIENTS})
        and len(bodies) == 1
        and seen["409"] > 0
    )
    print("GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
