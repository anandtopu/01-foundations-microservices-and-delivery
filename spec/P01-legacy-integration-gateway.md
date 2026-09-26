## P01 — Legacy Integration Gateway

| Field | Value |
|---|---|
| ID | P01 |
| Tier | **T1 Beginner** → **T2 Intermediate** |
| Time estimate | 25-35 hours |
| Industry framing | Meridian Freight (fictional 3PL): AS/400 CSV exports over SFTP plus a SOAP rate-quote service, 24x7 warehouse operations |
| Required categories covered | API integration; microservices intro |
| Languages | Python 3.14 (FastAPI, httpx, Pydantic, asyncssh, psycopg 3) |
| Cloud(s) | None required. Optional: a single small VM in the customer's AWS account for a realism pass |
| Estimated cost / keeping it near $0 | $0: everything runs in Docker Compose (SFTP server, SOAP mock, Postgres, webhook sink). For the optional AWS pass, use one t4g.small-class instance and destroy it the same day |
| Prerequisites | [C01](../02-curriculum/C01-python-mastery.md) T2, [C07](../02-curriculum/C07-api-design-and-integration.md) T1, [C06](../02-curriculum/C06-databases-and-storage.md) T1, [C10](../02-curriculum/C10-devops-and-cicd.md) T1 (Docker); no prior projects |

### 1. Problem statement

**Business context (composite scenario).** Meridian Freight runs 38 warehouses on an IBM i (AS/400) warehouse-management system dating from 2004. Every 15 minutes a `CPYTOIMPF` job drops a shipment-status CSV onto an SFTP server in Meridian's DMZ. Rate quotes come from a SOAP 1.1 `RateQuoteService` (WSDL last changed 2016) in their data center, reachable from their AWS account over Direct Connect.

**Pain.** Three newly signed enterprise shippers want a REST API for shipment status and rate quotes, plus push notifications on status changes; today they get a nightly email. Meridian once exposed the SOAP service directly, and a shipper's retry loop took it down in peak season: it allows only five concurrent requests.

**Constraints.**
- **Security:** read-only SFTP with key auth and a pinned host key; no inbound internet connections to the data center. Webhook targets are customer-supplied URLs, so SSRF is a first-class risk.
- **Legacy:** Windows-1252 files (CCSID 1252), space-padded fields, IBM i `CYYMMDD` dates (century digit `1` means 20xx), and a `.done` trigger file written after each CSV. The SOAP service returns HTTP 500 with a SOAP Fault for both "bad request" and "busy".
- **Change control:** the IBM i team changes nothing; every fix lives in the gateway.
- **Network:** the SOAP endpoint is reachable only from Meridian's VPC; p99 latency is 2.8 s at normal load.

**Stakeholders.** Meridian's VP of Customer Integration (sponsor), the IBM i team lead (owns the export job; says "no" to changes), Meridian's security architect (approves SFTP access and webhook egress), three shipper integration teams, and Beacon's FDE (you).

**Measurable success criteria.**
1. Shippers can read shipment status within 5 minutes of the AS/400 file landing, for 99% of rows.
2. No shipper behavior, including aggressive retries, can push more than 4 concurrent requests onto the SOAP service.
3. Retried `POST /v1/rate-quotes` calls with the same `Idempotency-Key` never produce a second upstream call.
4. 99% of webhooks reach a healthy subscriber within 60 s; failed deliveries are retried for 72 h and then land in a dead-letter queue that ops can replay.
5. The published OpenAPI contract passes automated conformance tests with zero failures on every build.

### 2. Requirements

**Functional requirements**

- **FR-1** Poll the SFTP drop every 60 s; ingest a CSV only when its `.done` trigger exists, and never twice (tracked by name, size and SHA-256).
- **FR-2** Validate every row: valid rows upsert into `shipments`; invalid rows go to `dead_letters` with file, line number, raw row and reason.
- **FR-3** `GET /v1/shipments/{shipment_id}` and `GET /v1/shipments?updated_since=...&cursor=...` (cursor pagination, max 200 per page).
- **FR-4** `POST /v1/rate-quotes` requires `Idempotency-Key`, calls SOAP, stores the quote for 15 minutes and returns `201` with `Location`; `GET /v1/rate-quotes/{quote_id}` returns it.
- **FR-5** `POST /v1/webhook-subscriptions` registers an HTTPS URL and event types and returns a signing secret exactly once; `DELETE` removes it.
- **FR-6** Emit `shipment.created`, `shipment.status_changed` and `rate_quote.completed` webhooks signed per Standard Webhooks.
- **FR-7** `GET /v1/dead-letters` and `POST /v1/dead-letters/{id}:replay`, restricted to an `ops` API-key scope.
- **FR-8** All errors use RFC 9457 Problem Details (`application/problem+json`).

**Non-functional requirements**

