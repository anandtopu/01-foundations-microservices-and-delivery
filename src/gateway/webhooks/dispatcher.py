"""webhook-dispatcher: deliver the outbox (spec M7, FR-6).

    python -m gateway.webhooks.dispatcher          # loop every WEBHOOK_POLL_INTERVAL_S
    python -m gateway.webhooks.dispatcher --once   # one pass, then exit (tests, demo)

Claim: due `pending` rows are claimed with FOR UPDATE SKIP LOCKED, and the claim itself is a lease
(next_attempt_at pushed 60 s out) that COMMITS before any HTTP call. So replicas never send the same
row, no transaction is held open across a 5 s network call, and a dispatcher that dies mid-send
just lets its lease expire: the row is retried (at-least-once; receivers de-duplicate on
`webhook-id`, which is the stable delivery_id).

Every attempt: SSRF guard (DNS answers change), Standard Webhooks signature over the exact bytes
sent, HTTPS only, follow_redirects=False, one total timeout. Outcome rules (spec):
- 2xx       delivered (and any open dead letter for it is resolved);
- 410 Gone  the subscription is disabled and its pending deliveries cancelled;
- anything else (other status, redirect, timeout, refused, SSRF): retry with full jitter from 30 s,
  doubling to a 6 h cap; once WEBHOOK_MAX_AGE has passed since the window started, dead-letter.
"""

import argparse
import asyncio
import json
import logging
import random
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from gateway.config import Settings, get_settings
from gateway.webhooks import ssrf
from gateway.webhooks.signing import sign

log = logging.getLogger("gateway.webhooks")

CLAIM_LEASE_S = 60  # must exceed one attempt (5 s); a crashed dispatcher's rows retry after this
USER_AGENT = "meridian-gateway-webhooks/1"

# The lease's expiry (d.next_attempt_at after the UPDATE) doubles as a fencing token: record() only
# writes if the row still carries it, so a dispatcher whose lease expired (and whose row another
# dispatcher re-claimed) cannot overwrite the newer outcome (PR #5 review).
CLAIM = """
WITH due AS (
    SELECT delivery_id FROM webhook_deliveries
     WHERE status = 'pending' AND next_attempt_at <= now()
     ORDER BY next_attempt_at
     LIMIT %(n)s
     FOR UPDATE SKIP LOCKED
)
UPDATE webhook_deliveries d
   SET next_attempt_at = now() + make_interval(secs => %(lease)s)
  FROM due, webhook_subscriptions s
 WHERE d.delivery_id = due.delivery_id AND s.subscription_id = d.subscription_id
RETURNING d.delivery_id, d.event_type, d.payload, d.attempts, d.window_start,
          s.subscription_id, s.url, s.secret, s.status, d.next_attempt_at
"""

# A failed attempt, in ONE guarded statement. The max age is checked by the database clock
# (now() - window_start), not the app's: no clock skew and no DST arithmetic on a zoned datetime
# (PR #5 review). The next attempt is clamped to the end of the window, so "dead after 72 h" means
# 72 h, not up to 78 h.
FAILED = """
UPDATE webhook_deliveries
   SET attempts = attempts + 1, last_attempt_at = now(), last_result = %(detail)s,
       status = CASE WHEN now() - window_start >= make_interval(secs => %(max_age)s)
                     THEN 'dead' ELSE 'pending' END,
       next_attempt_at = LEAST(now() + make_interval(secs => %(delay)s),
                               window_start + make_interval(secs => %(max_age)s))
 WHERE delivery_id = %(id)s AND status = 'pending' AND next_attempt_at = %(lease)s
RETURNING status, attempts
"""


@dataclass(frozen=True, slots=True)
class Job:
    delivery_id: str
    event_type: str
    payload: dict[str, Any]
    attempts: int  # attempts made BEFORE this one
    window_start: datetime
    subscription_id: str
    url: str
    secret: str
    subscription_status: str
    lease: datetime  # fencing token: the claim's next_attempt_at


@dataclass(frozen=True, slots=True)
class Outcome:
    kind: str  # delivered | gone | failed | cancelled
    detail: str  # "HTTP 204", "ConnectError", "ssrf: ..."


def backoff_s(attempts: int, cfg: Settings) -> float:
    """Full jitter (spec): uniform in [0, min(cap, base * 2^attempts)]: 30 s, 60 s, ... 6 h."""
    ceiling = min(cfg.webhook_max_delay_s, cfg.webhook_base_delay_s * 2**attempts)
    return random.uniform(0, ceiling)  # noqa: S311 - jitter, not crypto


