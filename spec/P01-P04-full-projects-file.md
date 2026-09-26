# Projects P01-P04: Integration, Microservices, Zero-Downtime Delivery, Observability

> **Purpose:** the four foundation projects every Beacon FDE builds before touching a customer environment: wrap a legacy system behind a clean API, build the polyglot reference app (Freightline), ship it with zero downtime, and make it observable under any customer's monitoring mandate.
> **Who it's for:** engineers moving into FDE work from SWE, DevOps/SRE, QA/test automation, solutions engineering or data engineering. P01 is the on-ramp (T1-T2); P02-P04 take you to T3.
> **Time to complete:** 130-180 hours across all four (P01 25-35 h, P02 50-70 h, P03 30-40 h, P04 25-35 h).
> **Prerequisites:** Docker Engine 29.x (or Podman 6.x), `kubectl`, `kind`, Helm 4, Git, a GitHub account and each project's listed modules at the stated tier (the minimum tier to start; the project carries you toward the next one). All four run on a 16 GB laptop at $0 cloud cost; versions below are as of September 2026.
> **Fiction notice:** Beacon, Meridian Freight, Cobalt Bank, Northstar Retail and Freightline are fictional composites. Real products, incidents and standards are named as themselves and cited in [Sources](#sources).

P02 defines **Freightline**, the reference application that P03, P04 and most later projects (P06, P08, P09, P10, P17, P21, P22, P24, P27, P28) deploy, break, migrate and harden.

| ID | Title | Tier | Hours | Categories covered |
|---|---|---|---|---|
| [P01](#p01--legacy-integration-gateway) | Legacy Integration Gateway | T1-T2 | 25-35 | API integration; microservices intro |
| [P02](#p02--freightline-polyglot-microservices) | Freightline polyglot microservices | T2-T3 | 50-70 | Production-grade microservices |
| [P03](#p03--zero-downtime-delivery-pipeline) | Zero-downtime delivery pipeline | T2-T3 | 30-40 | Zero-downtime deployments; CI/CD |
| [P04](#p04--observability-in-a-box) | Observability-in-a-box | T2 | 25-35 | Observability dashboards |

Jump to: [Sources](#sources) · [Related / Next](#related--next).

---

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

## P02 — Freightline Polyglot Microservices

| Field | Value |
|---|---|
| ID | P02 |
| Tier | **T1 Beginner** (P02 lite: M1-M2, about 11 h, no Kafka; see the [T1 on-ramp](README.md#4-dependency-graph-and-build-orders)) → **T2 Intermediate** (M3-M9) → **T3 Advanced** |
| Time estimate | 50-70 hours |
| Industry framing | Meridian Freight (fictional) order-to-cash, packaged as the reference application Beacon's FDE team deploys into every later environment |
| Required categories covered | Production-grade microservices |
| Languages | Go 1.27 (orders), Python 3.14 (inventory), TypeScript 7.0 on Node.js 24 LTS (billing, notifications), SQL, Protobuf |
| Cloud(s) | None. It runs on kind. P05-P08 move it to AWS, Azure and GCP |
| Estimated cost / keeping it near $0 | $0. Budget about 10 GB of RAM (kind, Kafka, four Postgres, four services, gateway). To cut it, run one replica per service and Redpanda in dev mode; `docker stop freightline-control-plane` when idle |
| Prerequisites | [C01](../02-curriculum/C01-python-mastery.md) T2, [C02](../02-curriculum/C02-go-mastery.md) T2, [C03](../02-curriculum/C03-typescript-mastery.md) T2, [C04](../02-curriculum/C04-systems-design-and-distributed-systems.md) T2, [C06](../02-curriculum/C06-databases-and-storage.md) T2, [C07](../02-curriculum/C07-api-design-and-integration.md) T2, [C09](../02-curriculum/C09-containers-and-kubernetes.md) T2; P01 recommended |

### 1. Problem statement

**Business context (composite scenario).** Meridian Freight invoices from a nightly batch: the AS/400 hands orders to a 2011 .NET billing application that issues invoices the next morning, so credit holds are a day stale. Twice last quarter a customer over its limit received a full truckload, and a month-end billing report once locked the shared orders table for 40 minutes during peak picking. Meridian wants an event-driven order-to-cash slice proven before funding wider modernization (P11).

**Constraints.**
- **Team ownership.** The data team owns inventory (Python), finance systems owns billing and notifications (TypeScript), and the platform team prefers Go. Each service owns its database and no service reads another's tables; the month-end lock is why.
- **Deployment reality.** It must install on a customer cluster with no runtime internet access (P16, P19): no remote config fetches or plugin downloads at start-up.
- **Operations.** Warehouses run 24x7, so dependency failures must degrade, not cascade.
- **Telemetry.** OpenTelemetry-native, because each customer mandates a different backend (P04).

**Stakeholders.** Meridian's VP Finance (invoice timeliness), the warehouse operations director (stock accuracy), Meridian's platform team (runs the cluster), Beacon's FDE lead and Beacon product management (wants the app reusable).

**Measurable success criteria.**
1. From order placed to invoice issued, p95 < 5 s. Today it takes until the next morning.
2. Across 1,000 orders placed while a random pod is killed every 30 s, zero orders are lost and zero invoices are duplicated.
3. `POST /v1/orders` p99 < 300 ms at 100 req/s.
4. If inventory, billing or Kafka is down for 60 s, reads return no 5xx, and writes either succeed or return `503` with `Retry-After` within 1 s.
5. `helm install` on a fresh kind cluster reaches all-ready in under 10 minutes.

### 2. Requirements

**Functional requirements**
- **FR-1** `POST /v1/orders` requires an `Idempotency-Key` (the P01 pattern), creates a `PENDING` order and returns `202` with `Location`.
- **FR-2** `GET /v1/orders/{id}` returns the order. Transitions: `PENDING → CONFIRMED | REJECTED`, `CONFIRMED → INVOICED | CANCELLED`.
- **FR-3** On `orders.order-placed.v1`, inventory reserves every line or none and emits `stock-reserved` or `stock-rejected`. It serves `GET /v1/stock/{sku}`.
- **FR-4** Orders runs a soft pre-check, `InventoryService.CheckAvailability`, with a 300 ms deadline; on timeout it accepts and the saga decides.
- **FR-5** On `orders.order-confirmed.v1`, billing issues an invoice and emits `billing.invoice-issued.v1`. It serves `GET /v1/invoices/{id}` and the `BillingService.GetInvoice` RPC.
- **FR-6** Notifications sends an email (Mailpit in the lab) and a signed webhook (the P01 signer) for confirmed, rejected and invoiced orders, calling `GetInvoice` to render the email.
- **FR-7** Every state change and its event commit atomically through a transactional outbox; every consumer dedupes through an inbox keyed on event ID.
- **FR-8** After 5 failed attempts a poison message goes to `dlq.<service>.v1`, with the error and original topic, partition and offset in headers.
- **FR-9** Every service exposes `/healthz` (liveness) and `/readyz` (readiness) and shuts down within 25 s of `SIGTERM`.
- **FR-10** W3C trace context flows across HTTP, RPC and Kafka.

**Non-functional requirements**

| Attribute | Target |
|---|---|
| Latency | `POST /v1/orders` p99 < 300 ms; `GET` p99 < 100 ms at 100 req/s |
| Throughput | 100 orders/s sustained for 30 min on a laptop kind cluster |
| Event lag | Outbox commit to consumer handled, p99 < 2 s |
| Availability | Order API 99.9% monthly (the SLO in P04) |
| RPO / RTO | RPO 0 for committed orders (synchronous commit; in the lab this depends on one PVC, so say so); RTO 5 min for pod or node loss |
| Shutdown | Zero failed requests during a rolling restart under load (proved in P03) |
| Cost | $0 locally; each service fits in 256 Mi requests |

**Constraints.** No shared database. Images are non-root with a read-only root filesystem. All configuration comes from env vars or mounted files. No runtime internet egress.

**Out of scope.** Payments, end-user authentication (P23 adds it at the edge), multi-region (P08), a schema registry (an extension), and any UI beyond Mailpit.

### 3. Architecture

```text
                    CLIENTS (Meridian apps, k6)          ZONE: edge (namespace envoy-gateway-system)
                              |  HTTPS / REST                  +-------------------------------------+
                              v                                | Envoy Gateway 1.9 (Gateway API 1.6) |
                    +--------------------+                     | Gateway "freightline" :80/:443      |
                    |  HTTPRoutes        |<--------------------+ timeouts, retries (GET only), CB    |
                    +--+--------+-----+--+                     +-------------------------------------+
   /v1/orders*  -------+        |     +------- /v1/invoices*
                       |  /v1/stock*  |
 ===== TRUST BOUNDARY: namespace freightline (default-deny NetworkPolicy, PSA "restricted") =========
                       v        v     v
  +----------------+  gRPC  +------------------+     +------------------+ Connect +-----------------+
  | orders  (Go)   |------->| inventory (Py)   |     | billing (TS)     |<--------| notifications   |
  | :8080 REST     | pre-   | :8080 REST       |     | :8080 REST       | GetInv. | (TS) :8080      |
  | :50051 RPC     | check  | :50051 gRPC      |     | :50051 RPC       |         | SMTP -> Mailpit |
  | outbox relay   | 300 ms | outbox relay     |     | outbox relay     |         | webhook signer  |
  +---+--------^---+        +---+---------^----+     +---+----------^---+         +---+---------^---+
  +---v----+   |            +---v------+  |          +---v-----+    |             +---v-----+   |
  |orders- |   |            |inventory-|  |          |billing- |    |             |notif-db |   |
  |db PG18 |   |            |db PG18   |  |          |db PG18  |    |             |PG18     |   |
  +---+----+   |            +---+------+  |          +---+-----+    |             +---------+   |
      | outbox |                | outbox  |              | outbox   |  (inbox tables everywhere) |
      v        |                v         |              v          |                            |
 ===== ZONE: data plane (namespace freightline-data) ===============================================
  +---------------------------------------------------------------------------------------------+
  | Apache Kafka 4.3.1 (KRaft, single node in lab; 3 brokers + RF3 in prod)  :9092              |
  |  orders.order-placed.v1  orders.order-confirmed.v1  orders.order-rejected.v1                |
  |  orders.order-cancelled.v1  inventory.stock-reserved.v1  inventory.stock-rejected.v1        |
  |  billing.invoice-issued.v1   dlq.{orders,inventory,billing,notifications}.v1                |
  +---------------------------------------------------------------------------------------------+
  All services --OTLP gRPC :4317--> otel-gateway.observability (P04)   stdout JSON logs --> agent
```

**Reference contract.** Later projects depend on these names; change them only with a version bump.

| Service | Language | REST + health | RPC (h2c) | Database | Publishes | Consumes (group ID = service name) |
|---|---|---|---|---|---|---|
| `orders` | Go 1.27 | 8080 | 50051 `OrderService` (Connect + gRPC); pprof on `127.0.0.1:6060` | `orders-db` / `orders` | `orders.order-placed.v1`, `orders.order-confirmed.v1`, `orders.order-rejected.v1`, `orders.order-cancelled.v1` | `inventory.stock-reserved.v1`, `inventory.stock-rejected.v1`, `billing.invoice-issued.v1` |
| `inventory` | Python 3.14 | 8080 | 50051 `InventoryService` (gRPC); headless Service `inventory-headless` for client-side balancing | `inventory-db` / `inventory` | `inventory.stock-reserved.v1`, `inventory.stock-rejected.v1` | `orders.order-placed.v1`, `orders.order-cancelled.v1` |
| `billing` | TS 7 / Node 24 | 8080 | 50051 `BillingService` (Connect + gRPC) | `billing-db` / `billing` | `billing.invoice-issued.v1` | `orders.order-confirmed.v1` |
| `notifications` | TS 7 / Node 24 | 8080 | none | `notifications-db` / `notifications` | none | `orders.order-confirmed.v1`, `orders.order-rejected.v1`, `billing.invoice-issued.v1` |
| `kafka` | Apache Kafka 4.3.1 | 9092 client, 9093 controller | | | | |
| `mailpit` | Mailpit 1.31 | 1025 SMTP, 8025 UI | | | | |

**Topics.** Every event is keyed by `order_id`, so all events for one order land on one partition in order. Lab: 6 partitions, RF 1. Production: 12 partitions, RF 3, `min.insync.replicas=2`. Retention is 7 days, except 30 days for `billing.invoice-issued.v1` and the DLQs. Each value is a CloudEvents 1.0 structured JSON envelope, with `traceparent` carried as a Kafka header:

```json
{
  "specversion": "1.0",
  "id": "01926f3e-8b1a-7c3d-9e2f-4a5b6c7d8e9f",
  "source": "freightline/orders",
  "type": "freightline.orders.order-placed.v1",
  "subject": "ord_01926f3e8b1a",
  "time": "2026-09-24T14:03:11.412Z",
  "datacontenttype": "application/json",
  "data": { "order_id": "ord_01926f3e8b1a", "customer_id": "C-100", "lines": [{ "sku": "SKU-7", "quantity": 2 }], "total_cents": 18400 }
}
```

The `id` is the outbox row's `uuidv7()`. Consumers deduplicate on it.

**Repository layout.**

```text
freightline/
├── proto/freightline/{orders,inventory,billing}/v1/*.proto   # buf-managed, generated code committed
├── buf.yaml  buf.gen.yaml
├── contracts/openapi/{orders,inventory,billing}.yaml         # external REST, OpenAPI 3.1
├── contracts/events/*.schema.json                            # JSON Schema per event type
├── services/orders/          # Go: cmd/orders, internal/{api,rpc,store,outbox,consumer}, db/migrations
├── services/inventory/       # Python: src/inventory/{api,rpc,consumer,outbox}, db/migrations, pyproject.toml, uv.lock
├── services/billing/         # TS: src/{main.ts,invoices.ts,gen/}, db/migrations, package.json, pnpm-lock.yaml
├── services/notifications/   # TS: same shape as billing
├── deploy/helm/freightline-service/   # library chart: Deployment/Rollout, Services, PDB, NetworkPolicy
├── deploy/helm/freightline/           # umbrella chart: four services, Kafka, Postgres, Mailpit, topic Job
├── deploy/kind/kind-config.yaml
├── deploy/envs/{kind,customer-*}/values*.yaml   # P03 GitOps promotes digests here
├── tests/{contract,e2e,chaos,load}/
└── docs/{adr,runbooks}/
```

| Component | Responsibility | Technology | Why | Alternative considered |
|---|---|---|---|---|
| Edge gateway | TLS, routing, timeouts, GET retries | Envoy Gateway 1.9 | Ingress-NGINX was retired 2026-03-24; Gateway API is the default | Istio or Cilium gateway |
| `orders` | Order aggregate, saga owner | Go, pgx v5, connect-go, franz-go | Hot path; lowest latency and memory | Python |
| `inventory` | Stock and reservations | Python, FastAPI, psycopg 3, aiokafka, grpcio | The data team's language | Go (takes ownership away) |
| `billing`, `notifications` | Invoices, communications | TS, Fastify 5, connect-es 2, `@confluentinc/kafka-javascript` | Finance team's language; KafkaJS has had no release since 2023 | NestJS (heavier) |
| Event bus | Durable ordered events | Apache Kafka 4.3.1, KRaft | The Kafka API is what customers run (MSK, Event Hubs, Confluent) | Redpanda 26.2 (dev profile) |
| Databases | One per service | PostgreSQL 18.6 | `uuidv7()` time-ordered event IDs | One shared cluster (month-end coupling) |
| Outbox relays | Publish committed events | In-process poller + advisory lock | No extra infrastructure | Debezium 3.6 CDC (P10) |

**Mini-ADRs**

| # | Decision | Options | Choice | Consequences |
|---|---|---|---|---|
| ADR-P02-1 | How state changes and events stay consistent | Dual write (DB then Kafka); Kafka transactions; transactional outbox | Outbox table in each service's database, relayed by a poller | Nothing is lost on a crash. Delivery is at-least-once, so every consumer needs an inbox. Adds 5-50 ms of publish latency |
| ADR-P02-2 | Outbox relay topology | Many relays with `SKIP LOCKED`; one relay per service via `pg_try_advisory_xact_lock`; Debezium | One active relay per service (advisory lock), `SKIP LOCKED` as a guard | Per-key ordering holds; throughput is bounded by one relay (about 2k events/s in the lab), which is enough |
| ADR-P02-3 | Internal protocol | REST/JSON; gRPC; Connect | Protobuf over Connect and gRPC on port 50051 (h2c) | One schema, generated clients in three languages, `buf breaking` in CI. HTTP/2 pinning needs `max_connection_age` |
| ADR-P02-4 | Saga style | Orchestrated; choreographed | Choreography, with orders owning order status | No workflow engine; the state machine lives in one table and every event is documented in `contracts/events/` |

### 4. Tools & technologies

| Tool | Version / status (as of September 2026) | Notes |
|---|---|---|
| Go | 1.27.1 (1.26 supported; older unsupported) | Container-aware GOMAXPROCS since 1.25: drop `automaxprocs` |
| TypeScript / Node.js | TS 7.0 (Go-native `tsc`) / Node 24 LTS | No compiler API until TS 7.1 (keep TS 6 for typescript-eslint); Node 26 becomes LTS 2026-10-28 |
| pnpm | 11.x | 1-day `minimumReleaseAge` default; list the Kafka client's native build in `allowBuilds` |
| connect-go / connect-es / grpcio / buf | 1.21.x / 2.2.x / 1.84.x / 1.73.x | connect-es 2 needs only `protoc-gen-es` |
| franz-go / aiokafka / `@confluentinc/kafka-javascript` | 1.22.x / 0.14.x / 1.10.x | One producing service per topic (partitioners differ) |
| Apache Kafka | 4.3.1 (KRaft only; share groups production-ready since 4.2) | Confluent is IBM-owned (closed 2026-03-17) |
| PostgreSQL / dbmate | 18.6 / 2.36.0 | PG 19 is still beta. dbmate: plain-SQL migrations for three languages |
| Envoy Gateway | 1.9.1 (Gateway API v1.6.1; tested on Kubernetes 1.33-1.36) | Why the kind node is pinned to 1.36, not 1.37 |
| kind / Helm | 0.33.0 (default node v1.37.0) / 4.3.x | Pin node images by digest. Helm 4 uses server-side apply; Helm 3 security fixes end 2027-02-10 (extended from 2026-11-11) |

### 5. Step-by-step implementation plan

**M1 — Contracts first (5 h).** Write the protos, the three OpenAPI files and one JSON Schema per event before any service code. Commit the generated code, because air-gapped builds (P16, P19) cannot reach the Buf Schema Registry.

```proto
// proto/freightline/inventory/v1/inventory.proto
syntax = "proto3";

package freightline.inventory.v1;

option go_package = "example.com/freightline/gen/go/freightline/inventory/v1;inventoryv1";

service InventoryService {
  // Soft pre-check. Never reserves; the saga decides.
  rpc CheckAvailability(CheckAvailabilityRequest) returns (CheckAvailabilityResponse);
}

message Line {
  string sku = 1;
  int32 quantity = 2;
}

message CheckAvailabilityRequest {
  repeated Line lines = 1;
}

message CheckAvailabilityResponse {
  bool all_available = 1;
  repeated string short_skus = 2;
}
```

*Done when* lint passes and there are no breaking changes against `main`:

```bash
buf lint
```

```bash
buf breaking --against '.git#branch=main'
```

**M2 — `orders` skeleton with real shutdown semantics (6 h).** Get readiness, graceful shutdown and h2c right before business logic; every later project relies on them.

```go
// services/orders/cmd/orders/main.go (lifecycle only)
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	pool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Error("db pool", "err", err)
		os.Exit(1)
	}
	var ready atomic.Bool
	mux := http.NewServeMux()
	registerAPI(mux, pool) // POST /v1/orders, GET /v1/orders/{id}
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		if !ready.Load() || pool.Ping(r.Context()) != nil {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	})

	var h2c http.Protocols
	h2c.SetHTTP1(true)
	h2c.SetUnencryptedHTTP2(true) // net/http h2c (API since Go 1.24): gRPC/Connect over cleartext HTTP/2
	servers := []*http.Server{
		{Addr: ":8080", Handler: otelhttp.NewHandler(mux, "orders"), ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second},
		{Addr: ":50051", Handler: rpcHandler(pool), Protocols: &h2c, ReadHeaderTimeout: 5 * time.Second},
	}
	for _, s := range servers {
		go func() {
			if err := s.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
				log.Error("listen", "addr", s.Addr, "err", err)
				stop()
			}
		}()
	}
	ready.Store(true)

	<-ctx.Done() // SIGTERM arrives after the preStop sleep (M8)
	ready.Store(false)
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	for _, s := range servers {
		_ = s.Shutdown(shutdownCtx) // stop accepting, finish in-flight, GOAWAY on HTTP/2
	}
	pool.Close()
	log.Info("shutdown complete")
}
```

*Done when* `kubectl delete pod` during a 50 req/s loop logs `shutdown complete` in under 25 s with no failed requests (P03 turns this into a formal k6 gate).

**M3 — Transactional outbox in `orders` (6 h).** The order row and its event commit in one transaction; the relay publishes afterwards.

```sql
-- services/orders/db/migrations/20260901000002_outbox.sql
-- migrate:up
CREATE TABLE outbox (
  id           uuid        PRIMARY KEY DEFAULT uuidv7(),
  topic        text        NOT NULL,
  msg_key      text        NOT NULL,
  payload      jsonb       NOT NULL,
  traceparent  text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
);
CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

-- migrate:down
DROP TABLE outbox;
```

```go
// services/orders/internal/outbox/relay.go (one publish cycle)
func (r *Relay) publishBatch(ctx context.Context) (int, error) {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck // no-op after Commit

	var leader bool // one active relay per service keeps per-key order (ADR-P02-2)
	if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock(hashtext('orders-outbox'))`).Scan(&leader); err != nil || !leader {
		return 0, err
	}
	rows, err := tx.Query(ctx, `SELECT id::text, topic, msg_key, payload, coalesce(traceparent, '')
		FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT 200 FOR UPDATE SKIP LOCKED`)
	if err != nil {
		return 0, err
	}
	defer rows.Close()
	var ids []string
	var recs []*kgo.Record
	for rows.Next() {
		var id, topic, key, tp string
		var payload []byte
		if err := rows.Scan(&id, &topic, &key, &payload, &tp); err != nil {
			return 0, err
		}
		rec := &kgo.Record{Topic: topic, Key: []byte(key), Value: payload,
			Headers: []kgo.RecordHeader{{Key: "content-type", Value: []byte("application/cloudevents+json")}}}
		if tp != "" { // context captured at write time, not relay time (M7)
			rec.Headers = append(rec.Headers, kgo.RecordHeader{Key: "traceparent", Value: []byte(tp)})
		}
		ids, recs = append(ids, id), append(recs, rec)
	}
	if err := rows.Err(); err != nil || len(recs) == 0 {
		return 0, err
	}
	if err := r.kafka.ProduceSync(ctx, recs...).FirstErr(); err != nil {
		return 0, err // rollback: rows stay unpublished and are retried (at-least-once)
	}
	if _, err := tx.Exec(ctx, `UPDATE outbox SET published_at = now() WHERE id = ANY($1::text[]::uuid[])`, ids); err != nil {
		return 0, err
	}
	return len(ids), tx.Commit(ctx)
}
```

franz-go's producer is idempotent with `acks=all` by default. A crash between produce and commit re-publishes the batch (inboxes absorb it); a nightly job deletes published rows older than 3 days.

*Done when* killing the orders pod mid-load leaves the unpublished count draining to 0 within 5 s of restart (expected: `0`):

```bash
kubectl -n freightline exec orders-db-0 -- psql -U orders -d orders -tAc "SELECT count(*) FROM outbox WHERE published_at IS NULL"
```

**M4 — `inventory` consumer with an inbox and all-or-nothing reservations (6 h).**

```python
# services/inventory/src/inventory/consumer.py
import json

from aiokafka import AIOKafkaConsumer
from opentelemetry import propagate, trace
from psycopg.types.json import Jsonb

from inventory.events import build_event  # CloudEvents envelope helper

tracer = trace.get_tracer("inventory.consumer")


class OutOfStock(Exception): ...


async def run(pool, bootstrap: str) -> None:
    consumer = AIOKafkaConsumer(
        "orders.order-placed.v1", "orders.order-cancelled.v1",
        bootstrap_servers=bootstrap, group_id="inventory",
        enable_auto_commit=False, auto_offset_reset="earliest",
    )
    await consumer.start()
    try:
        async for msg in consumer:
            carrier = {k: v.decode() for k, v in (msg.headers or [])}
            with tracer.start_as_current_span(
                f"process {msg.topic}", context=propagate.extract(carrier), kind=trace.SpanKind.CONSUMER
            ):
                await handle(pool, json.loads(msg.value))
            await consumer.commit()  # only after the DB transaction committed
    finally:
        await consumer.stop()


async def handle(pool, event: dict) -> None:
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("INSERT INTO inbox (event_id) VALUES (%s) ON CONFLICT DO NOTHING", (event["id"],))
        if cur.rowcount == 0:
            return  # duplicate delivery, already handled
        order = event["data"]
        ok = await reserve_all(conn, order["order_id"], order["lines"])
        topic = "inventory.stock-reserved.v1" if ok else "inventory.stock-rejected.v1"
        headers: dict[str, str] = {}
        propagate.inject(headers)
        await conn.execute(
            "INSERT INTO outbox (topic, msg_key, payload, traceparent) VALUES (%s, %s, %s, %s)",
            (topic, order["order_id"], Jsonb(build_event(topic, order)), headers.get("traceparent")),
        )


async def reserve_all(conn, order_id: str, lines: list[dict]) -> bool:
    try:
        async with conn.transaction():  # SAVEPOINT: all lines or none
            for line in lines:
                cur = await conn.execute(
                    """UPDATE stock SET reserved = reserved + %(q)s
                       WHERE sku = %(sku)s AND on_hand - reserved >= %(q)s""",
                    {"q": line["quantity"], "sku": line["sku"]},
                )
                if cur.rowcount != 1:
                    raise OutOfStock(line["sku"])
                await conn.execute(
                    "INSERT INTO reservations (order_id, sku, quantity) VALUES (%s, %s, %s)",
                    (order_id, line["sku"], line["quantity"]),
                )
        return True
    except OutOfStock:
        return False
```

The conditional `UPDATE ... WHERE on_hand - reserved >= q` is the oversell guard: an atomic single-row check, no `SERIALIZABLE` needed. On the fifth failure of a `(partition, offset)`, produce the raw message to `dlq.inventory.v1` with `x-error` and `x-original-*` headers, then commit. Serve the pre-check from `grpc.aio.server(options=[("grpc.max_connection_age_ms", 30000), ("grpc.max_connection_age_grace_ms", 5000)])` so HTTP/2 clients reconnect and spread across pods.

*Done when* replaying the same `order-placed` event 10 times yields one reservation, and an order for 3 units of a SKU with 2 on hand yields `stock-rejected` with no partial reservation. Both cases are integration tests against real Postgres and Kafka (expected: `2 passed`):

```bash
uv run --directory services/inventory pytest tests/integration -q -k "duplicate_replay or partial_reservation"
```

**M5 — `billing` and `notifications` in TypeScript (7 h).** Both run as compiled ESM on Node 24. `tsconfig.json` uses `"module": "nodenext"`, `"target": "es2025"`, `"strict": true`, `"verbatimModuleSyntax": true` and `"erasableSyntaxOnly": true`. `baseUrl` and `moduleResolution: node` are errors in TS 7.

```ts
// services/billing/src/main.ts
import http2 from "node:http2";
import Fastify from "fastify";
import pg from "pg";
import kafka from "@confluentinc/kafka-javascript";
import { Code, ConnectError } from "@connectrpc/connect";
import { connectNodeAdapter } from "@connectrpc/connect-node";
import { BillingService } from "./gen/freightline/billing/v1/billing_pb.js";
import { handleOrderConfirmed, loadInvoice } from "./invoices.js";

const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 10 });
const app = Fastify({ logger: true });
let ready = false;

app.get("/healthz", async () => ({ status: "ok" }));
app.get("/readyz", async (_req, reply) => {
  if (!ready) return reply.code(503).send({ status: "draining" });
  await pool.query("SELECT 1");
  return { status: "ready" };
});
// GET /v1/invoices/:id is registered the same way, returning Problem Details on 404

const rpc = http2.createServer(
  connectNodeAdapter({
    routes: (router) =>
      router.service(BillingService, {
        async getInvoice(req) {
          const invoice = await loadInvoice(pool, req.invoiceId);
          if (!invoice) throw new ConnectError("invoice not found", Code.NotFound);
          return invoice;
        },
      }),
  }),
);

const broker = process.env.KAFKA_BOOTSTRAP ?? "kafka.freightline-data:9092";
const consumer = new kafka.KafkaJS.Kafka({ kafkaJS: { brokers: [broker] } }).consumer({
  kafkaJS: { groupId: "billing", fromBeginning: true },
});

await app.listen({ host: "0.0.0.0", port: 8080 });
rpc.listen(50051);
await consumer.connect();
await consumer.subscribe({ topics: ["orders.order-confirmed.v1"] });
await consumer.run({ eachMessage: async ({ message }) => handleOrderConfirmed(pool, message) });
ready = true;

for (const signal of ["SIGTERM", "SIGINT"] as const) {
  process.once(signal, async () => {
    ready = false;
    await consumer.disconnect(); // leave the group after in-flight eachMessage completes
    await app.close(); // stop accepting, drain in-flight HTTP
    rpc.close(); // GOAWAY to HTTP/2 clients
    await pool.end();
    process.exit(0);
  });
}
```

`handleOrderConfirmed` runs the same inbox, write and outbox transaction as M4, with `UNIQUE (order_id)` on `invoices` as a second duplicate guard. Notifications has the same shape; it calls `GetInvoice` with a 500 ms deadline and, if billing is slow, sends the email without the invoice link and records an enrichment retry.

*Done when* one order produces exactly one invoice row, one Mailpit email and one signed webhook, and the email lists the invoice number.

**M6 — Timeouts, retries and bulkheads on every edge (4 h).** Write this table into `docs/adr/` and enforce it in code and gateway policy:

| Edge | Timeout | Retries | Bulkhead | Fallback |
|---|---|---|---|---|
| Gateway → `GET` routes | 1 s | 2 on connect-failure/reset, 50-500 ms backoff | Envoy circuit breaker defaults | `503` |
| Gateway → `POST /v1/orders` | 2 s | 0 at the gateway (the client retries with the same `Idempotency-Key`) | same | `503` with `Retry-After: 1` |
| orders → inventory pre-check | 300 ms | 0 | 32 in-flight | Accept the order; the saga decides |
| notifications → billing `GetInvoice` | 500 ms | 2, full jitter, retry budget 10% | 16 in-flight | Email without invoice link, enrich later |
| Service → Postgres | `statement_timeout=2s`, pool acquire 500 ms | 0 | Pool of 10 | `503` |
| Relay → Kafka | 30 s delivery timeout | Client-internal, idempotent | One relay | Rows stay in the outbox |
| Consumer handler | 10 s | 5 attempts with backoff | One message per partition at a time | DLQ |

In Go the bulkhead is a buffered channel of 32: a non-blocking `select` send either takes a slot or increments `precheck_skipped` and accepts the order; the call runs under `context.WithTimeout(ctx, 300*time.Millisecond)`, and errors count as `precheck_degraded`. The client is `inventoryv1connect.NewInventoryServiceClient(&http.Client{Transport: &http.Transport{Protocols: h2cOnly}}, "http://inventory:50051", connect.WithGRPC())`, where `h2cOnly` is a `*http.Protocols` with only `SetUnencryptedHTTP2(true)`.

*Done when* scaling inventory to zero keeps `POST /v1/orders` p99 under 400 ms and `precheck_degraded` climbing, with no 5xx.

**M7 — OpenTelemetry end to end (4 h).** Every service gets the same environment, set by the chart:

| Variable | Value |
|---|---|
| `OTEL_SERVICE_NAME` | `orders`, `inventory`, `billing` or `notifications` |
| `OTEL_RESOURCE_ATTRIBUTES` | `service.namespace=freightline,deployment.environment.name=kind,service.version=<image tag>` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-gateway.observability:4317` (until P04 exists, a `grafana/otel-lgtm` pod) |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | `http,database`, so Python and Node emit the same stable metric names as Go |
| `OTEL_TRACES_SAMPLER` / `_ARG` | `parentbased_traceidratio` / `1.0` in the lab, `0.1` at 100 req/s sustained |

Go uses the SDK with `otelhttp`, `otelconnect` and `otelpgx`; Python uses `opentelemetry-instrument`; Node starts with `node --import @opentelemetry/auto-instrumentations-node/register dist/main.js`. The subtle part is Kafka: the relay publishes later, in another goroutine, so capture `traceparent` when you insert the outbox row (M3) and extract it from record headers in consumers (M4); in Go, implement `propagation.TextMapCarrier` over `kgo.Record.Headers` or use franz-go's `plugin/kotel`. Log JSON to stdout with `trace_id` and `span_id` on every line.

*Done when* one trace shows `POST /v1/orders` → outbox relay publish → inventory consume → orders consume → billing consume → notifications `GetInvoice`, with no broken parent links.

**M8 — Helm library chart and umbrella chart on kind (8 h).** One library chart renders every service identically; P03 depends on these lifecycle settings.

```yaml
# deploy/helm/freightline-service/templates/_deployment.tpl (excerpt)
{{- define "freightline-service.podSpec" -}}
terminationGracePeriodSeconds: 40
securityContext:
  runAsNonRoot: true
  seccompProfile: { type: RuntimeDefault }
containers:
  - name: app
    image: "{{ .Values.image.repository }}{{ if .Values.image.digest }}@{{ .Values.image.digest }}{{ else }}:{{ .Values.image.tag }}{{ end }}"
    ports:
      - { name: http, containerPort: 8080 }
      - { name: rpc, containerPort: 50051 }
    envFrom:
      - secretRef: { name: {{ .Values.name }}-db }
    startupProbe:
      httpGet: { path: /healthz, port: http }
      periodSeconds: 2
      failureThreshold: 30
    readinessProbe:
      httpGet: { path: /readyz, port: http }
      periodSeconds: 2
      failureThreshold: 2
    livenessProbe:
      httpGet: { path: /healthz, port: http }
      periodSeconds: 10
      failureThreshold: 3
    lifecycle:
      preStop:
        sleep: { seconds: 10 }   # native sleep action: works in distroless images with no /bin/sleep
    resources:
      requests: { cpu: {{ .Values.resources.cpu }}, memory: {{ .Values.resources.memory }} }
      limits: { memory: {{ .Values.resources.memory }} }
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: { drop: [ALL] }
    volumeMounts:
      - { name: tmp, mountPath: /tmp }
volumes:
  - name: tmp
    emptyDir: {}
{{- end }}
```

Liveness checks only the process, never the database, or a Postgres blip restarts every pod. The umbrella chart also renders a PDB (`maxUnavailable: 1`) per service, a default-deny NetworkPolicy with explicit allows, `app.kubernetes.io/name` and `app.kubernetes.io/part-of: freightline` labels, and a post-install Job running `kafka-topics.sh --create --if-not-exists` for every contract topic, with broker `auto.create.topics.enable=false` so a typo never creates a 1-partition topic. Skip Bitnami's Kafka and PostgreSQL charts (the free images moved to unmaintained `bitnamilegacy`): render StatefulSets on digest-pinned `apache/kafka:4.3.1` and `postgres:18.6`, or use CloudNativePG if the customer runs it.

*Done when* `helm lint` passes and the rendered output passes a server-side dry run once the `freightline` and `freightline-data` namespaces exist (expected: every object ends in `(server dry run)`):

```bash
helm template freightline deploy/helm/freightline -n freightline -f deploy/envs/kind/values.yaml | kubectl apply --dry-run=server -f -
```

**M9 — End-to-end and failure-injection proof (4 h).** Place 1,000 orders with k6 while `tests/chaos/kill-loop.sh` deletes one random pod labeled `app.kubernetes.io/part-of=freightline` every 30 s for 10 minutes (`kubectl delete --wait=false`), then reconcile: the first query must return 1,000 and the second zero rows:

```sql
-- run against billing-db
SELECT count(*) FROM invoices;
SELECT order_id, count(*) FROM invoices GROUP BY order_id HAVING count(*) > 1;
```

*Done when* all five success criteria in section 1 are met, with evidence saved in `docs/evidence/p02/`.

### 6. Deployment instructions

Save the cluster config. The node is pinned to Kubernetes 1.36.4 by digest because Envoy Gateway 1.9 is tested on 1.33-1.36:

```yaml
# deploy/kind/kind-config.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: freightline
nodes:
  - role: control-plane
    image: kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed
```

Create the cluster:

```bash
kind create cluster --config deploy/kind/kind-config.yaml
```

Install Envoy Gateway, which also installs the Gateway API CRDs:

```bash
helm install eg oci://docker.io/envoyproxy/gateway-helm --version v1.9.1 -n envoy-gateway-system --create-namespace
```

```bash
kubectl wait --timeout=5m -n envoy-gateway-system deployment/envoy-gateway --for=condition=Available
```

Build the four images (`freightline-orders:dev` and the rest) and load them into the node:

```bash
make images TAG=dev
```

```bash
kind load docker-image freightline-orders:dev freightline-inventory:dev freightline-billing:dev freightline-notifications:dev --name freightline
```

Install the umbrella chart (namespaces, Postgres, Kafka, the topics Job, services, Gateway and HTTPRoutes):

```bash
helm dependency build deploy/helm/freightline
```

```bash
helm install freightline deploy/helm/freightline -n freightline --create-namespace -f deploy/envs/kind/values.yaml --wait --timeout 10m
```

Verify every pod is ready and the Gateway is `Programmed`:

```bash
kubectl get pods -A -l app.kubernetes.io/part-of=freightline
```

```bash
kubectl get gateway,httproute -n freightline
```

Reach the gateway from your laptop:

```bash
export ENVOY_SERVICE=$(kubectl get svc -n envoy-gateway-system --selector=gateway.envoyproxy.io/owning-gateway-namespace=freightline,gateway.envoyproxy.io/owning-gateway-name=freightline -o jsonpath='{.items[0].metadata.name}')
```

```bash
kubectl -n envoy-gateway-system port-forward service/${ENVOY_SERVICE} 8088:80
```

In a second terminal, place an order; expect `202` and a `Location` to poll until `status` is `INVOICED`, usually within 2 s:

```bash
curl -si -X POST localhost:8088/v1/orders -H "Content-Type: application/json" -H "Idempotency-Key: first-order-000001" -d '{"customer_id":"C-100","lines":[{"sku":"SKU-7","quantity":2}]}'
```

**Rollback** one Helm revision, then confirm with `helm history`:

```bash
helm rollback freightline -n freightline
```

**Teardown.**

```bash
kind delete cluster --name freightline
```

### 7. Testing & validation

| Layer | What | Tool | Pass threshold |
|---|---|---|---|
| Unit | State machine, reservation SQL, invoice numbering, carrier | `go test -race`, pytest 9, `node --test` | 100% pass; race detector clean |
| Contract | Protos, REST vs OpenAPI, events vs JSON Schema | `buf breaking`, Schemathesis, schema tests | Zero breaking changes or failures |
| Integration | Each service with real Postgres and Kafka | Compose profile `it` | Inbox dedupe proved by duplicate injection |
| End to end | Order → invoice → email on kind | pytest e2e | p95 order-to-invoice < 5 s over 200 orders |
| Load | 100 orders/s for 30 min | k6 v2.3 | `POST` p99 < 300 ms; `http_req_failed` < 0.1% |
| Chaos | Kill loop; Kafka down 60 s; inventory at 0 | kubectl | No lost or duplicate invoices; reads never 5xx; outbox drains < 30 s |
| Security | Dependency and image scans; non-root, read-only | `govulncheck`, `pip-audit`, `pnpm audit`, Grype | No known exploited vulnerabilities; PSA `restricted` dry-run passes |

### 8. Observability & operations

Beyond the automatic RED metrics, add:

| Signal | Source | Alert |
|---|---|---|
| Outbox backlog age (`now() - min(created_at)` for unpublished rows) | Each relay, as a gauge | > 30 s for 5 min → page: events are not flowing |
| Consumer lag per group | Kafka exporter or client metrics | > 5,000 messages or rising for 10 min → ticket |
| DLQ depth | Kafka exporter | Any message in `dlq.*` → ticket with the `x-error` header |
| Pre-check degraded rate | `orders` counter | > 20% for 10 min → ticket: inventory is slow |
| Saga stuck | `orders` query: `PENDING` older than 60 s | > 0 → page |

Runbook: **Orders stuck in `PENDING`.** (1) A growing outbox backlog in orders-db means the relay or Kafka. (2) Otherwise check `inventory` consumer lag. (3) If lag is zero, find the order in `dlq.inventory.v1`. (4) Never hand-edit status: fix the cause and replay the DLQ message so every service sees the same events.

### 9. Security & compliance

- **Threat model highlights.** A compromised pod reaching every database (per-service NetworkPolicies and credentials); forged events (per-principal Kafka ACLs in production); oversell under concurrency (the conditional `UPDATE`); poison messages stalling a partition (the DLQ); dependency compromise (lockfiles, pnpm 11's release-age delay, `allowBuilds`, digest-pinned bases; P22 goes deeper).
- **Controls.** PSA `restricted`, non-root, read-only root filesystem, dropped capabilities, `RuntimeDefault` seccomp, DB credentials as Secrets, and no mounted service account tokens.
- **Compliance mapping.** None for Meridian; the same controls later map to PCI DSS v4.0.1 segmentation (P25) and HIPAA technical safeguards (P26): keep evidence in `docs/evidence/`.

### 10. Extensions for advanced learners

1. **T3 — Kafka share groups for notifications** (production-ready since Kafka 4.2). *Hard because* you lose per-key ordering and must prove that is fine for emails but not for status events.
2. **T3 — Schema registry and `BACKWARD` compatibility gates.** *Hard because* it touches three languages' serializers and every consumer at once.
3. **T3 — Service mesh mTLS** with Istio ambient (GA since 1.24) or Linkerd. *Hard because* HTTP/2 balancing, NetworkPolicies and the gateway all change behavior.
4. **T4 — Saga compensation with timeouts:** cancel and release stock if billing is silent for 10 minutes. *Hard because* timers must survive restarts and race correctly against late events.
5. **T4 — Tenant-aware Freightline** (`tenant_id` everywhere plus Postgres row-level security), the base for P27. *Hard because* every query, topic key, cache key and metric label must carry the tenant with bounded cardinality.

### 11. How to demonstrate it in interviews

**2-minute pitch.** "Freightline is a four-service order-to-cash system for a composite 3PL: orders in Go, inventory in Python, billing and notifications in TypeScript, mirroring how customer teams split. Each service owns its Postgres. State changes and events commit through a transactional outbox, one relay per service keeps per-order ordering, and every consumer dedupes through an inbox. Internally it's Protobuf over Connect and gRPC; externally, REST through Envoy Gateway, since ingress-nginx is retired. Every edge has an explicit timeout, retry and bulkhead. The proof: 1,000 orders while a pod died every 30 seconds, with zero lost or duplicate invoices and p95 order-to-invoice under 5 seconds."

**10-minute demo flow.**
1. Diagram and reference contract table (1.5 min).
2. Place an order and follow its single trace across four services (2 min).
3. The outbox and inbox rows for that order (1 min).
4. Scale inventory to 0: orders still accept, degraded pre-check counter rising (1.5 min).
5. Kill loop under k6, then reconciliation SQL: 1,000 invoices, zero duplicates (2.5 min).
6. A poison message in the DLQ with its headers (1 min), then trade-offs (30 s).

**Likely questions and strong-answer outlines.**
1. *"Why not Kafka transactions?"* They give Kafka-to-Kafka exactly-once, not a database write plus a publish; the outbox makes Postgres the source of truth.
2. *"How do you keep per-order ordering?"* `order_id` keys, one relay per service, sequential per-partition consumption; explain what share groups would break.
3. *"Why three languages?"* Customer team ownership, contained by one library chart, one OTel convention and one contract repo.
4. *"Kafka is down. Now what?"* Writes still commit and the outbox grows; show the backlog-age alert and the drain rate.
5. *"How does gRPC balance behind a ClusterIP?"* It doesn't: HTTP/2 pins a connection. Use `max_connection_age`, client-side balancing over the headless Service, or a mesh.

**Artifacts to bring:** diagram, contract table, ADRs, the one-trace screenshot, the kill-loop k6 summary with reconciliation output, the resilience-policy table.

**Metrics to quote:** p95 order-to-invoice, `POST` p99 at 100 req/s, lost/duplicate counts under chaos (0/0), outbox drain time after a Kafka outage, memory per pod per language.

**What you would do differently:** start with Protobuf events and a registry (retrofitting schemas across three languages is the costliest change); build the reconciliation job on day one.

### 12. Common failure points while building

| Failure | Symptom | Fix |
|---|---|---|
| gRPC connections pinned to one pod | New inventory pods idle while old ones run hot | Server `grpc.max_connection_age_ms`, or client-side balancing over `inventory-headless` |
| Broken traces across Kafka | Consumer spans start new traces | Store `traceparent` in the outbox row; extract it from headers in consumers |
| Mismatched metric names across languages | Dashboards show Go only; Python emits `http.server.duration` in ms | `OTEL_SEMCONV_STABILITY_OPT_IN=http` for Python and Node |
| Native Kafka client blocked | `pnpm install` succeeds; runtime fails with a missing binding | pnpm 11 blocks install scripts: add `@confluentinc/kafka-javascript` to `allowBuilds` |
| Consumer commits before the DB commit | A crash silently loses events | Commit offsets only after the transaction (`enable_auto_commit=False`) |
| Two languages producing to one keyed topic | One order's events land on different partitions | Native librdkafka clients (confluent-kafka-python, confluent-kafka-go) default to CRC32 `consistent_random`; franz-go, aiokafka and the JS client's KafkaJS API use Java-compatible murmur2. Set `partitioner=murmur2_random` explicitly, or keep one producer per topic |

**See also:** [Distributed systems primitives](../../05-theory/05-distributed-systems-primitives.md) and [Production reliability patterns](../../05-theory/06-production-reliability-patterns.md) (outbox, idempotency and bulkhead theory); [M08 Containers and Kubernetes](../../01-curriculum/M08-containers-and-kubernetes.md).

---

## P03 — Zero-Downtime Delivery Pipeline

| Field | Value |
|---|---|
| ID | P03 |
| Tier | **T2 Intermediate** → **T3 Advanced** |
| Time estimate | 30-40 hours |
| Industry framing | Northstar Retail (fictional): 1,200 stores place replenishment orders through Freightline, and Black Friday traffic peaks at several times normal load |
| Required categories covered | Zero-downtime deployments; CI/CD |
| Languages | YAML (Actions, Kubernetes), Go (flag wiring), SQL (migrations), JavaScript (k6), Bash |
| Cloud(s) | GitHub (Actions, GHCR) plus kind. The same manifests run on EKS, AKS or GKE in P06 and P08 |
| Estimated cost / keeping it near $0 | $0: public repositories get free hosted-runner minutes and free public GHCR packages. A 3-node kind cluster with Argo CD, Rollouts, Prometheus and Freightline needs about 14 GB of RAM; on 16 GB run `replicas: 2` per service |
| Prerequisites | [C09](../02-curriculum/C09-containers-and-kubernetes.md) T2, [C10](../02-curriculum/C10-devops-and-cicd.md) T2, [C12](../02-curriculum/C12-observability-and-monitoring.md) T1, [C15](../02-curriculum/C15-production-readiness-and-incident-response.md) T1; P02; P04 M2 (Prometheus with the OTLP receiver). Recommended build order: P01 → P02 → P04 → P03 |

### 1. Problem statement

**Business context (composite scenario).** Northstar Retail's store systems send replenishment orders to Freightline, which Beacon runs in Northstar's cluster. Last November a routine deploy in trading hours produced about three minutes of `502`s: pods were killed before the load balancer stopped routing to them. Store edge nodes replayed the failed orders and a store-side bug created duplicates. Northstar's change advisory board (CAB) now bans deploys from 2026-11-16 to 2026-12-01 unless Beacon can *prove* a deploy and a rollback cause zero failed requests.

**Constraints.**
- **Retries.** Store clients retry aggressively, so any failure becomes a burst.
- **Credentials.** CI may hold no long-lived cloud or registry credentials, and every third-party Action is pinned to a commit SHA, a rule adopted after the 2025 tj-actions compromise ([source](#sources)).
- **Audit.** Every production change traces to a reviewed Git commit, which is the change record.
- **Database.** Schema changes never need a maintenance window.

**Stakeholders.** Northstar's VP Store Operations (sponsor), the CAB chair (needs evidence), Northstar platform security (owns the CI policy), Freightline service owners, and Beacon's FDE (you).

**Measurable success criteria.**
1. **0 failed requests** over a k6 run at 200 req/s while a healthy release of `orders` goes from 0% to 100%.
2. **0 failed requests** while a healthy release is aborted and rolled back mid-canary.
3. A deliberately bad release (5% `500`s) is aborted automatically within 90 s of reaching 10% weight. Failed requests stay under 0.1% of the run.
4. An expand migration and backfill run under load with 0 failed requests and no lock wait over 2 s.
5. A feature-flag kill switch disables the new code path on every pod within 2 minutes, with no deploy.

### 2. Requirements

**Functional requirements**
- **FR-1** Every PR runs tests, lint, `buf breaking` and a workflow-security lint; merges to `main` build, sign and attest images.
- **FR-2** CI uses OIDC only; keyless cosign signing and provenance attestations use the workflow's OIDC token.
- **FR-3** Promotion is a PR changing digests in `deploy/envs/<env>/values-images.yaml`; the merge is the change record.
- **FR-4** Argo CD syncs from Git with self-heal; drift is visible and reverted.
- **FR-5** Argo Rollouts shifts 10%, 25%, 50% through the Gateway API, then promotes; background Prometheus analysis aborts automatically.
- **FR-6** Migrations follow expand/contract and are compatible with releases N-1 and N, because the canary runs both.
- **FR-7** New behavior ships dark behind an OpenFeature flag evaluated by flagd.
- **FR-8** Every workload has a PDB, a `preStop` sleep, drain-aware readiness and a grace period longer than its drain.
- **FR-9** A k6 test runs during every rehearsal and gates the result.

**Non-functional requirements**

| Attribute | Target |
|---|---|
| Failed requests during a healthy deploy or rollback | 0 at 200 req/s |
| Detection-to-abort for a bad canary | < 90 s (2 analysis intervals plus propagation) |
| Rollback traffic shift | < 10 s once aborted (a weight change, not a redeploy) |
| Lead time, merge to 100% in the kind environment | < 20 min |
| Migration lock waits | `lock_timeout = 2s` on every DDL statement |
| Pipeline cost | $0 (public repository) |

**Constraints.** No `pull_request_target` workflows. The default `GITHUB_TOKEN` is read-only. No `latest` tags anywhere: images are referenced by digest.

**Out of scope.** Multi-cluster promotion across regions (P08), admission-time signature enforcement (P22), and progressive delivery of configuration (an extension).

### 3. Architecture

```text
 DEVELOPER            GITHUB (OIDC issuer: token.actions.githubusercontent.com)             REGISTRY
 +--------+  PR    +---------------------------------------------------------------+     +-----------+
 | git    |------->| ci.yaml (pinned SHAs, perms read-only by default)             |     | GHCR      |
 | push   |        |  test ─> build ─> push by digest ─> cosign sign (keyless) ────+────>| images +  |
 +--------+        |          └─> attest-build-provenance (Sigstore)               |     | sigs +    |
                   |  promote job: PR "orders@sha256:..." into deploy/envs/kind    |     | attests   |
                   +------------------------------+--------------------------------+     +-----^-----+
                                                  | reviewed merge = change record             | pull by
 ================ TRUST BOUNDARY: cluster pulls from Git and registry; nothing pushes in ======| digest
                                                  v                                            |
 +--------------------------------------------------------------------------------------------+-----+
 | KIND CLUSTER (1 control plane + 2 workers, Kubernetes 1.36)                                      |
 |  argocd ns: Argo CD 3.5 ──sync──> freightline ns (Rollouts, Services, HTTPRoutes, PDBs)          |
 |  argo-rollouts ns: controller + Gateway API plugin ──patches weights──> HTTPRoute "orders"       |
 |                                                                                                  |
 |  k6 (200 req/s) ──> Envoy Gateway ──weight 90──> orders-stable (RS hash a1b2)  ┐                 |
 |                                   └─weight 10──> orders-canary (RS hash c3d4)  ├─ OTLP ─> otel   |
 |                                                                                ┘   gateway      |
 |  AnalysisRun (every 30 s) ──PromQL by pod-template-hash──> Prometheus <── OTLP receiver ─┘       |
 |       fail ⇒ abort: weights back to 100/0, canary scaled down after 30 s                         |
 |  flagd (flags from Git via ConfigMap) <──in-process sync :8015── orders pods                     |
 |  PreSync hook Job: dbmate migrate (expand only) ──> orders-db                                    |
 +--------------------------------------------------------------------------------------------------+
```

| Component | Responsibility | Technology | Why | Alternative considered |
|---|---|---|---|---|
| CI | Test, build, sign, attest, promotion PR | GitHub Actions, SHA-pinned | Customer-mandated; OIDC built in | GitLab CI 19.4 |
| Signing | Prove the image's origin | cosign keyless + build provenance | No keys to manage | KMS-held key (air-gapped, P16) |
| GitOps | Desired state, drift correction | Argo CD | UI for the CAB, hooks, RBAC | Flux 2.9 |
| Progressive delivery | Weighted canary, analysis, abort | Argo Rollouts + Gateway API plugin | Exact HTTPRoute weights | Flagger 1.45 |
| Traffic | Weighted routing, draining | Envoy Gateway | Implements the plugin's weights | Replica-count canary |
| Analysis source | SLIs per ReplicaSet | Prometheus via OTLP | The canary's own error rate | Datadog or New Relic providers |
| Flags | Separate deploy from release | OpenFeature + flagd in-process | Vendor-neutral; runs air-gapped | LaunchDarkly or Unleash |
| Migrations | Expand/contract DDL | dbmate in a `PreSync` hook | Plain SQL for three languages | App-start migrations (replicas race) |

**Mini-ADRs**

| # | Decision | Options | Choice | Consequences |
|---|---|---|---|---|
| ADR-P03-1 | How canary traffic is split | Replica ratio; mesh; Gateway API plugin | Plugin writing HTTPRoute weights | Exact at any replica count; Argo CD must ignore the weights (M2) or self-heal fights the canary |
| ADR-P03-2 | What the analysis measures | Service-wide or canary-only error rate | Canary-only, by pod-template hash | A 5% regression at 10% weight shows as 5%, not 0.5% |
| ADR-P03-3 | How promotion is recorded | Image Updater; CI pushes; PR with digests | PR with digests, merged by a human | The merge is the CAB evidence; no cluster credentials in CI |
| ADR-P03-4 | Where migrations run | App start; CI job; `PreSync` hook | `PreSync` hook, expand-only | Runs once per sync, before the new ReplicaSet; contracts ship a release later |

### 4. Tools & technologies

| Tool | Version / status (as of September 2026) | Notes |
|---|---|---|
| GitHub Actions | Node 24 runtime (Node 20 removed 2026-09-23) | `pull_request_target` disabled by default in public repos from 2026-11-02 |
| Pinned actions | Ten actions on current majors (checkout v7, build-push v7, attest-build-provenance v4) | Full SHAs with version comments in M1; re-resolve them when you bump |
| cosign | 3.1.x (bundle format, Rekor v2) | v2.6.x gets backports only |
| Argo CD | 3.5.3 | 3.0 changed defaults: annotation tracking; `update`/`delete` RBAC no longer covers sub-resources |
| Argo Rollouts + Gateway API plugin | 1.10.0 (2026-08-27) + 0.17.0 | 1.9.1 fixed CVE-2026-35469; pick the plugin binary for your node architecture |
| OpenFeature / flagd | CNCF incubating / flagd 0.16.3 (0.17.0 landed 2026-09-25: re-test before bumping) | In-process provider syncs on port 8015 |
| dbmate | 2.36.0 | `-- migrate:up transaction:false` for `CREATE INDEX CONCURRENTLY` |
| k6 | v2.3.0 (AGPL-3.0) | The OpenTelemetry Demo moved back to Locust over licensing |
| kind | 0.33.0, node image 1.36.4 | 3 nodes so you can drain one |

### 5. Step-by-step implementation plan

**M1 — CI with least privilege and pinned actions (5 h).** Default permissions are read-only. Only the build job gets `packages: write`, `id-token: write` and `attestations: write`.

```yaml
# .github/workflows/ci.yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false }
      - uses: actions/setup-go@b7ad1dad31e06c5925ef5d2fc7ad053ef454303e # v7.0.0
        with: { go-version-file: services/orders/go.mod }
      - uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0
      - uses: pnpm/action-setup@ea17c68df8912ef543352723c149a84f56e3d413 # v6.1.0
      - uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0
        with: { node-version: 24 }
      - run: make test lint
  build:
    needs: test
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    strategy:
      matrix: { service: [orders, inventory, billing, notifications] }
    permissions:
      contents: read
      packages: write
      id-token: write      # OIDC token for keyless signing and provenance
      attestations: write
    env:
      IMAGE: ghcr.io/${{ github.repository_owner }}/freightline-${{ matrix.service }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false }
      - uses: docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069 # v4.4.1
      - uses: docker/login-action@dbcb813823bdd20940b903addbd779551569679f # v4.6.0
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: build
        uses: docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc # v7.4.0
        with:
          context: services/${{ matrix.service }}
          push: true
          tags: ${{ env.IMAGE }}:${{ github.sha }}
      - uses: sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6 # v4.1.2
      - run: cosign sign --yes "${IMAGE}@${DIGEST}"
        env: { DIGEST: "${{ steps.build.outputs.digest }}" }
      - uses: actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2
        with:
          subject-name: ${{ env.IMAGE }}
          subject-digest: ${{ steps.build.outputs.digest }}
          push-to-registry: true
```

The step-level `env` passes the digest to the shell instead of interpolating `${{ }}` into the script (GitHub's injection guidance). A separate `promote` job (`contents: write`, `pull-requests: write`) resolves digests with `docker buildx imagetools inspect`, writes them into `deploy/envs/kind/values-images.yaml` with `yq`, and runs `gh pr create`.

*Done when* a merge produces four signed images, and verification succeeds (set `GH_USER` and `GIT_SHA` first):

```bash
gh attestation verify "oci://ghcr.io/${GH_USER}/freightline-orders:${GIT_SHA}" -R "${GH_USER}/freightline"
```

**M2 — Argo CD with the right ignore rules (3 h).** The Rollouts plugin edits HTTPRoute weights during a canary. Unless Argo CD both ignores and *respects* that difference, self-heal puts the weights back every few seconds.

```yaml
# deploy/argocd/freightline-kind.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: freightline-kind
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/<your-user>/freightline.git
    targetRevision: main
    path: deploy/helm/freightline
    helm:
      valueFiles: [../../envs/kind/values.yaml, ../../envs/kind/values-images.yaml]
  destination: { server: https://kubernetes.default.svc, namespace: freightline }
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true, ServerSideApply=true, RespectIgnoreDifferences=true]
  ignoreDifferences:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      jqPathExpressions: [".spec.rules[].backendRefs[].weight"]
```

*Done when* `argocd app get freightline-kind` shows `Synced` and `Healthy`, and a manual `kubectl scale` is reverted within one reconcile.

**M3 — Rollouts, the plugin and canary analysis (6 h).** With `progressiveDelivery.enabled=true` the library chart renders a `Rollout` instead of a `Deployment`, plus `orders-stable` and `orders-canary` Services and an HTTPRoute listing both. The downward-API field `metadata.labels['rollouts-pod-template-hash']` feeds `OTEL_RESOURCE_ATTRIBUTES=...,freightline.pod_template_hash=$(POD_TEMPLATE_HASH)`, and Prometheus promotes that attribute to a label (P04 M2).

```yaml
# rendered Rollout strategy for orders (excerpt)
strategy:
  canary:
    stableService: orders-stable
    canaryService: orders-canary
    abortScaleDownDelaySeconds: 30
    trafficRouting:
      plugins:
        argoproj-labs/gatewayAPI:
          httpRoute: orders
          namespace: freightline
    analysis:
      templates: [{ templateName: canary-health }]
      startingStep: 1
      args:
        - { name: service, value: orders }
        - name: canary-hash
          valueFrom: { podTemplateHashValue: Latest }
    steps:
      - setWeight: 10
      - pause: { duration: 2m }
      - setWeight: 25
      - pause: { duration: 2m }
      - setWeight: 50
      - pause: { duration: 3m }
```

```yaml
# deploy/helm/freightline/files/analysis-canary-health.yaml
# Rendered with {{ .Files.Get }} so Helm never tries to evaluate Argo's {{args.*}} placeholders
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: canary-health
spec:
  args:
    - name: service
    - name: canary-hash
  metrics:
    - name: error-ratio
      interval: 30s
      failureLimit: 1
      successCondition: result[0] <= 0.005
      provider:
        prometheus:
          address: http://kps-prometheus.monitoring.svc:9090
          query: |
            (sum(rate(http_server_request_duration_seconds_count{job="freightline/{{args.service}}",freightline_pod_template_hash="{{args.canary-hash}}",http_response_status_code=~"5.."}[1m])) or vector(0))
            /
            sum(rate(http_server_request_duration_seconds_count{job="freightline/{{args.service}}",freightline_pod_template_hash="{{args.canary-hash}}"}[1m]))
```

Add a `p99-latency-seconds` metric the same way: `histogram_quantile(0.99, sum by (le) (rate(..._bucket{<same selector>}[1m])))` with `successCondition: result[0] <= 0.3`. With no canary traffic the division returns an empty vector, `result[0]` errors, and after the default consecutive-error limit the analysis fails: an unexercised canary is unproven. k6 guarantees traffic in rehearsals.

*Done when* a healthy image goes 10 → 25 → 50 → 100 on its own, and `kubectl argo rollouts get rollout orders -n freightline` ends in `Healthy`.

**M4 — Draining: PDBs, `preStop` and the termination timeline (4 h).** Zero-downtime failures almost always come from this race:

```text
t=0s    pod gets deletionTimestamp (scale-down, eviction or abort)
          ├─ EndpointSlice marks it terminating ──> Envoy Gateway pushes new endpoints (≈1-2 s)
          └─ kubelet runs preStop: sleep 10 s    (the app keeps serving stragglers)
t=10s   SIGTERM ──> /readyz returns 503, Server.Shutdown(): stop accepting, finish in-flight,
                     GOAWAY on HTTP/2, Kafka consumer leaves its group, relay finishes its batch
t≤35s   process exits 0
t=40s   terminationGracePeriodSeconds (> preStop 10 + shutdown 20): SIGKILL if still alive
```

Without the `preStop` sleep the process can exit before Envoy stops routing to it: those are the `502`s Northstar saw. The PDB covers voluntary disruptions such as node drains:

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: orders
spec:
  maxUnavailable: 1
  unhealthyPodEvictionPolicy: AlwaysAllow
  selector:
    matchLabels: { app.kubernetes.io/name: orders }
```

`AlwaysAllow` stops a crash-looping pod blocking a drain forever. Keep at least 3 replicas so the PDB and a canary coexist during a drain.

*Done when* `kubectl drain freightline-worker --ignore-daemonsets --delete-emptydir-data` under 200 req/s shows zero k6 failures.

**M5 — Expand/contract migration under load (5 h).** The feature: structured shipping addresses. `orders.ship_to` (text) becomes `ship_to_v2` (jsonb), and the N-1 release keeps working at every step.

| Step | Release | Schema | Code |
|---|---|---|---|
| 1 Expand | N+1 `PreSync` | `ADD COLUMN ship_to_v2 jsonb` (nullable, no default: metadata-only) | Writes both columns, reads `ship_to` |
| 2 Backfill | Job after N+1 is at 100% | Batches of 5,000 with `SKIP LOCKED` | Unchanged |
| 3 Switch reads | N+2 | none | Reads `ship_to_v2`, still writes both (rollback to N+1 stays safe) |
| 4 Contract | N+3 or later | `DROP COLUMN ship_to` once `pg_stat_statements` shows no reads | Writes only `ship_to_v2` |

```sql
-- services/orders/db/migrations/20260924090000_expand_ship_to_v2.sql
-- migrate:up
SET lock_timeout = '2s';
ALTER TABLE orders ADD COLUMN ship_to_v2 jsonb;

-- migrate:down
ALTER TABLE orders DROP COLUMN ship_to_v2;
```

```sql
-- services/orders/db/migrations/20260924091000_index_ship_to_postcode.sql
-- migrate:up transaction:false
CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_ship_to_postcode ON orders ((ship_to_v2->>'postcode'));

-- migrate:down transaction:false
DROP INDEX CONCURRENTLY IF EXISTS orders_ship_to_postcode;
```

`lock_timeout` matters because `ALTER TABLE` queues for an `ACCESS EXCLUSIVE` lock, and while it waits behind one long transaction every later query queues behind it: fail fast and retry. The migration Job carries `argocd.argoproj.io/hook: PreSync` and `argocd.argoproj.io/hook-delete-policy: BeforeHookCreation`, and runs `dbmate --no-dump-schema up` from `ghcr.io/amacneil/dbmate:2.36.0`.

*Done when* the expand, index and backfill all run while k6 reports zero failures, and `pg_stat_activity` never shows a `Lock` wait over 2 s.

**M6 — OpenFeature flag and kill switch (3 h).** At start-up, orders calls `flagd.NewProvider(flagd.WithInProcessResolver())` (from `github.com/open-feature/go-sdk-contrib/providers/flagd/pkg`, reading `FLAGD_HOST` and `FLAGD_PORT=8015`), then `openfeature.SetProviderAndWait(provider)`. In the handler, `useV2, _ := client.BooleanValue(ctx, "orders-structured-ship-to", false, openfeature.NewEvaluationContext(req.CustomerID, nil))`. Any evaluation error returns the default `false`: the old path is the safe path. The flag definition lives in Git:

```json
{
  "$schema": "https://flagd.dev/schema/v0/flags.json",
  "flags": {
    "orders-structured-ship-to": {
      "state": "ENABLED",
      "variants": { "on": true, "off": false },
      "defaultVariant": "off",
      "targeting": { "fractional": [["on", 10], ["off", 90]] }
    }
  }
}
```

flagd reads it from a mounted ConfigMap (`flagd start --uri file:/etc/flagd/flags.json`). The kubelet refreshes mounted ConfigMaps on its sync period, hence the 2-minute target.

*Done when* setting the fractional split to `[["off", 100]]` in Git reaches every pod within 2 minutes, confirmed by the flag-evaluation span events the OpenFeature OpenTelemetry hook adds to traces.

**M7 — The k6 proof (4 h).**

```javascript
// tests/load/deploy-proof.js
import http from "k6/http";
import { check } from "k6";

export const options = {
  scenarios: {
    steady: {
      executor: "constant-arrival-rate",
      rate: 200,
      timeUnit: "1s",
      duration: "15m",
      preAllocatedVUs: 100,
      maxVUs: 400,
    },
  },
  thresholds: {
    http_req_failed: ["rate==0"],
    "http_req_duration{name:place_order}": ["p(99)<300"],
    checks: ["rate==1"],
  },
};

const BASE = __ENV.BASE_URL || "http://localhost:8088";
if (__ENV.MAX_FAIL_RATE) {
  options.thresholds.http_req_failed = [`rate<${__ENV.MAX_FAIL_RATE}`]; // bad-release rehearsal only
}

export default function () {
  if (Math.random() < 0.8) {
    const r = http.get(`${BASE}/v1/stock/SKU-${(__ITER % 50) + 1}`, { tags: { name: "get_stock" } });
    check(r, { "stock 200": (res) => res.status === 200 });
    return;
  }
  const body = JSON.stringify({ customer_id: "C-100", lines: [{ sku: "SKU-7", quantity: 1 }] });
  const r = http.post(`${BASE}/v1/orders`, body, {
    headers: { "Content-Type": "application/json", "Idempotency-Key": `k6-${__VU}-${__ITER}-${Date.now()}` },
    tags: { name: "place_order" },
  });
  check(r, { "order 202": (res) => res.status === 202 });
}
```

Only the bad-release rehearsal runs with `-e MAX_FAIL_RATE=0.001`; every other keeps `rate==0`. The `:bad` image sets `FAULT_5XX_RATE=0.05`, which orders honors only when `FREIGHTLINE_ENV` is not `prod`.

*Done when* the three rehearsals (good rollout, abort plus undo, bad release) each have a k6 summary and a Grafana screenshot saved in `docs/evidence/p03/`.

**M8 — Rehearsal script and CAB evidence pack (3 h).** One page: pipeline diagram, k6 summaries, the aborted release's AnalysisRun (`kubectl get analysisrun -o yaml`), the commit and PR for each promotion, and the rehearsal week's DORA metrics.

*Done when* someone new to the project follows `docs/runbooks/release.md` and reproduces a rehearsal.

### 6. Deployment instructions

Create a 3-node cluster (control plane plus two workers on the pinned 1.36.4 image), then install in order: gateway, Prometheus, Argo CD, Rollouts, plugin, Freightline. Put the `kubectl-argo-rollouts` plugin from the same Rollouts release on your `PATH`.

```bash
kind create cluster --config deploy/kind/kind-config-3node.yaml
```

```bash
helm install eg oci://docker.io/envoyproxy/gateway-helm --version v1.9.1 -n envoy-gateway-system --create-namespace
```

Install Prometheus with the values file from P04 M2 (OTLP receiver, exemplars, promoted attributes):

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
```

```bash
helm install kps prometheus-community/kube-prometheus-stack --version 91.5.1 -n monitoring --create-namespace -f deploy/observability/kps-values.yaml
```

Install Argo CD (server-side apply, because some CRDs exceed the client-side annotation limit):

```bash
kubectl create namespace argocd
```

```bash
kubectl apply -n argocd --server-side --force-conflicts -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.3/manifests/install.yaml
```

Install Argo Rollouts:

```bash
kubectl create namespace argo-rollouts
```

```bash
kubectl apply -n argo-rollouts -f https://github.com/argoproj/argo-rollouts/releases/download/v1.10.0/install.yaml
```

Register the Gateway API plugin and grant it HTTPRoute access. The ConfigMap points at `.../download/v0.17.0/gatewayapi-plugin-linux-amd64`, or `-linux-arm64` on Apple Silicon:

```bash
kubectl apply -f deploy/argo-rollouts/plugin-config.yaml -f deploy/argo-rollouts/plugin-rbac.yaml
```

```bash
kubectl rollout restart deployment -n argo-rollouts argo-rollouts
```

Check that the controller logs `Download complete` for the plugin:

```bash
kubectl logs -n argo-rollouts deploy/argo-rollouts | grep -i "plugin"
```

Create the Application; from now on only Git changes the environment:

```bash
kubectl apply -f deploy/argocd/freightline-kind.yaml
```

**Rehearsal: healthy rollout.** Start k6 in one terminal, then merge the promotion PR (or run the promote job) for a new `orders` digest:

```bash
k6 run -e BASE_URL=http://localhost:8088 tests/load/deploy-proof.js
```

```bash
kubectl argo rollouts get rollout orders -n freightline --watch
```

**Rehearsal: abort and roll back.** Abort returns 100% of traffic to stable at once; then revert the promotion commit so Git and the cluster agree again:

```bash
kubectl argo rollouts abort orders -n freightline
```

```bash
git revert --no-edit HEAD
```

```bash
git push origin main
```

**Rehearsal: bad release.** Promote the `:bad` digest and watch the AnalysisRun fail and abort without you:

```bash
kubectl get analysisrun -n freightline -w
```

**Teardown.**

```bash
kind delete cluster --name freightline
```

### 7. Testing & validation

| Scenario | How | Pass threshold |
|---|---|---|
| Healthy canary | Promote a new digest during a 15-min k6 run | `http_req_failed rate==0`; the rollout reaches `Healthy` |
| Abort and undo | Abort at 25% weight, then revert in Git | `rate==0`; stable serves 100% within 10 s |
| Bad release | `:bad` image with 5% injected `500`s | Aborted < 90 s after the 10% step; `http_req_failed` < 0.1% overall |
| Node drain | Drain a worker mid-run | `rate==0`; the PDB is never violated |
| Migration under load | Expand, index and backfill during k6 | `rate==0`; no lock wait > 2 s |
| Flag kill switch | Flip in Git during load | All pods report `off` within 2 min; `rate==0` |
| Pipeline security | Workflow lint (unpinned actions, `pull_request_target`, template injection); `gh attestation verify` on every promoted digest | Zero findings; every digest verifies |

### 8. Observability & operations

- **Deploy markers.** Argo CD notifications post a Grafana annotation per sync and a PostSync hook one per promotion, so every P04 dashboard shows where each release started.
- **Alerts.** `argocd_app_info{sync_status="OutOfSync"}` for 30 min (ticket); a Rollout in `Degraded`, from the controller's metrics (page in business hours); an AnalysisRun in `Error`: the analysis itself is broken, usually a Prometheus address or query typo (ticket).
- **DORA.** Compute the five current metrics from Git and Argo CD history: deployment frequency, change lead time, change fail rate, failed deployment recovery time (formerly MTTR) and deployment rework rate.
- **Runbook: canary aborted.** Read the AnalysisRun's failed metric and value; filter the P04 dashboard to the canary hash and follow an exemplar to a failing trace; fix forward with a new digest or revert the promotion commit. Never `promote --full` past a failed analysis without the service owner's written approval.

### 9. Security & compliance

- **CI identity.** OIDC only. Repositories created after 2026-07-15 get an immutable `sub` of the form `repo:OWNER@OWNER-ID/REPO@REPO-ID:...`; trust policies written as `repo:org/name:*` will not match them.
- **Actions supply chain.** Pin full SHAs: the tj-actions (2025) and Trivy (March 2026) compromises both force-pushed tags ([source](#sources)). Set `persist-credentials: false`, pass untrusted values through `env`, and avoid `pull_request_target`.
- **Provenance and Argo CD.** Keyless signatures and provenance on every digest (P22 enforces them at admission). Since Argo CD 3.0, `update`/`delete` on an Application no longer cover its resources: grant `update/*` explicitly, via SSO groups.
- **Compliance mapping.** For SOX-style change management (Cobalt Bank, P25) the reviewed PR is the approval, the AnalysisRun the test evidence and the revert the rollback plan.

### 10. Extensions for advanced learners

1. **T3 — Canary analysis for Kafka consumers** on DLQ rate and processing latency by pod-template hash. *Hard because* consumers have no traffic weights; a canary gets whatever partitions the group assigns.
2. **T3 — Faster flag propagation** with the OpenFeature Operator. *Hard because* it adds an operator, CRDs and RBAC to a customer cluster while flags stay GitOps-managed.
3. **T4 — Progressive delivery of configuration** in health-gated rings, the idea behind Cloudflare's "Fail Small" work ([source](#sources)). *Hard because* config skips most review and test gates.
4. **T4 — Multi-cluster promotion with ApplicationSets** (kind → staging → two regions). *Hard because* a mid-promotion failure leaves regions on different versions, and the schema must tolerate that.
5. **T4 — Air-gapped variant** without GHCR or GitHub (P16, P19). *Hard because* signatures, attestations and plugin binaries must be mirrored and verified offline.

### 11. How to demonstrate it in interviews

**2-minute pitch.** "A composite retailer's deploy caused three minutes of 502s, and their CAB banned peak-season deploys unless we could prove zero downtime. CI holds no long-lived credentials: SHA-pinned actions, keyless signing through OIDC, and promotion as a pull request that changes a digest, so the merge is the change record. Argo Rollouts shifts 10, 25, then 50 percent through Gateway API weights, analyzing only the canary's pods by ReplicaSet hash. The root cause, endpoint removal racing SIGTERM, I fixed with a preStop sleep and drain-aware readiness. k6 at 200 req/s saw zero failed requests across a rollout, an abort, a node drain and an expand migration, and a bad build was aborted in under 90 seconds."

**10-minute demo flow.**
1. Diagram and the workflow's `permissions` blocks (1.5 min).
2. `gh attestation verify` on the running digest (1 min).
3. Start k6, merge a promotion, watch weights in `kubectl argo rollouts get` (2 min).
4. Promote the bad image: the AnalysisRun fails, k6 failures confined to seconds at 10% (2 min).
5. Drain a node live (1 min).
6. Termination timeline and expand/contract table (1.5 min), then the CAB evidence pack (1 min).

**Likely questions and strong-answer outlines.**
1. *"Why did the original deploy cause 502s?"* Endpoint removal and SIGTERM are concurrent; without a `preStop` delay the process exits before the proxy stops routing to it.
2. *"Why analyze only canary pods?"* Dilution: 5% errors at 10% weight is 0.5% overall, under most thresholds. Per-hash SLIs catch it in one interval.
3. *"How do you roll back a schema change?"* You don't. Migrations are backward compatible, rollback is code-only, and contract steps wait until no environment runs code that needs the old column.
4. *"What stops Argo CD fighting Rollouts?"* `ignoreDifferences` on the weights *plus* `RespectIgnoreDifferences=true`; explain what fails without the second.
5. *"How does this work at a bank with a CAB?"* Map each artifact to the CAB form (section 9), and turn the freeze window into a branch protection rule.

**Artifacts to bring:** pipeline diagram, ADRs, three k6 summaries, a Grafana screenshot of the aborted canary, the failed AnalysisRun YAML, and the evidence pack.

**Metrics to quote:** failed requests per rehearsal (0, 0, < 0.1% for the bad release); detection-to-abort time; merge-to-100% lead time; rollback shift time.

**What you would do differently:** add the per-hash telemetry label first (without it canary analysis is guessing); budget time for the Argo CD and Rollouts interaction.

### 12. Common failure points while building

| Failure | Symptom | Fix |
|---|---|---|
| Argo CD self-heal fights the canary | Weights flip back to 100/0 every few seconds; the rollout stalls | `ignoreDifferences` on `backendRefs[].weight` **and** `RespectIgnoreDifferences=true` |
| No `preStop` sleep, or a shell `sleep` in a distroless image | `502`s during scale-down; `exec: "sleep": executable file not found` | Use the native `lifecycle.preStop.sleep` action (on by default since Kubernetes 1.30) |
| Analysis compares blended metrics | A bad canary passes | Filter by `freightline_pod_template_hash` from `podTemplateHashValue: Latest` |
| AnalysisTemplate placed in Helm `templates/` | `helm template` fails with `function "args" not defined` | Keep Argo's `{{args.*}}` out of Helm's parser: load the file with `.Files.Get`, or escape the placeholders |
| Migration incompatible with N-1 | Stable pods throw `column does not exist` during the canary | Expand only in `PreSync`; contract at least one release later; a required N-1 test job |

**See also:** [M07 CI/CD and IaC](../../01-curriculum/M07-cicd-and-iac.md).

---

## P04 — Observability-in-a-Box

| Field | Value |
|---|---|
| ID | P04 |
| Tier | **T2 Intermediate** |
| Time estimate | 25-35 hours |
| Industry framing | Beacon's standard observability pack, forced by Cobalt Bank (fictional; its SOC mandates Splunk and a proxy inspects all TLS) and Northstar Retail (fictional; a Datadog shop) |
| Required categories covered | Observability dashboards |
| Languages | YAML (Collector, Helm, Sloth), PromQL, LogQL; small Go, Python and TypeScript changes |
| Cloud(s) | None; kind. Export profiles reach Splunk or Datadog without a cloud account |
| Estimated cost / keeping it near $0 | $0. Adds about 5 GB of RAM to P02 (use P02's low-RAM profile on 16 GB); 72 h retention; the in-cluster `customer-sim` Collector stands in for Splunk. Revoke any Datadog trial key the same day |
| Prerequisites | [C12](../02-curriculum/C12-observability-and-monitoring.md) T2, [C09](../02-curriculum/C09-containers-and-kubernetes.md) T1, [C15](../02-curriculum/C15-production-readiness-and-incident-response.md) T2; P02 running on kind |

### 1. Problem statement

**Business context (composite scenario).** In a Beacon incident, Freightline orders sat in `PENDING` for 70 minutes. On-call had Prometheus graphs and `kubectl logs` but no traces; the cause, an exhausted Postgres pool in inventory, sat in one trace nobody could find. Cobalt Bank then wrote observability into its contract: its security operations center (SOC) wants every application log in Splunk within 60 s, its CAB approves configuration but not per-customer code, and all egress crosses a TLS-inspecting proxy. Northstar wants the same telemetry in Datadog.

**Constraints.** One image per service everywhere: the destination must be configuration. Ship-to addresses and consignee names never leave the cluster. Egress crosses a proxy with a corporate CA and later sites are air-gapped (P16), so the default mode needs no SaaS. Cobalt pays for Splunk ingest per GB.

**Stakeholders.** Beacon's SRE lead (owns on-call), Freightline service owners, Cobalt's SOC lead and CAB chair, Northstar's platform team, and Beacon's FDE (you).

**Measurable success criteria.**
1. Alert → failing trace → its logs in 3 clicks and under 2 minutes, timed in a game day.
2. RED panels for all four services and USE panels for every pod and node, from one template.
3. A 50% error burst on `orders` pages within 5 minutes; a steady 0.1% error rate (1x burn of a 99.9% SLO) never pages in 2 hours.
4. Export mode is a values change only: no pod or image changes in `freightline`, counts within 0.1% of the Loki path over 30 minutes of k6, and no log loss across a 10-minute sink outage.
5. Under 50,000 active series for the whole lab.

### 2. Requirements

**Functional requirements**
- **FR-1** Services send traces and metrics over OTLP only to `otel-gateway.observability:4317`; a node agent collects stdout JSON logs.
- **FR-2** Metrics reach Prometheus's OTLP receiver with promoted resource attributes, including `freightline_pod_template_hash` for P03.
- **FR-3** Traces reach Tempo (span metrics and exemplars included); logs reach Loki with `trace_id` and `span_id` as structured metadata.
- **FR-4** Grafana links exemplar → trace → logs and log line → trace.
- **FR-5** RED, USE and Freightline-flow dashboards are JSON in Git.
- **FR-6** SLO specs in Git generate multi-window, multi-burn-rate rules with separate `page` and `ticket` routes.
- **FR-7** An overlay adds Splunk HEC or Datadog exporters with PII removal, a persistent queue and proxy/CA support.
- **FR-8** The pipeline alerts on its own queue depth, send failures and refused data.

**Non-functional requirements**

| Attribute | Target |
|---|---|
| Overhead | SDK CPU overhead < 5% at 100 req/s; gateway `memory_limiter` at 80% of a 1 Gi limit |
| Freshness | Metrics < 30 s; Loki < 15 s; customer sink < 60 s |
| Durability | No log loss across a 10-minute sink outage; gateway ×2 with a PDB |
| Cardinality | < 50,000 active series; no order, customer or trace IDs as labels |
| Retention / cost | 72 h per signal; $0 in the lab; customer ingest volume signed off before go-live |

**Constraints.** No per-customer code; pinned charts and images; applications speak only OTLP.

**Out of scope.** Long-term metrics (Mimir, Thanos), tail sampling (an extension), audit logging for the customer's SIEM of record, and OpenTelemetry Profiles (public alpha as of September 2026).

### 3. Architecture

```text
 ZONE: workloads (ns freightline, PSA restricted)     ZONE: pipeline (ns observability)
 +---------------------------------------+   OTLP    +--------------------------------------------+
 | orders (Go), inventory (Py),          |   :4317   | otel-gateway (Collector contrib 0.161, x2) |
 | billing + notifications (TS):         |---------->| memory_limiter > k8s_attributes > resource |
 | SDK traces, metrics, exemplars;       |           | [attributes/pii in export mode]            |
 | stdout JSON logs (trace_id, span_id)  |           | exporters with sending_queue + batch       |
 +------------------+--------------------+           +------+-----------+-----------+--------+----+
                    | /var/log/pods                         |           |           |        |
 +------------------v--------------------+   OTLP    ^      |           |           |        |
 | otel-agent DaemonSet: file_log >      |-----------+      |           |           |        |
 | container > json_parser (trace, level)|                  |           |           |        |
 | alloy (optional): pyroscope.ebpf ---> Pyroscope :4040    |           |           |        |
 +---------------------------------------+                  v           v           v        |
 ZONE: backends (ns monitoring)                  Tempo :4317/:3200  Loki :3100  Prometheus   |
   Tempo metrics-generator --remote_write + exemplars--> Prometheus "kps" :9090 (OTLP recv)  |
   Grafana: exemplar > Tempo > Loki (trace_id); Loki derived field > Tempo                   |
   Alertmanager: page / ticket > Mailpit (lab) or the customer's incident tool               |
 ===== TRUST BOUNDARY: customer egress (proxy re-signs TLS; destination allow-list) ==========|
   export mode: splunk_hec/cobalt > Cobalt Splunk HEC :8088 | datadog/northstar > Datadog <--+
   lab stand-in: ns customer-sim (Collector with a splunk_hec receiver and a debug exporter)
```

| Component | Responsibility | Technology | Why | Alternative considered |
|---|---|---|---|---|
| `otel-gateway` | Receive, enrich, fan out | Collector contrib, Deployment ×2 | Vendor-neutral; all customer exporters in contrib | Vendor agent per customer |
| `otel-agent` | Tail pod logs, lift `trace_id` | Collector contrib DaemonSet | Same config and path as the gateway | Alloy (Loki-only output) |
| Prometheus + Alertmanager | Metrics, exemplars, rules, routing | kube-prometheus-stack | Built-in OTLP receiver; P03 depends on it | Mimir 3 (needs Kafka) |
| Tempo / Loki | Traces / logs | Monolithic modes | No Kafka; native OTLP | Jaeger v2 / OpenSearch |
| Grafana | Dashboards, cross-signal links | Bundled with the stack | One UI over every backend | Perses (pre-1.0) |
| Alloy + Pyroscope (optional) | eBPF CPU profiles | DaemonSet | No code change | Pyroscope SDK |

**Mini-ADRs**

| # | Decision | Options | Choice | Consequences |
|---|---|---|---|---|
| ADR-P04-1 | How telemetry leaves the app | SDK to each backend; vendor agent per customer; one OTLP gateway | OTLP to `otel-gateway` only | A customer backend is a values overlay; the gateway becomes tier 1 (replicas, PDB, `memory_limiter`, queue alerts) |
| ADR-P04-2 | Log collection | OTel logs SDK; node agent on stdout | Node agent parsing JSON | JS and Python OTel logs SDKs are still "Development", and `kubectl logs` keeps working. Cost: one parser per log format |
| ADR-P04-3 | Metrics transport | Scrape `/metrics`; OTLP push | OTLP push | One protocol, exportable as-is. No `up` metric (use `absent_over_time()`); pushers need an out-of-order window |
| ADR-P04-4 | SLO tooling | Hand-written rules; Pyrra; Sloth | Sloth in CI | No controller in customer clusters; the rule diff is change evidence. SLO views live in Grafana |

### 4. Tools & technologies

| Tool | Version / status (as of September 2026) | Notes |
|---|---|---|
| OpenTelemetry | CNCF Graduated 2026-05-11 | HTTP semconv stable; logs SDK maturity varies by language |
| Collector contrib / chart | 0.161.0 / `opentelemetry-collector` 0.173.1 | snake_case renames (`otlp_grpc`, `otlp_http`, `file_log`, `k8s_attributes`); old names are deprecated aliases. Exporter `sending_queue.batch` replaces the `batch` processor |
| Prometheus / kube-prometheus-stack | 3.14.0 / 91.5.1 | Exemplars still need `exemplar-storage`; native histograms stable since 3.8 |
| Grafana / Loki / Tempo | 13.2.2 / 3.7.8 / 3.0.3 | OSS charts are community-maintained at `grafana-community` (Loki's moved 2026-03-16): grafana 13.2.5, loki 18.13.5, tempo 3.0.0 |
| Alloy / Pyroscope | v1.19.2 / 2.3.1 | Grafana Agent EOL 2025-11-01; Alloy's `alloy otel` engine is experimental |
| Sloth / Pyrra | v0.16.0 / v0.10.2 | Multi-window, multi-burn-rate rule generators |
| Incident routing | Alertmanager → your incident tool | Opsgenie shuts down 2027-04-05 |

### 5. Step-by-step implementation plan

**M1 — Instrumentation hygiene (3 h).** *Identity:* the library chart appends `service.instance.id=$(POD_UID)` (downward API) to `OTEL_RESOURCE_ATTRIBUTES`, or two replicas push identical series. *Probes:* kubelet probes add about 0.6 req/s of free "good" traffic per pod, so exclude them (Python: `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS=healthz,readyz`; Node: an `instrumentation.ts` that registers `@opentelemetry/instrumentation/hook.mjs` and sets `ignoreIncomingRequestHook`; Go below). *Exemplars:* set `OTEL_METRICS_EXEMPLAR_FILTER=trace_based` everywhere.

```go
// services/orders/cmd/orders/main.go: replaces otelhttp.NewHandler(mux, "orders") from P02 M2
notProbe := func(r *http.Request) bool { return r.URL.Path != "/healthz" && r.URL.Path != "/readyz" }
apiHandler := otelhttp.NewHandler(mux, "orders", otelhttp.WithFilter(notProbe))
```

*Done when* the rendered chart carries the instance ID for all four services (expected: `4`):

```bash
helm template freightline deploy/helm/freightline -f deploy/envs/kind/values.yaml | grep -c 'service.instance.id=$(POD_UID)'
```

**M2 — Prometheus with the OTLP receiver (4 h).** P03 installs Prometheus with this file; `fullnameOverride: kps` yields the `kps-prometheus` Service its AnalysisTemplate queries. OTLP metrics get `job` = `service.namespace/service.name` and `instance` = `service.instance.id`.

```yaml
# deploy/observability/kps-values.yaml
fullnameOverride: kps
prometheus:
  prometheusSpec:
    enableOTLPReceiver: true            # --web.enable-otlp-receiver
    enableRemoteWriteReceiver: true     # Tempo's metrics-generator writes here
    enableFeatures: [exemplar-storage]
    exemplars: { maxSize: 100000 }
    tsdb: { outOfOrderTimeWindow: 30m } # several gateway replicas push
    otlp:
      promoteResourceAttributes: [service.version, deployment.environment.name, k8s.namespace.name, k8s.pod.name, freightline.pod_template_hash]
    retention: 72h
    ruleSelectorNilUsesHelmValues: false
    serviceMonitorSelectorNilUsesHelmValues: false
prometheus-node-exporter:
  hostRootFsMount: { enabled: false }   # kind / Docker Desktop reject the shared root mount
alertmanager:
  config:                               # lists replace the chart defaults, so keep "null"
    route:
      receiver: ticket
      group_by: [alertname, service]
      routes:
        - { matchers: ['alertname="Watchdog"'], receiver: "null" }
        - { matchers: ['severity="page"'], receiver: page }
    receivers:
      - name: "null"
      - name: page
        email_configs: [{ to: page@lab.local, from: am@lab.local, smarthost: "mailpit.freightline:1025", require_tls: false }]
      - name: ticket
        email_configs: [{ to: ticket@lab.local, from: am@lab.local, smarthost: "mailpit.freightline:1025", require_tls: false }]
grafana:
  sidecar:
    datasources:
      exemplarTraceIdDestinations: { datasourceUid: tempo, traceIdLabelName: trace_id }
  additionalDataSources:
    - name: Tempo
      uid: tempo
      type: tempo
      url: http://tempo.monitoring:3200
      jsonData:
        serviceMap: { datasourceUid: prometheus }
        tracesToLogsV2:
          datasourceUid: loki
          filterByTraceID: true
          spanStartTimeShift: "-5m"
          spanEndTimeShift: "5m"
          tags: [{ key: service.name, value: service_name }]
    - name: Loki
      uid: loki
      type: loki
      url: http://loki.monitoring:3100
      jsonData:
        derivedFields:   # "$$" stops Grafana provisioning from expanding ${__value.raw}
          - { name: trace_id, matcherType: label, matcherRegex: trace_id, datasourceUid: tempo, url: "$${__value.raw}" }
```

*Done when* the container args include `--web.enable-otlp-receiver` and `--enable-feature=exemplar-storage`:

```bash
kubectl -n monitoring get pod prometheus-kps-prometheus-0 -o jsonpath='{.spec.containers[?(@.name=="prometheus")].args}'
```

**M3 — Tempo and Loki, monolithic (3 h).** Loki's OTLP endpoint stores `trace_id` and `span_id` as structured metadata and indexes `service.name` as `service_name`.

```yaml
# deploy/observability/tempo-values.yaml
tempo:
  reportingEnabled: false
  retention: 72h
  metricsGenerator:
    enabled: true
    storage:
      path: /var/tempo/generator
      remote_write: [{ url: "http://kps-prometheus.monitoring:9090/api/v1/write", send_exemplars: true }]
  overrides:
    defaults:
      metrics_generator: { processors: [service-graphs, span-metrics] }
```

```yaml
# deploy/observability/loki-values.yaml
deploymentMode: Monolithic
loki:
  auth_enabled: false
  commonConfig: { replication_factor: 1 }
  storage: { type: filesystem }
  useTestSchema: true          # lab only; production pins a real schemaConfig
  limits_config: { allow_structured_metadata: true }
singleBinary: { replicas: 1 }
gateway: { enabled: false }
chunksCache: { enabled: false }
resultsCache: { enabled: false }
lokiCanary: { enabled: false }
test: { enabled: false }
```

*Done when* `kubectl -n monitoring get pods` shows `tempo-0` and `loki-0` Ready.

**M4 — Gateway and agent Collectors (5 h).**

```yaml
# deploy/observability/otel-gateway-values.yaml
mode: deployment
replicaCount: 2
fullnameOverride: otel-gateway       # Service otel-gateway.observability, the P02 contract
image: { repository: otel/opentelemetry-collector-contrib, tag: "0.161.0" }
command: { name: otelcol-contrib }
resources: { limits: { memory: 1Gi } }
podDisruptionBudget: { enabled: true, minAvailable: 1 }
presets: { kubernetesAttributes: { enabled: true } }
ports: { metrics: { enabled: true } }
serviceMonitor: { enabled: true }
config:
  receivers: { jaeger: null, zipkin: null, prometheus: null }
  processors:
    batch: null                      # batching moves into each exporter's sending_queue
    k8s_attributes:
      pod_association:               # logs arrive from the agent: match on pod UID, not source IP
        - sources: [{ from: resource_attribute, name: k8s.pod.uid }]
        - sources: [{ from: connection }]
    resource:
      attributes: [{ key: k8s.cluster.name, value: freightline-kind, action: upsert }]
  exporters:
    otlp_grpc/tempo: { endpoint: "tempo.monitoring:4317", tls: { insecure: true }, sending_queue: { batch: {} } }
    otlp_http/prometheus: { endpoint: "http://kps-prometheus.monitoring:9090/api/v1/otlp", sending_queue: { batch: {} } }
    otlp_http/loki: { endpoint: "http://loki.monitoring:3100/otlp", sending_queue: { batch: {} } }
  service:
    pipelines:
      traces: { receivers: [otlp], processors: [memory_limiter, k8s_attributes, resource], exporters: [otlp_grpc/tempo] }
      metrics: { receivers: [otlp], processors: [memory_limiter, k8s_attributes, resource], exporters: [otlp_http/prometheus] }
      logs: { receivers: [otlp], processors: [memory_limiter, k8s_attributes, resource], exporters: [otlp_http/loki] }
```

The agent is the same chart as a DaemonSet (`otel-agent`, `presets.logsCollection.enabled: true`, other defaults set to `null`) with one pipeline: `file_log → memory_limiter → k8s_attributes → otlp_grpc` to the gateway. Its `k8s_attributes` maps the pod label `app.kubernetes.io/name` to `service.name`, which gives Loki its `service_name`. Parsing:

```yaml
# deploy/observability/otel-agent-values.yaml (receiver excerpt)
config:
  receivers:
    file_log:
      include: [/var/log/pods/freightline_*/app/*.log]
      operators:
        - { type: container, id: container-parser }
        - type: json_parser
          if: 'body matches "^\\{"'
          parse_from: body
          severity:
            parse_from: attributes.level
            mapping: { info: 30, warn: 40, error: 50, fatal: 60 }   # pino (Node) logs numeric levels
          trace:
            trace_id: { parse_from: attributes.trace_id }
            span_id: { parse_from: attributes.span_id }
```

*Done when* (with section 6's port-forwards running) idle traffic reads `0` for all four jobs, proving the probe filter:

```bash
curl -s localhost:9090/api/v1/query --data-urlencode 'query=sum by (job) (rate(http_server_request_duration_seconds_count{job=~"freightline/.*"}[5m]))'
```

and an `orders` log line carries a trace ID (one stream returned):

```bash
curl -s -G localhost:3100/loki/api/v1/query_range --data-urlencode 'query={service_name="orders"} | trace_id != ""' --data-urlencode 'limit=1'
```

**M5 — RED and USE dashboards as code (5 h).** Three JSON dashboards in `deploy/observability/dashboards/`, shipped as ConfigMaps labeled `grafana_dashboard: "1"`: *RED* (a `$service` variable over `job`), *USE* (pods and nodes) and *Flow* (P02's outbox backlog age, consumer lag and DLQ depth, plus Tempo's service graph), all with P03's deploy annotations.

| Panel | PromQL |
|---|---|
| Rate / errors | `sum by (job) (rate(http_server_request_duration_seconds_count{job=~"$service"}[$__rate_interval]))`; errors add `http_response_status_code=~"5.."` and divide |
| Duration p99, exemplars on | `histogram_quantile(0.99, sum by (job, le) (rate(http_server_request_duration_seconds_bucket{job=~"$service"}[$__rate_interval])))` |
| USE | `container_cpu_usage_seconds_total` ÷ CPU request; throttled ÷ total CFS periods; working-set memory ÷ limit; restarts and `OOMKilled` from kube-state-metrics |

*Done when* the three dashboards are loaded (expected: `3`):

```bash
kubectl -n monitoring get configmap -l grafana_dashboard=1 --no-headers | grep -c freightline
```

**M6 — SLOs and burn-rate alerts (4 h).** Two 30-day SLOs for `orders`: availability 99.9% and latency 99% within 250 ms. 250 ms, not the 300 ms NFR, because 0.25 is a default HTTP histogram bucket boundary and 0.3 would be interpolated. Helm never renders this file, because `{{.window}}` is Sloth's placeholder:

```yaml
# deploy/observability/slo/orders.yaml
apiVersion: sloth.slok.dev/v1
kind: PrometheusServiceLevel
metadata: { name: freightline-orders, namespace: monitoring }
spec:
  service: orders
  slos:
    - name: requests-availability
      objective: 99.9
      sli:
        events:
          errorQuery: sum(rate(http_server_request_duration_seconds_count{job="freightline/orders",http_response_status_code=~"5.."}[{{.window}}]))
          totalQuery: sum(rate(http_server_request_duration_seconds_count{job="freightline/orders"}[{{.window}}]))
      alerting:
        name: OrdersAvailabilityBurn
        annotations: { runbook_url: "docs/runbooks/slo-burn.md" }
        pageAlert: { labels: { severity: page } }
        ticketAlert: { labels: { severity: ticket } }
```

The latency SLO's `errorQuery` is total requests minus the `le="0.25"` bucket. Sloth emits the SRE-workbook pairs: page on 14.4x burn over 1 h and 5 m or 6x over 6 h and 30 m; ticket on 3x over 1 d and 2 h or 1x over 3 d and 6 h. From a clean hour, burn rate *B* trips the 1 h window after about 60 × 14.4 / *B* minutes: under 2 minutes for a 50% error burst (*B* = 500), about 43 for 2% errors (*B* = 20). Alertmanager (M2 file) routes `page` and `ticket` to different Mailpit addresses and `Watchdog` to `null`.

*Done when* the rule exists and section 7's burn tests pass:

```bash
kubectl -n monitoring get prometheusrule freightline-orders
```

**M7 — Customer export mode (5 h).** The Cobalt profile overlays the gateway release; Helm merges maps and replaces lists, so it names only what changes:

```yaml
# deploy/observability/export-splunk-values.yaml (Cobalt profile)
extraEnvs:
  - name: SPLUNK_HEC_TOKEN
    valueFrom: { secretKeyRef: { name: customer-splunk-hec, key: token } }
  # At Cobalt also: HTTPS_PROXY=http://<proxy>:8080 and NO_PROXY=.svc,.cluster.local
extraVolumes: [{ name: queue, emptyDir: {} }]            # production: statefulset mode + PVC
extraVolumeMounts: [{ name: queue, mountPath: /var/lib/otelcol/queue }]
config:
  extensions:
    file_storage/queue: { directory: /var/lib/otelcol/queue }
  processors:
    attributes/pii:
      actions: [{ key: ship_to, action: delete }, { key: consignee_name, action: delete }]
  exporters:
    splunk_hec/cobalt:
      endpoint: http://customer-sim.customer-sim:8088/services/collector   # Cobalt: https://<hec-host>:8088/...
      token: ${env:SPLUNK_HEC_TOKEN}
      index: freightline
      sourcetype: freightline:otel
      # tls: { ca_file: /etc/cobalt-ca/ca.crt }   # the proxy re-signs TLS with Cobalt's CA
      sending_queue: { storage: file_storage/queue, batch: {} }
      retry_on_failure: { max_elapsed_time: 0s }  # retry until the persistent queue fills
  service:
    extensions: [health_check, file_storage/queue]
    pipelines:
      logs:
        processors: [memory_limiter, k8s_attributes, resource, attributes/pii]
        exporters: [otlp_http/loki, splunk_hec/cobalt]
```

The Northstar profile adds a `datadog/connector` (it computes APM stats; the exporter no longer does by default) and a `datadog/northstar` exporter (`api: { site: datadoghq.com, key: ${env:DD_API_KEY} }`); the connector exports from the traces pipeline and receives into the metrics pipeline. An OTLP-native backend needs only an `otlp_http/customer` exporter with endpoint and auth header; Cobalt's Splunk takes HEC, hence `splunk_hec`. `customer-sim` is a third release of the chart with only a `splunk_hec` receiver on 8088 and a `debug` exporter.

*Done when* section 6's `diff` is empty and both log exporters report the same count:

```bash
curl -s localhost:9090/api/v1/query --data-urlencode 'query=sum by (exporter) (increase(otelcol_exporter_sent_log_records_total[30m]))'
```

**M8 — Profiles (optional) and the game day (3 h).** Run Alloy's `pyroscope.ebpf` as a DaemonSet writing to `http://pyroscope.monitoring:4040`; it needs root in the host PID namespace, so lab only unless a customer approves in writing. Then shrink inventory's pool to 2, drive 100 req/s and time alert → exemplar → trace → logs.

*Done when* two people who did not build the stack each reach the failing span's logs in under 2 minutes, with timings in `docs/evidence/p04/`.

### 6. Deployment instructions

Order: Prometheus and Grafana, Tempo and Loki, gateway, agent, SLOs and dashboards, then the Freightline upgrade (M1). Secrets such as `SPLUNK_HEC_TOKEN` never go into values files.

```bash
helm install kps prometheus-community/kube-prometheus-stack --version 91.5.1 -n monitoring --create-namespace -f deploy/observability/kps-values.yaml
```

```bash
helm install tempo oci://ghcr.io/grafana-community/helm-charts/tempo --version 3.0.0 -n monitoring -f deploy/observability/tempo-values.yaml
```

```bash
helm install loki oci://ghcr.io/grafana-community/helm-charts/loki --version 18.13.5 -n monitoring -f deploy/observability/loki-values.yaml
```

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
```

```bash
helm install otel-gateway open-telemetry/opentelemetry-collector --version 0.173.1 -n observability --create-namespace -f deploy/observability/otel-gateway-values.yaml
```

```bash
helm install otel-agent open-telemetry/opentelemetry-collector --version 0.173.1 -n observability -f deploy/observability/otel-agent-values.yaml
```

Generate the SLO rules (`go install github.com/slok/sloth/cmd/sloth@v0.16.0`), apply them with the dashboards, and upgrade Freightline:

```bash
sloth generate -i deploy/observability/slo/orders.yaml -o deploy/observability/slo/generated/orders.rules.yaml
```

```bash
kubectl apply -f deploy/observability/slo/generated/ -f deploy/observability/dashboards/
```

```bash
helm upgrade freightline deploy/helm/freightline -n freightline -f deploy/envs/kind/values.yaml --wait
```

Verify through port-forwards (one terminal each); Grafana is at `localhost:3000`, user `admin`, password from the last command:

```bash
kubectl -n monitoring port-forward svc/kps-prometheus 9090:9090
```

```bash
kubectl -n monitoring port-forward svc/loki 3100:3100
```

```bash
kubectl -n monitoring port-forward svc/kps-grafana 3000:80
```

```bash
kubectl -n monitoring get secret kps-grafana -o jsonpath='{.data.admin-password}' | base64 -d
```

**Customer export mode.** Snapshot the workloads, create the token and the stand-in sink, apply the overlay, and compare (expected: no output):

```bash
kubectl -n freightline get pods -o custom-columns=POD:.metadata.name,STARTED:.status.startTime,IMAGE:.spec.containers[0].image > before.txt
```

```bash
kubectl -n observability create secret generic customer-splunk-hec --from-literal=token=lab-only-token
```

```bash
helm install customer-sim open-telemetry/opentelemetry-collector --version 0.173.1 -n customer-sim --create-namespace -f deploy/observability/customer-sim-values.yaml
```

```bash
helm upgrade otel-gateway open-telemetry/opentelemetry-collector --version 0.173.1 -n observability -f deploy/observability/otel-gateway-values.yaml -f deploy/observability/export-splunk-values.yaml
```

```bash
kubectl -n freightline get pods -o custom-columns=POD:.metadata.name,STARTED:.status.startTime,IMAGE:.spec.containers[0].image | diff before.txt -
```

**Rollback** to Beacon-only mode; applications are untouched either way:

```bash
helm rollback otel-gateway -n observability
```

**Teardown.** Everything lives in P02's cluster:

```bash
kind delete cluster --name freightline
```

### 7. Testing & validation

| Test | How | Pass threshold |
|---|---|---|
| Config | `otelcol-contrib validate` on rendered configs in CI; `promtool check rules` on Sloth output | Zero errors; no deprecated component names |
| Correlation | Script: exemplar → Tempo trace → Loki by `trace_id` | 20/20 resolve to a trace with ≥ 1 log line |
| Fast / slow burn | `FAULT_5XX_RATE=0.5` (P03's fault hook), then `0.001` for 2 h | Page in < 5 min; no page on the slow burn |
| Export parity and outage | 30-min k6 run; `customer-sim` scaled to 0 for 10 min | Counts within 0.1%; queue drains in < 5 min |
| Gateway chaos | Delete a gateway pod every 2 min for 20 min | k6 `http_req_failed` unchanged; trace gaps < 1% |
| Cardinality and PII | Active series at 100 req/s; search Loki and the sink for a seeded consignee name | < 50,000 series; zero hits |

### 8. Observability & operations

Monitor the monitor via the gateway's ServiceMonitor:

| Alert | Expression (sketch) | Severity |
|---|---|---|
| TelemetryExportFailing | `rate(otelcol_exporter_send_failed_log_records_total[5m]) > 0` for 10 min, plus span and metric-point variants | Ticket; page for a contractual customer sink |
| TelemetryQueueFilling | `otelcol_exporter_queue_size / otelcol_exporter_queue_capacity > 0.8` for 10 min | Page: data loss is minutes away |
| TelemetryRefused | `rate(otelcol_receiver_refused_spans_total[5m]) > 0` | Ticket: usually `memory_limiter` pushback |
| OrdersMetricsAbsent | `absent_over_time(http_server_request_duration_seconds_count{job="freightline/orders"}[10m])` | Page: OTLP push has no `up` |

Runbook entries (`docs/runbooks/`):
1. **Queue filling.** TLS or connection errors to the customer sink mean proxy or CA: test from the gateway's network with the customer's network team, and never set `insecure_skip_verify`. A full queue rejects new data (`block_on_overflow` defaults to false), so tell the customer's SOC the affected window first.
2. **SLO page.** Follow an exemplar from the p99 panel, look for a deploy annotation, and switch to P03's abort runbook if a canary is live. If the trace-to-logs link is empty, check `trace_id` parsing and `service_name` extraction in the agent.

### 9. Security & compliance

| Threat | Scenario | Control implemented |
|---|---|---|
| Information disclosure | Consignee names reach a third-party backend | `attributes/pii`, log-field allow-lists, a signed-off payload sample |
| Spoofing | Any pod pushes fake telemetry | NetworkPolicy admits only `freightline` and `observability` on 4317; receiver TLS at customer sites |
| Credential exposure | HEC token or Datadog key in Git | Secret referenced as `${env:...}`; External Secrets in P05 |
| Denial of service | A telemetry storm exhausts the gateway | `memory_limiter`, bounded queues, promote only named attributes |
| Repudiation | Dropped logs treated as evidence | Persistent queue and delivery metrics; security audit logs stay on the customer's SIEM forwarder of record, because this pipeline is not tamper-evident |

**Compliance mapping (Cobalt).** PCI DSS v4.0.1 Requirement 10 (12 months' log retention, 3 immediately available) binds in-scope systems; Freightline is outside the cardholder data environment (see P25). The SIEM feed supports NYDFS Part 500 audit trails and DORA ICT-incident classification (applicable since 2025-01-17); the overlay goes through the CAB as configuration.

### 10. Extensions for advanced learners

1. **T3 — Tail sampling:** a `load_balancing` exporter tier routing by trace ID to a `tail_sampling` tier. *Hard because* it needs two tiers, memory sized for decision windows, and span metrics computed before sampling or every rate lies.
2. **T3 — Native histograms end to end** with Pyrra v0.10 SLOs. *Hard because* P03's `_bucket` queries and every dashboard change, and both series types coexist during migration.
3. **T4 — Air-gapped telemetry (P16)** on Mimir 3 and Tempo 3 in Kafka-backed RF1 modes. *Hard because* Kafka becomes a telemetry dependency you run without vendor support.
4. **T4 — Multi-tenant telemetry for P27.** *Hard because* cardinality must be capped per tenant and tenant A must provably never see tenant B's traces.
5. **T4 — Zero-code instrumentation with OBI v0.13 (pre-1.0)** for P01's SOAP façade. *Hard because* it needs kernel 5.8+ with BTF and privileges PSA `restricted` forbids.

### 11. How to demonstrate it in interviews

**2-minute pitch.** "Every customer mandates a different backend, so I made the destination configuration. Four services in three languages send only OTLP to a Collector gateway, and a node agent lifts trace IDs out of stdout JSON. By default everything lands in-cluster, in Prometheus with exemplars, Tempo and Loki, so one click goes from a latency spike to the trace to its logs. SLOs are Sloth specs generating multi-window burn-rate alerts. For a composite bank whose SOC mandated Splunk, export mode was a Helm overlay: a HEC exporter behind a persistent queue, PII stripped. No application pod restarted, counts matched within 0.1%, and a 10-minute sink outage lost nothing."

**10-minute demo flow.**
1. Diagram and ADR-P04-1 (1 min).
2. Latency panel → exemplar → trace → logs, live (2 min).
3. `kps-values.yaml` and why P03 depends on it (1 min).
4. Sloth spec, generated rules, detection maths (1.5 min).
5. Fast-burn injection; the page lands in Mailpit (1.5 min).
6. Export overlay: the empty `diff`, the parity query (2 min).
7. Sink outage and queue drain; trade-offs (1 min).

**Likely questions and strong-answer outlines.**
1. *"Why a gateway, not the vendor's agent?"* One build, one place for PII rules, queues and cardinality; concede the tier-1 component you now operate.
2. *"How do you stop cardinality blow-ups?"* Named promoted attributes only, no IDs as labels, series counted in CI load tests.
3. *"Why 250 ms when the requirement said 300?"* Bucket boundaries and interpolation error; native histograms fix it.
4. *"The customer's proxy breaks TLS?"* Mount their CA, set `NO_PROXY`, never skip verification; the queue buys time.
5. *"Splunk for traces too?"* Whoever operates it decides; the gateway keeps the choice reversible.

**Artifacts to bring:** diagram, ADRs, Sloth spec and rules, an exemplar-to-logs screenshot, the parity result with the empty `diff`, the game-day timings.

**Metrics to quote:** alert-to-logs time, active series at 100 req/s, export parity, queue drain time, SDK CPU overhead.

**What you would do differently:** fix probe filtering and instance IDs before the first dashboard (every early SLO number was wrong); book the customer's network team early: proxy and CA work is the long pole.

### 12. Common failure points while building

| Failure | Symptom | Fix |
|---|---|---|
| No `service.instance.id` | Counters "reset", `rate()` spikes, out-of-order sample errors | Pod UID in `OTEL_RESOURCE_ATTRIBUTES` (M1) |
| Trace-to-logs empty | "Logs for this span" returns nothing | Missing `service_name` (agent label extraction) or `trace_id` (JSON not parsed) |
| Old component names | Deprecation warnings; `dynamic_sampling` fails (renamed `adaptive_tail_sampling`, no alias) | `otlp_grpc`, `otlp_http`, `file_log`, `k8s_attributes`; exporter batching instead of `batch` |
| Wrong chart source | Stale Loki chart, no `Monolithic` mode | OSS Loki, Tempo and Grafana charts come from `grafana-community`; the in-repo Loki chart now targets GEL |
| Customer proxy | `x509: certificate signed by unknown authority`, or in-cluster calls sent to the proxy | CA as `tls.ca_file`; `NO_PROXY=.svc,.cluster.local` |
| NetworkPolicy | Alert emails never arrive | Freightline's default-deny blocks Alertmanager → Mailpit: allow ingress from `monitoring` on 1025 |

**See also:** [M09 Observability and production debugging](../../01-curriculum/M09-observability-and-production-debugging.md); [P4 Cloud deployment and monitoring](../../03-projects/P4-cloud-deployment-and-monitoring.md) (managed-cloud version).

---

## Sources

Incidents and supply chain
- Google Cloud Service Control incident (2025-06-12): https://status.cloud.google.com/incidents/ow5i3PPK96RduMcb1SsW
- CISA, tj-actions/changed-files compromise (2025-03-18): https://www.cisa.gov/news-events/alerts/2025/03/18/supply-chain-compromise-third-party-tj-actionschanged-files-cve-2025-30066-and-reviewdogaction
- Trivy advisory GHSA-69fq-xp46-6x23 (March 2026): https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23
- Cloudflare "Fail Small" plan and completion: https://blog.cloudflare.com/fail-small-resilience-plan/ · https://blog.cloudflare.com/code-orange-fail-small-complete/
- GitHub Actions secure use and OIDC: https://docs.github.com/en/actions/reference/security/secure-use · https://docs.github.com/en/actions/reference/security/oidc

Platform and delivery
- Kubernetes, Ingress-NGINX retirement: https://kubernetes.io/blog/2025/11/11/ingress-nginx-retirement/
- Argo Rollouts releases: https://github.com/argoproj/argo-rollouts/releases
- OWASP Top 10:2025: https://top10.owasp.org/2025
- RFC 9457, Problem Details for HTTP APIs: https://www.rfc-editor.org/rfc/rfc9457
- Standard Webhooks specification: https://www.standardwebhooks.com/

Observability (P04)
- OpenTelemetry CNCF status: https://www.cncf.io/projects/opentelemetry/
- Prometheus OTLP guide and feature flags: https://prometheus.io/docs/guides/opentelemetry/ · https://prometheus.io/docs/prometheus/latest/feature_flags/
- Collector exporterhelper (queue, batch): https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/exporterhelper/README.md
- Collector contrib changelog (renames): https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/CHANGELOG.md
- Grafana Loki, OTLP ingestion: https://grafana.com/docs/loki/latest/send-data/otel/
- Loki Helm chart move to grafana-community: https://github.com/grafana/loki/tree/main/production/helm/loki · https://github.com/grafana-community/helm-charts
- Grafana Tempo 3.0 release notes: https://grafana.com/docs/tempo/latest/release-notes/v3-0/
- Alloy OpenTelemetry Engine; Grafana Agent EOL: https://grafana.com/docs/alloy/latest/set-up/otel_engine/ · https://grafana.com/docs/agent/latest/
- Sloth: https://github.com/slok/sloth
- Google SRE Workbook, "Alerting on SLOs": https://sre.google/workbook/alerting-on-slos/
- Opsgenie shutdown: https://www.atlassian.com/software/opsgenie/migration

## Related / Next

- **Next projects:** [P05-P08 Cloud, multi-cloud and IaC](./02-cloud-multicloud-and-iac.md) moves Freightline onto AWS, Azure and GCP; [P21-P24 Security and reliability](./06-security-and-reliability.md) hardens it and runs the game day on P04's telemetry.
- **Curriculum:** [C07 API design & integration](../02-curriculum/C07-api-design-and-integration.md) · [C09 Containers & Kubernetes](../02-curriculum/C09-containers-and-kubernetes.md) · [C10 DevOps & CI/CD](../02-curriculum/C10-devops-and-cicd.md) · [C12 Observability & monitoring](../02-curriculum/C12-observability-and-monitoring.md) · [C15 Production readiness & incident response](../02-curriculum/C15-production-readiness-and-incident-response.md)
- **Practice:** [Debugging and log-analysis drills](../04-drills/02-debugging-and-log-analysis-drills.md) · [Deployment and incident drills](../04-drills/03-deployment-and-incident-drills.md)
- **Interview:** [T091-T120 distributed systems, observability and AI](../07-interview/05-technical-questions-systems-observability-ai.md) · [SD01-SD14 core infrastructure design](../07-interview/06-systems-design-core-infrastructure.md)
- **Plans and templates:** [90-day plan](../08-plans/03-90-day-plan.md) · [Production readiness checklist](../../08-templates/production-readiness-checklist.md) · [Incident report template](../../08-templates/incident-report-template.md)