| Attribute | Target |
|---|---|
| Read latency | `GET /v1/shipments/*` p95 < 150 ms at 200 req/s on a 2-vCPU container |
| Quote latency | `POST /v1/rate-quotes` p95 < 3.5 s (upstream p99 is 2.8 s); gateway overhead p95 < 50 ms |
| Availability | Read API 99.9% monthly; quote API 99.5% (bounded by the SOAP service) |
| Freshness | 99% of rows queryable within 5 min of the `.done` file appearing |
| Upstream protection | ≤ 4 concurrent SOAP calls from the gateway; fast `503` + `Retry-After` beyond that |
| RPO / RTO | RPO 15 min (the database can be rebuilt from the 7 days of files Meridian retains); RTO 1 h |
| Throughput | 250,000 shipment rows/day; bursts of 20,000 rows in one file |
| Cost | $0 in the lab; < $40/month if hosted on one small VM plus managed Postgres |

**Constraints.** Python 3.14; no writes to Meridian's SFTP directories; no inbound ports opened in the data center; secrets from environment or a secret store, never in the image.

**Out of scope.** Writing back to the AS/400; customer self-service UI; multi-region; OAuth 2.0 client-credentials for shippers (API keys first, OAuth in an extension); EDI X12 214 translation.

### 3. Architecture

```text
 SHIPPER NETWORKS (internet)                  BEACON-MANAGED ZONE (Meridian AWS VPC, private subnets)
 +----------------------+                    +-------------------------------------------------------------+
 | Shipper apps         | HTTPS + API key    |  +----------------------+     +---------------------------+ |
 |  - status lookups    |------------------->|  | meridian-gateway-api | --> | Postgres 18               | |
 |  - rate quotes       |<-------------------|  | FastAPI, :8000       |     |  shipments, rate_quotes,  | |
 +----------------------+  Problem Details   |  |  idempotency, authz  |     |  idempotency_keys,        | |
           ^                                 |  +----------+-----------+     |  webhook_subscriptions,   | |
           | signed webhooks (HTTPS, egress  |             | bulkhead(4)     |  webhook_deliveries(outbox)| |
           | allow-list, SSRF guard)         |             | retry+jitter    |  ingested_files,          | |
           |                                 |             | breaker         |  dead_letters             | |
 +---------+------------+                    |             v                 +-------------+-------------+ |
 | webhook-dispatcher   |<------ poll outbox (FOR UPDATE SKIP LOCKED) --------------------+             | |
 | worker               |                    |  +----------------------+                 ^             | |
 +----------------------+                    |  | sftp-poller worker   |--- upsert rows -+             | |
                                             |  | asyncssh, every 60 s |--- bad rows ----> dead_letters  | |
                                             |  +----------+-----------+                               | |
                                             +-------------|-------------------|-----------------------+ |
                     ==== TRUST BOUNDARY: Direct Connect / customer data center (read-only access) ====
                                             +-------------|-------------------|-----------------------+
                                             |  SFTP (DMZ) v :22, key auth,    | SOAP 1.1 over HTTPS    |
                                             |  pinned host key                v :443                   |
                                             |  /outbound/shipments/*.csv  RateQuoteService (5 concurrent |
                                             |  + *.csv.done               max, p99 2.8 s)                |
                                             |  ^ written by IBM i CPYTOIMPF job every 15 min             |
                                             +------------------------------------------------------------+
                                               MERIDIAN DATA CENTER (IBM i / AS/400, change-frozen)
```

| Component | Responsibility | Technology | Why | Alternative considered |
|---|---|---|---|---|
| `meridian-gateway-api` | REST API, auth, idempotency, SOAP façade | FastAPI + Pydantic | Async I/O suits a latency-bound upstream; Pydantic also validates CSV rows | Go (faster, but the customer maintains Python) |
| `sftp-poller` worker | Detect, decode, validate, upsert, dead-letter | asyncssh 2.x | asyncio SFTP with host-key verification | paramiko (sync) |
| `webhook-dispatcher` worker | Signed delivery from the outbox, retry, DLQ | httpx 0.28 | Explicit timeouts, no redirects | Celery + Redis (extra parts) |
| Postgres 18 | Records, outbox, idempotency store | PostgreSQL 18.6 | `ON CONFLICT` and `SKIP LOCKED` replace a broker | Redis (no transactional outbox) |
| SOAP adapter | Envelopes, fault mapping | httpx + defusedxml + t-strings | 40 debuggable lines; blocks XXE | zeep (for large WSDLs) |

**Mini-ADRs**

| # | Decision | Options | Choice | Consequences |
|---|---|---|---|---|
| ADR-P01-1 | How to protect a fragile SOAP backend | Rate limit per shipper; global semaphore; queue and async reply | Global bulkhead of 4 + circuit breaker + bounded retries, fast `503` on saturation | Shippers see `503 Retry-After: 2` at peak instead of a dead upstream; the limit goes in the API contract |
| ADR-P01-2 | Where idempotency state lives | In-memory cache; Redis; Postgres | Postgres table keyed by `(client_id, key)` with request hash | Survives restarts and multiple replicas. Adds one write per quote; negligible at this volume |
| ADR-P01-3 | How webhooks are made reliable | Fire-and-forget from the request path; background tasks; transactional outbox | Outbox rows written in the same transaction as the state change | No lost events on crash; delivery is at-least-once, so subscribers must dedupe on `webhook-id` |
| ADR-P01-4 | API contract workflow | Code-first (FastAPI-generated); design-first | Design-first OpenAPI 3.1 in `contracts/openapi.yaml`, conformance-tested against the running service | Shippers review the contract before code exists. OpenAPI 3.2 exists, but tooling support is uneven, so stay on 3.1 |