def body_bytes(payload: dict[str, Any]) -> bytes:
    """The exact bytes signed AND sent (canonical JSON: a retry sends identical bytes)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


async def attempt(client: httpx.AsyncClient, job: Job, cfg: Settings) -> Outcome:
    """One delivery attempt. Never raises: whatever a receiver (or its URL) does, it becomes an
    Outcome, so one bad endpoint cannot take the batch, or the dispatcher, down (PR #5 review)."""
    try:
        # ONE budget for the SSRF lookup and the request: a shipper's slow DNS counts too.
        async with asyncio.timeout(cfg.webhook_timeout_s):
            return await _send(client, job, cfg)
    except TimeoutError:
        return Outcome("failed", f"timeout after {cfg.webhook_timeout_s:g} s")
    except Exception as exc:  # InvalidURL, DecodingError, TransportError, ...
        return Outcome("failed", type(exc).__name__)


async def _send(client: httpx.AsyncClient, job: Job, cfg: Settings) -> Outcome:
    try:
        await ssrf.guard(job.url, cfg.dev_allow_hosts)
    except (ValueError, OSError) as exc:
        return Outcome("failed", f"ssrf: {exc}")
    body = body_bytes(job.payload)
    ts = int(time.time())
    headers = {
        "content-type": "application/json",
        "user-agent": USER_AGENT,
        "accept-encoding": "identity",  # we never read the body, so never ask for a compressed one
        "webhook-id": job.delivery_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sign(job.secret, job.delivery_id, ts, body),
    }
    # stream(): the outcome is the status code alone and the response body is never read, so a
    # hostile receiver cannot answer with a gzip bomb or an endless body (PR #5 review).
    async with client.stream("POST", job.url, content=body, headers=headers) as resp:
        status = resp.status_code
    if 200 <= status < 300:
        return Outcome("delivered", f"HTTP {status}")
    if status == 410:
        return Outcome("gone", "HTTP 410")
    return Outcome("failed", f"HTTP {status}")  # includes 3xx: redirects are not followed


async def record(conn: AsyncConnection, job: Job, outcome: Outcome, cfg: Settings) -> str:
    """Persist one attempt's outcome; returns a short log description."""
    async with conn.transaction():
        if outcome.kind == "delivered":
            # The receiver has it, whatever happened to the row meanwhile: delivered wins.
            await conn.execute(
                """UPDATE webhook_deliveries
                      SET status = 'delivered', delivered_at = now(), attempts = attempts + 1,
                          last_attempt_at = now(), last_result = %s
                    WHERE delivery_id = %s AND status <> 'delivered'""",
                (outcome.detail, job.delivery_id),
            )
            await conn.execute(  # a replayed dead letter is resolved once really delivered
                """UPDATE dead_letters SET resolved_at = now()
                    WHERE delivery_id = %s AND kind = 'webhook' AND resolved_at IS NULL""",
                (job.delivery_id,),
            )
            return "delivered"
        if outcome.kind == "gone":
            await conn.execute(
                """UPDATE webhook_subscriptions SET status = 'disabled', disabled_reason = %s
                    WHERE subscription_id = %s""",
                ("endpoint answered 410 Gone", job.subscription_id),
            )
            await conn.execute(
                """UPDATE webhook_deliveries
                      SET status = 'cancelled', last_attempt_at = now(), last_result = %s,
                          attempts = attempts + CASE WHEN delivery_id = %s THEN 1 ELSE 0 END
                    WHERE subscription_id = %s AND status = 'pending'""",
                (outcome.detail, job.delivery_id, job.subscription_id),
            )
            return "subscription disabled (410)"
        if outcome.kind == "cancelled":
            cur = await conn.execute(
                """UPDATE webhook_deliveries SET status = 'cancelled', last_result = %s
                    WHERE delivery_id = %s AND status = 'pending' AND next_attempt_at = %s""",
                (outcome.detail, job.delivery_id, job.lease),
            )
            return "cancelled (" + outcome.detail + ")" if cur.rowcount else "stale, ignored"
        cur = await conn.execute(
            FAILED,
            {
                "detail": outcome.detail,
                "max_age": cfg.webhook_max_age_s,
                "delay": backoff_s(job.attempts, cfg),
                "id": job.delivery_id,
                "lease": job.lease,
            },
        )
        row = await cur.fetchone()
        if row is None:  # the lease was lost (re-claimed, cancelled, deleted): not ours to write
            return f"stale outcome ignored ({outcome.detail})"
        status, attempts = row
        if status == "dead":
            source = {
                "subscription_id": job.subscription_id,
                "event_type": job.event_type,
                "attempts": attempts,
            }
            reason = f"max age {cfg.webhook_max_age} exceeded (last: {outcome.detail})"
            await conn.execute(
                """INSERT INTO dead_letters (kind, reason, source, delivery_id)
                   VALUES ('webhook', %s, %s, %s)
                   ON CONFLICT (delivery_id) WHERE kind = 'webhook' AND resolved_at IS NULL
                   DO UPDATE SET reason = EXCLUDED.reason, source = EXCLUDED.source""",
                (reason, Jsonb(source), job.delivery_id),
            )
            return f"dead-lettered after {attempts} attempts ({outcome.detail})"
        cur = await conn.execute(
            "SELECT extract(epoch FROM next_attempt_at - now()) FROM webhook_deliveries"
            " WHERE delivery_id = %s",
            (job.delivery_id,),
        )
        (delay,) = await cur.fetchone() or (0,)
        return f"retry in {float(delay):.1f}s (attempt {attempts}, {outcome.detail})"


async def claim(conn: AsyncConnection, n: int) -> list[Job]:
    async with conn.transaction():  # the lease commits before any HTTP call
        cur = await conn.execute(CLAIM, {"n": n, "lease": CLAIM_LEASE_S})
        rows = await cur.fetchall()
    return [Job(str(r[0]), r[1], r[2], r[3], r[4], str(r[5]), r[6], r[7], r[8], r[9]) for r in rows]


async def dispatch_once(conn: AsyncConnection, client: httpx.AsyncClient, cfg: Settings) -> int:
    """Claim a batch, attempt it concurrently, record each outcome; returns how many claimed."""
    jobs = await claim(conn, cfg.webhook_batch_size)

    async def run_one(job: Job) -> Outcome:
        if job.subscription_status != "active":
            # Queued by a transaction that raced a 410 (PR #5 review): never send it.
            return Outcome("cancelled", f"subscription {job.subscription_status}")
        return await attempt(client, job, cfg)

    outcomes = await asyncio.gather(*(run_one(j) for j in jobs))
    # 410s first: they cancel the subscription's other pending rows, so a sibling's failure in the
    # same batch then finds its row cancelled and is ignored, instead of retrying or dead-lettering
    # a delivery for an endpoint that is gone (PR #5 review). Siblings already in flight were sent.
    ordered = sorted(zip(jobs, outcomes, strict=True), key=lambda jo: jo[1].kind != "gone")
    for job, outcome in ordered:
        try:
            what = await record(conn, job, outcome, cfg)
        except psycopg.OperationalError:
            raise  # the connection is gone: let run() reconnect; unrecorded leases just expire
        except Exception:
            log.exception("delivery=%s: recording %s failed", job.delivery_id, outcome.detail)
            continue  # one row's problem must not lose the rest of the batch's outcomes
        log.info("delivery=%s event=%s -> %s", job.delivery_id, job.event_type, what)
    return len(jobs)


def http_client(cfg: Settings) -> httpx.AsyncClient:
    # The lab sink's self-signed certificate is trusted by the DISPATCHER only (an extra CA on top
    # of the system store), never by the rest of the gateway.
    ctx = ssl.create_default_context()
    if cfg.webhook_ca_bundle:
        ctx.load_verify_locations(cfg.webhook_ca_bundle)
    return httpx.AsyncClient(verify=ctx, follow_redirects=False, timeout=cfg.webhook_timeout_s)


async def run(cfg: Settings, *, once: bool) -> None:
    log.info(
        "dispatching every %ss, max age %s, backoff %gs..%gs, dev hosts %s",
        cfg.webhook_poll_interval_s, cfg.webhook_max_age, cfg.webhook_base_delay_s,
        cfg.webhook_max_delay_s, sorted(cfg.dev_allow_hosts) or "none",
    )  # fmt: skip
    async with http_client(cfg) as client:
        while True:
            try:
                async with await AsyncConnection.connect(cfg.database_url) as conn:
                    while await dispatch_once(conn, client, cfg):
                        pass  # drain everything due before sleeping
            except Exception as err:
                if once:
                    raise
                # Keep going whatever happened: a dead dispatcher stops webhooks for every tenant.
                log.error("dispatch failed: %s: %s", type(err).__name__, err)
            if once:
                return
            await asyncio.sleep(cfg.webhook_poll_interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(get_settings(), once=args.once))


if __name__ == "__main__":
    main()