### 4. Tools & technologies

| Tool | Version / status (as of September 2026) | Notes |
|---|---|---|
| Python | 3.14.7 | 3.15.0 is due 2026-10-01; wait for 3.15.1. 3.10 goes EOL October 2026 |
| uv | 0.12.x (0.12.18 on 2026-09-22) | Pin it in CI. Astral (uv, ruff, ty) is being acquired by OpenAI |
| FastAPI / Pydantic | 0.141.x / 2.13.x | FastAPI is still 0.x: pin exactly. Pydantic handles 3.14 deferred annotations |
| httpx | 0.28.1 | Set explicit `Timeout` objects; the default is 5 s everywhere |
| asyncssh | 2.24.x | Always pass `known_hosts`; `known_hosts=None` disables host-key checking |
| PostgreSQL | 18.6 | Native `uuidv7()`; PG 14 goes EOL 2026-11-12 |
| pytest / ruff / type checker | pytest 9.x, ruff 0.16.x, mypy 2.3.x or Pyrefly 1.0 | ty is still 0.0.x beta |
| Schemathesis | 4.28.0 | Property-based conformance tests from the OpenAPI document |
| k6 | v2.3.0 | AGPL-3.0: expect customer legal questions |
| Docker Engine / Compose | Engine 29.x with Compose v2 | Requires API ≥ 1.44 clients; containerd image store on fresh installs |
| Standards | OpenAPI 3.1, RFC 9457 Problem Details, Standard Webhooks | `Idempotency-Key` is a convention, not an RFC: the IETF draft (-07, Oct 2025) expired unpublished |

### 5. Step-by-step implementation plan

**M1 — Contract first (3-4 h).** Write `contracts/openapi.yaml` before any code. Review it as if you were a shipper.

```yaml
openapi: 3.1.0
info: { title: Meridian Integration Gateway, version: 1.0.0 }
paths:
  /v1/rate-quotes:
    post:
      operationId: createRateQuote
      parameters:
        - name: Idempotency-Key
          in: header
          required: true
          schema: { type: string, minLength: 16, maxLength: 128 }
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/RateQuoteRequest' }
      responses:
        '201':
          description: Quote created
          headers:
            Location: { schema: { type: string } }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/RateQuote' }
        '409': { $ref: '#/components/responses/Problem' }   # same key still in progress
        '422': { $ref: '#/components/responses/Problem' }   # same key, different body
        '503':
          description: Upstream saturated or circuit open
          headers:
            Retry-After: { schema: { type: integer } }
          content:
            application/problem+json:
              schema: { $ref: '#/components/schemas/Problem' }
components:
  schemas:
    RateQuoteRequest:
      type: object
      required: [origin_zip, dest_zip, weight_lb, service_level]
      properties:
        origin_zip: { type: string, pattern: '^[0-9]{5}$' }
        dest_zip: { type: string, pattern: '^[0-9]{5}$' }
        weight_lb: { type: number, exclusiveMinimum: 0, maximum: 45000 }
        service_level: { type: string, enum: [LTL_STANDARD, LTL_EXPEDITED, FTL] }
```

*Done when:* the linter passes and the file parses as 3.1.

```bash
npx @redocly/cli lint contracts/openapi.yaml
```

Expected: `Woohoo! Your API description is valid.` (or zero errors reported).

**M2 — Local legacy environment (3-4 h).** Build the stand-ins: an OpenSSH SFTP server with a chrooted read-only user, a SOAP mock with a fault-injection endpoint, and a webhook sink.

The SFTP image is `debian:trixie-slim` plus `openssh-server`, a `gateway` user with a `nologin` shell and the gateway's public key in `authorized_keys`, running `sshd -D -e`. The config that matters:

```text
# mocks/sftp/sshd_config
Port 22
PasswordAuthentication no
PubkeyAuthentication yes
Subsystem sftp internal-sftp
Match User gateway
    ChrootDirectory /srv/sftp
    ForceCommand internal-sftp -R
    AllowTcpForwarding no
    X11Forwarding no
```

`internal-sftp -R` makes the session read-only, mirroring what Meridian's security architect will grant; the chroot ownership rules are in the failure points.

The SOAP mock (published on `localhost:8080`) exposes `GET /__stats` (call and peak-concurrency counters) and `POST /__faults` accepting `{"latency_ms": 3000, "busy_rate": 0.3, "max_concurrency": 5}` to reproduce Meridian: above five concurrent requests it returns HTTP 500 with a `soapenv:Server.Busy` fault.

*Done when:* you can list the drop directory with the gateway key and the host key you pinned.

```bash
ssh-keyscan -p 2222 localhost > secrets/known_hosts
```

```bash
sftp -i secrets/gateway_ed25519 -P 2222 -o UserKnownHostsFile=secrets/known_hosts gateway@localhost:/outbound/shipments
```

Expected: an `sftp>` prompt; `put` fails with `Permission denied` because the session is read-only.

**M3 — CSV ingestion with legacy quirks (4-5 h).** Parse IBM i exports with Pydantic `mode="before"` validators so the quirks live in one place.

```python
# src/gateway/ingest/model.py
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, field_validator

STATUS = {"P": "picked", "L": "loaded", "T": "in_transit", "D": "delivered", "X": "exception"}


def parse_cyymmdd(raw: str) -> date:
    """IBM i CYYMMDD: C=0 -> 19xx, C=1 -> 20xx. '1260924' -> 2026-09-24."""
    v = raw.strip().zfill(7)
    return date(1900 + int(v[0]) * 100 + int(v[1:3]), int(v[3:5]), int(v[5:7]))


class ShipmentRow(BaseModel):
    shipment_id: str
    order_no: str
    status: Literal["picked", "loaded", "in_transit", "delivered", "exception"]
    ship_date: date
    weight_lb: Decimal

    @field_validator("shipment_id", "order_no", mode="before")
    @classmethod
    def strip_padding(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("blank key field")
        return v

    @field_validator("status", mode="before")
    @classmethod
    def map_status(cls, v: str) -> str:
        try:
            return STATUS[v.strip().upper()]
        except KeyError:
            raise ValueError(f"unknown status code {v!r}") from None

    @field_validator("ship_date", mode="before")
    @classmethod
    def ibm_date(cls, v: str) -> date:
        return parse_cyymmdd(v)
```

The poller (`asyncssh.connect(..., known_hosts=cfg.known_hosts_path)`, then `conn.start_sftp_client()`) downloads a CSV only when `<name>.done` exists, skips it if `(name, size, sha256)` is already in `ingested_files`, decodes with `raw.decode("cp1252")` and commits per 1,000-row batch. Invalid rows go to `dead_letters`; the batch continues.

*Done when:* dropping the sample file (3 good rows, 1 row with status `Q`, 1 row with a blank shipment ID) plus its `.done` file yields 3 shipments and 2 dead letters.

```bash
docker compose exec postgres psql -U gateway -d gateway -c "select count(*) from shipments; select reason from dead_letters;"
```

Expected: `3`, then two rows mentioning `unknown status code 'Q'` and `blank key field`.

**M4 — SOAP adapter with safe XML (3-4 h).** Build the envelope with a Python 3.14 t-string so every interpolated value is escaped, and parse responses with `defusedxml`.

```python
# src/gateway/soap/client.py
from string.templatelib import Interpolation, Template
from xml.sax.saxutils import escape

import httpx
from defusedxml import ElementTree as ET

from gateway.resilience import RetryableError

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
RQ_NS = "urn:meridian:ratequote:v2"


class UpstreamRejected(Exception):
    """SOAP Client fault: our request was wrong. Never retried."""


def render_xml(template: Template) -> str:
    return "".join(
        escape(str(part.value)) if isinstance(part, Interpolation) else part for part in template
    )


async def get_rate_quote(client: httpx.AsyncClient, q) -> dict[str, str]:
    body = render_xml(t"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="{SOAP_NS}" xmlns:rq="{RQ_NS}">
  <soapenv:Body><rq:GetRateQuote>
    <rq:OriginZip>{q.origin_zip}</rq:OriginZip><rq:DestZip>{q.dest_zip}</rq:DestZip>
    <rq:WeightLb>{q.weight_lb}</rq:WeightLb><rq:ServiceLevel>{q.service_level}</rq:ServiceLevel>
  </rq:GetRateQuote></soapenv:Body>
</soapenv:Envelope>""")
    try:
        resp = await client.post(
            "/RateQuoteService", content=body,
            headers={"Content-Type": "text/xml; charset=utf-8",
                     "SOAPAction": '"urn:meridian:ratequote:v2#GetRateQuote"'},
            timeout=httpx.Timeout(3.0, connect=0.5),
        )
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        raise RetryableError(type(exc).__name__) from exc
    if resp.status_code in (502, 503, 504):
        raise RetryableError(f"http {resp.status_code}")
    root = ET.fromstring(resp.content)
    fault = root.find(f".//{{{SOAP_NS}}}Fault")
    if fault is not None:
        code = (fault.findtext("faultcode") or "").strip()
        if code.endswith("Server.Busy"):
            raise RetryableError(code)
        raise UpstreamRejected(f"{code}: {fault.findtext('faultstring')}")
    result = root.find(f".//{{{RQ_NS}}}GetRateQuoteResult")
    if result is None:
        raise UpstreamRejected("response has no GetRateQuoteResult")
    return {child.tag.split("}")[1]: (child.text or "").strip() for child in result}
```

Retrying is safe only because `GetRateQuote` has no side effects; calls that create something (a booking, a payment) retry only with an upstream idempotency token, or not at all.

*Done when:* golden-fixture tests map recorded success, `Client` fault and `Server.Busy` fault responses to a dict, `UpstreamRejected` and `RetryableError`, and an origin ZIP of `<x/>` is rejected by Pydantic before any XML is built (expected: `passed`, no `failed`):

```bash
uv run pytest tests/soap -q
```

**M5 — Resilience stack (4-5 h).** Compose it outside-in: `bulkhead → retry(full jitter) → circuit breaker → per-attempt timeout`. The breaker sits inside the retry loop, so when it opens, `CircuitOpenError` (not retryable) ends the retries immediately.

```python
# src/gateway/resilience.py
import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from enum import Enum


class RetryableError(Exception): ...
class CircuitOpenError(Exception): ...
class BulkheadFull(Exception): ...


async def retry_full_jitter[T](op: Callable[[], Awaitable[T]], *, attempts: int = 3,
                               base: float = 0.2, cap: float = 2.0, deadline: float = 4.0) -> T:
    loop = asyncio.get_running_loop()
    stop_at = loop.time() + deadline
    for attempt in range(attempts):
        try:
            return await op()
        except RetryableError:
            delay = random.uniform(0, min(cap, base * 2**attempt))  # "full jitter"
            if attempt == attempts - 1 or loop.time() + delay >= stop_at:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


class State(Enum):
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, reset_after: float = 30.0) -> None:
        self.failure_threshold, self.reset_after = failure_threshold, reset_after
        self.state, self.failures, self.opened_at, self._trial = State.CLOSED, 0, 0.0, False

    async def call[T](self, op: Callable[[], Awaitable[T]]) -> T:
        if self.state is State.OPEN:
            if time.monotonic() - self.opened_at < self.reset_after:
                raise CircuitOpenError("rate-quote circuit open")
            self.state = State.HALF_OPEN
        if self.state is State.HALF_OPEN:
            if self._trial:
                raise CircuitOpenError("half-open trial in flight")
            self._trial = True
        try:
            result = await op()
        except RetryableError:
            self._trial, self.failures = False, self.failures + 1
            if self.state is State.HALF_OPEN or self.failures >= self.failure_threshold:
                self.state, self.opened_at = State.OPEN, time.monotonic()
            raise
        except BaseException:          # includes CancelledError: never leave a stuck trial
            self._trial = False
            raise
        self.state, self.failures, self._trial = State.CLOSED, 0, False
        return result
```

`Bulkhead` wraps `asyncio.Semaphore(4)`, acquiring via `asyncio.wait_for(..., timeout=0.2)`, raising `BulkheadFull` on timeout and releasing in `finally`. The breaker needs no lock (no `await` separates check from change); add one for threads or free-threaded Python. Jitter matters: the 2025-06-12 Google Cloud incident report cites missing randomized backoff as why restarting tasks overloaded Spanner in us-central1 ([source](#sources)). Map `BulkheadFull` and `CircuitOpenError` to `503` Problem Details with `Retry-After`.

*Done when:* with the mock set to `{"busy_rate": 1.0}`, the breaker opens after 5 failed attempts (the second call, since each call retries), every later call returns `503` in under 10 ms, and the mock's request log shows no more than 4 concurrent requests during a 50-VU k6 burst.

**M6 — Idempotency keys (3 h).** Store the key, a SHA-256 of the canonical request body, the state and the stored response.

```sql
CREATE TABLE idempotency_keys (
  client_id     text        NOT NULL,
  key           text        NOT NULL,
  request_hash  bytea       NOT NULL,
  status        text        NOT NULL CHECK (status IN ('in_progress', 'completed')),
  response_code int,
  response_body jsonb,
  locked_until  timestamptz NOT NULL DEFAULT now() + interval '30 seconds',
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, key)
);
```

```python
# src/gateway/idempotency.py
from gateway.errors import ProblemError  # maps to RFC 9457 responses


async def begin(conn, client_id: str, key: str, request_hash: bytes):
    cur = await conn.execute(
        """INSERT INTO idempotency_keys (client_id, key, request_hash, status)
           VALUES (%s, %s, %s, 'in_progress')
           ON CONFLICT (client_id, key) DO UPDATE
             SET locked_until = now() + interval '30 seconds'
             WHERE idempotency_keys.status = 'in_progress'
               AND idempotency_keys.locked_until < now()
               AND idempotency_keys.request_hash = EXCLUDED.request_hash
           RETURNING key""",
        (client_id, key, request_hash),
    )
    if await cur.fetchone() is not None:
        return None                     # we own the key: call upstream
    cur = await conn.execute(
        "SELECT request_hash, status, response_code, response_body FROM idempotency_keys"
        " WHERE client_id = %s AND key = %s", (client_id, key))
    stored_hash, status, code, body = await cur.fetchone()
    if stored_hash != request_hash:
        raise ProblemError(422, "idempotency-key-reused", "Key was used with a different body")
    if status == "in_progress":
        raise ProblemError(409, "request-in-progress", "Retry shortly", retry_after=2)
    return code, body                   # replay the stored response
```

`DO UPDATE ... WHERE locked_until < now()` lets a new request take over a key whose worker crashed mid-flight instead of returning `409` forever.

*Done when:* 20 concurrent identical requests with one key produce exactly one upstream call and 20 identical response bodies, some after `409` retries. Read the mock's counter (expected: `{"calls": 1}`):

```bash
curl -s localhost:8080/__stats
```

**M7 — Signed webhook fan-out (4-5 h).** The transaction that upserts `shipments` also inserts one `webhook_deliveries` row per matching subscription: the outbox. The dispatcher claims due rows with `FOR UPDATE SKIP LOCKED`, so replicas never send the same row. Signatures follow Standard Webhooks: HMAC-SHA256 over `{webhook-id}.{webhook-timestamp}.{body}`, keyed with the base64-decoded part of a `whsec_` secret.

```python
# src/gateway/webhooks/signing.py
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
```

`webhook-signature` can carry several space-separated signatures, so you rotate a secret by signing with old and new for 24 hours, then dropping the old one. Delivery rules: HTTPS only, `follow_redirects=False`, 5 s total timeout; `2xx` is delivered, `410 Gone` disables the subscription; anything else retries with full jitter from 30 s, doubling to a 6 h cap, and dead-letters after 72 h. Before every attempt, resolve the host and refuse non-public addresses:

```python
# src/gateway/webhooks/ssrf.py
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


async def assert_public_https(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("webhook URL must be https")
    infos = await asyncio.get_running_loop().getaddrinfo(
        parts.hostname, parts.port or 443, type=socket.SOCK_STREAM
    )
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:  # RFC 1918, loopback, link-local incl. 169.254.169.254, ULA
            raise ValueError(f"webhook target resolves to non-public address {ip}")
```

The check leaves a DNS-rebinding window between resolution and connection. In production, close it with an allow-listing egress proxy that resolves for itself, and record the gap in the threat model.

*Done when* the sink verifies 100% of signatures; stopping it for 10 minutes shows growing, jittered retry gaps; with `WEBHOOK_MAX_AGE=120s` the row lands in `dead_letters`; and a replay delivers it once the sink is back.

```bash
docker compose logs webhook-sink --since 15m | grep -c "signature=valid"
```

**M8 — Contract tests and packaging (3 h).** Run property-based conformance tests against the running service; build one image that runs the API or either worker.

```bash
uvx schemathesis run contracts/openapi.yaml --url http://localhost:8000 -H "X-API-Key: dev-shipper-key" --checks all
```

*Done when:* Schemathesis reports zero failures, and `docker image ls meridian-gateway` shows one image under 200 MB running as non-root (`USER 10001`).

### 6. Deployment instructions

Order: keys, legacy mocks, pinned host key, migrations, API and workers, smoke test.

| Variable | Example | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://gateway:gateway@postgres:5432/gateway` | Postgres DSN |
| `SFTP_HOST` / `SFTP_PORT` | `sftp` / `22` | Drop server as seen from inside the Compose network |
| `SFTP_KEY_PATH` / `SFTP_KNOWN_HOSTS` | `/run/secrets/gateway_ed25519` / `/run/secrets/known_hosts` | Key auth and the pinned host key |
| `SOAP_BASE_URL` | `http://soap-mock:8080` | Rate-quote service |
| `SOAP_MAX_CONCURRENCY` | `4` | Bulkhead size (ADR-P01-1) |
| `WEBHOOK_MAX_AGE` | `72h` | Retry horizon before dead-lettering |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-lgtm:4317` | Telemetry (P04 replaces the target) |

Generate the gateway's SFTP key and hand the public half to the mock:

```bash
ssh-keygen -t ed25519 -N "" -f secrets/gateway_ed25519
```

```bash
cp secrets/gateway_ed25519.pub mocks/sftp/gateway_ed25519.pub
```

Start the legacy stand-ins and Postgres:

```bash
docker compose up -d --build postgres sftp soap-mock webhook-sink
```

Pin the host key under the name the workers use (`sftp`, not `localhost:2222`):

```bash
docker compose exec -T sftp sh -c 'echo "sftp $(cut -d" " -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"' > secrets/known_hosts
```

Apply migrations, then start the API and workers:

```bash
docker compose run --rm gateway-api python -m gateway.migrate
```

```bash
docker compose up -d gateway-api sftp-poller webhook-dispatcher
```

Verify readiness (expected: `{"status":"ready","db":"ok","sftp":"ok","soap_circuit":"closed"}`):

```bash
curl -fsS localhost:8000/readyz
```

Smoke-test a quote twice: the second call must return the same body with no second upstream call.

```bash
curl -fsS -X POST localhost:8000/v1/rate-quotes -H "X-API-Key: dev-shipper-key" -H "Idempotency-Key: smoke-0001-aaaa-bbbb" -H "Content-Type: application/json" -d '{"origin_zip":"30301","dest_zip":"60601","weight_lb":1200,"service_level":"LTL_STANDARD"}'
```

**Rollback.** Images are tagged with the Git SHA, and migrations are additive only (the expand/contract rule from P03), so a rollback never needs a down migration:

```bash
GATEWAY_TAG=$(git rev-parse --short HEAD~1) docker compose up -d gateway-api sftp-poller webhook-dispatcher
```

**Teardown** (removes volumes; destroy any optional AWS instance the same day):

```bash
docker compose down -v --remove-orphans
```

### 7. Testing & validation

| Layer | What you test | Tool | Pass threshold |
|---|---|---|---|
| Unit and golden fixtures | `CYYMMDD` parser, status map, signer/verifier (incl. rotation), breaker states, retry deadline; recorded SOAP faults; CP1252 files with `é`, `ñ`, `£` | pytest 9 | 100% pass; branch coverage ≥ 90% on `ingest/`, `resilience.py`, `webhooks/` |
| Integration | Compose stack: file lands, rows queryable, webhooks fire | pytest | 99% of rows visible within 5 min; re-dropped file creates zero duplicates |
| Contract | OpenAPI conformance | Schemathesis 4.28 | Zero failures |
| Load | 200 req/s reads for 5 min; 50 VUs of quotes at 2.8 s mock latency | k6 v2.3 | Read p95 < 150 ms; mock never sees > 4 concurrent calls; non-`503` errors < 0.1% |
| Chaos | Kill `soap-mock`; stop Postgres 30 s; restart the poller mid-way through a 20,000-row file | `docker compose kill/stop` | Breaker opens in < 10 s and recloses; the file is ingested exactly once |
| Security | SSRF table (`127.0.0.1`, `169.254.169.254`, `[::1]`, a name resolving to `10.0.0.5`, a 302 to an internal host); billion-laughs and XXE; `pip-audit`; image scan | pytest, ruff `S`, Grype or Trivy pinned by digest | Every SSRF and XXE case blocked; zero critical CVEs |

### 8. Observability & operations

Instrument with `opentelemetry-instrument` plus the FastAPI, httpx and psycopg instrumentations, and set `OTEL_SEMCONV_STABILITY_OPT_IN=http` so HTTP metrics use the stable names. Add these custom metrics:

| Metric | Type | Why |
|---|---|---|
| `gateway.upstream.inflight` | gauge | Proves the bulkhead holds at 4 |
| `gateway.circuit.state` | gauge (0 closed, 1 half-open, 2 open) | First thing to check when quotes fail |
| `gateway.ingest.rows` | counter by `outcome` (`upserted`, `dead_lettered`, `duplicate`) | Data-quality trend per file |
| `gateway.ingest.lag` | histogram (s) from `.done` mtime to commit | The 5-minute freshness SLI |
| `gateway.webhook.delivery.age` | histogram (s) from event to 2xx | The 60 s delivery SLI |
| `gateway.dead_letters.open` | gauge by `kind` (`row`, `webhook`) | Ops backlog |

Alerts:
- **IngestStale (page):** no file ingested for 30 min between 05:00 and 23:00 site time (the job runs every 15 min).
- **CircuitOpen (ticket):** open for more than 5 min. Notify Meridian's integration on-call.
- **WebhookBacklogOld (ticket):** the oldest pending delivery is older than 15 min.
- **DeadLettersGrowing (ticket):** more than 50 new dead letters in 1 h.

Runbook entries (in `docs/runbooks/`):
1. **Circuit open.** `Server.Busy` faults mean load: confirm the bulkhead held. Connect errors mean network: test Direct Connect from the gateway subnet before calling Meridian.
2. **Host key mismatch.** Never disable verification. Confirm with Meridian's security architect that the host was rebuilt, get the fingerprint through a second channel, update `known_hosts`.
3. **Dead-letter spike.** Group by `reason`. A new status code (such as `Q`) means the IBM i team added one: map it, deploy, replay.

### 9. Security & compliance

| Threat (STRIDE) | Scenario | Control implemented |
|---|---|---|
| Spoofing | Stolen shipper API key | Keys stored as SHA-256 hashes, scoped per shipper, rotated with overlap; `ops` scope separate |
| Tampering | Forged webhook to a shipper; XXE or entity expansion in SOAP responses | Standard Webhooks HMAC, 5-minute timestamp tolerance, two-signature rotation; `defusedxml` for every parse |
| Repudiation | "We never replayed that" | Audit table of every replay: ops user, dead-letter ID, time |
| Information disclosure | Shipper A reads shipper B's shipments (BOLA, OWASP API1:2023) | Every query filters on `client_id` from the authenticated key; a test enumerates foreign IDs and expects `404` |
| Denial of service | Retry storm takes down the SOAP service | Bulkhead of 4, breaker, 64 KB body limit, per-key rate limit |
| Elevation of privilege | Webhook URL pointed at the cloud metadata service | SSRF guard, no redirects, egress proxy in production |

SSRF sits under A01 Broken Access Control in the OWASP Top 10:2025; a breaker that fails open is an A10 (Mishandling of Exceptional Conditions) problem. Consignee names and addresses are personal data: keep `shipments` 90 days, redact addresses from logs, and give Meridian the data-flow diagram.

### 10. Extensions for advanced learners

1. **T3 — OAuth 2.0 client credentials** (for example Keycloak 26.7, following RFC 9700). *Hard because* live shippers must migrate without a flag day, and token validation and key rotation sit on every request.
2. **T3 — Per-shipper fair share of the SOAP bulkhead.** *Hard because* fairness and utilization pull against each other; naive designs starve small shippers or idle slots.
3. **T3 — EDI X12 214 output** for shippers who cannot consume JSON. *Hard because* every trading partner bends the standard, and you need 997 acknowledgments.
4. **T4 — BYOC deployment in Meridian's AWS account** with OpenTofu, Direct Connect and PrivateLink (P05, P07). *Hard because* private DNS, routing and least-privilege IAM must be right first time in an account you do not own.
5. **T4 — Close the DNS-rebinding gap** with an egress proxy and per-subscription allow-lists. *Hard because* the proxy becomes a tier-0 dependency that must fail closed without losing deliveries.

### 11. How to demonstrate it in interviews

**2-minute pitch.** "In a composite 3PL, an AS/400 dropped CSVs over SFTP, a SOAP rate service fell over above five concurrent calls, and nothing on the IBM i side could change, so everything lives in a gateway. I wrote the OpenAPI 3.1 contract first. Files are ingested once by hash, bad rows are dead-lettered for replay, and the SOAP service sits behind a bulkhead of four, full-jitter retries and a circuit breaker. Postgres idempotency keys stop a retried quote reaching upstream twice, and webhooks go through an outbox with Standard Webhooks signatures and an SSRF guard. Under a 50-VU burst the mock never saw more than 4 concurrent calls; read p95 stayed under 150 ms at 200 req/s."

**10-minute demo flow.**
1. Diagram and trust boundary (1 min).
2. `contracts/openapi.yaml` and Problem Details (1 min).
3. A CSV with a bad status code: 3 rows ingested, 2 dead letters (1.5 min).
4. One quote twice with the same `Idempotency-Key`; the mock's counter stays at 1 (1 min).
5. `busy_rate: 1.0` plus the k6 burst: `503 Retry-After`, in-flight gauge flat at 4 (2 min).
6. Stop the webhook sink, show jittered retries, replay a dead letter (2 min).
7. The SSRF test table live, then what you would change (1.5 min).

**Likely questions and strong-answer outlines.**
1. *"Why not just retry harder?"* Retries multiply load on a failing system (3 attempts × 50 clients against 5 slots); cite the Google Cloud 2025-06-12 herd effect. Budget, jitter, bound concurrency.
2. *"Is ingestion exactly-once?"* Effectively-once: file hash, per-batch transactions and idempotent upserts, so a crash mid-file replays safely.
3. *"Two requests with the same key at once?"* One wins the `INSERT`; the other gets `409` with `Retry-After`; `locked_until` handles a crashed owner.
4. *"Why Postgres rather than Kafka for webhooks?"* At 250k rows a day a `SKIP LOCKED` table is transactional and needs no extra operations; name the volume that would change your mind.
5. *"How do you know the SOAP limit is 5?"* Measured in a joint test window and confirmed in writing; the bulkhead is 4 to leave headroom for Meridian's own callers.

**Artifacts to bring:** diagram, ADRs, the OpenAPI file, a k6 summary with concurrency held at 4, a breaker open/close screenshot, and a one-page postmortem of the poller restarting mid-file.

**Metrics to quote:** read p95 at 200 req/s; peak upstream concurrency (4); duplicate upstream calls under 20 concurrent retries (0); webhook p99 delivery age; freshness p99.

**What you would do differently:** offer an asynchronous quote API (`202` plus a webhook), because the upstream p99 of 2.8 s will not improve; add the egress proxy on day one; get the SOAP concurrency limit in writing first.

### 12. Common failure points while building

| Failure | Symptom | Fix |
|---|---|---|
| Chroot directory permissions | `bad ownership or modes for chroot directory`; session closes | `/srv/sftp` owned by root, not group- or world-writable; only subdirectories belong to the user |
| Host key pinned under the wrong name | Works from the laptop, fails in Compose with `Host key is not trusted` | Pin it for `sftp`, not `[localhost]:2222`; mount host keys from a volume so rebuilds keep the identity |
| Decoding with UTF-8 | `UnicodeDecodeError` on `é`, or silent mojibake | Decode `cp1252` explicitly; add a non-ASCII golden file |
| Breaker stuck half-open | Quotes fail forever after one trial timeout | Reset the trial flag on *every* exit path, including `CancelledError` |
| Idempotency hash over raw bytes | Reordered JSON keys get `422` | Hash `json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))` |

**See also:** [M04 API design and integration](../../01-curriculum/M04-api-design-and-integration.md) (REST, pagination, versioning).

---
