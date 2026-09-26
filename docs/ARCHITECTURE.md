# Architecture: P01 Legacy Integration Gateway (as built)

This file tracks the architecture **as built**. It starts from the spec's section 3 diagram and changes only when the build diverges, with the reason recorded in `BUILD_LOG.md` (and an ADR if a decision changed).

## Target (spec section 3)

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

## Lab topology (what actually runs)

One Docker Compose project with 7 services on one bridge network. The trust boundary is simulated: the "data center" side is the three mocks.

| Compose service | Stands in for | Published port | Built in | Status |
|---|---|---|---|---|
| `postgres` (postgres:18) | Managed Postgres | 127.0.0.1:5432 | M2 | built (M2) |
| `sftp` (debian:trixie-slim + openssh-server) | Meridian DMZ SFTP | 2222 → 22 | M2 | built (M2) |
| `soap-mock` (FastAPI) | `RateQuoteService` | 8080 | M2 | built (M2) |
| `webhook-sink` (FastAPI) | A shipper's webhook receiver | 9000 | M2 | built (M2) |
| `gateway-api` | `meridian-gateway-api` | 8000 | M8 | planned |
| `sftp-poller` | sftp-poller worker | none | M3/M8 | code built (M3), runs on the host via `make poll-local`; container in M8 |
| `webhook-dispatcher` | webhook-dispatcher worker | none | M7/M8 | planned |

## Known differences from the spec (running log)

| # | Difference | Why | Milestone |
|---|---|---|---|
| 1 | Docker runs on a `dockerd` we start ourselves (cgroup v1, containerd image store) | The cloud VM ships dockerd but does not start it | M0 |
| 2 | Container egress: `deb.debian.org` is blocked (403), and container TLS does not trust the session proxy's CA | Cloud network allowlist and proxy; affects image builds only | M0 (found), M2/M8 (handled) |
| 3 | `deb.debian.org` is reachable as of session 3; Docker Hub returns 429 on the shared egress IP, so `make base-images` pulls base images from `mirror.gcr.io` and retags them | Anonymous Hub quota (100/h per IP) is shared with other tenants | M2 |
| 4 | The SFTP host key is pinned once, under `sftp`, read from inside the container; the M2 gate uses `-o HostKeyAlias=sftp` instead of `ssh-keyscan -p 2222 localhost` | The spec's gate (M2) and deploy steps (section 6) pin under different names in the same file | M2 |
| 5 | The CSV has a `SHIPPER_CODE` column, and `ShipmentRow` has a `shipper_code` field that becomes `shipments.client_id` | The spec's row has no owner, so section 9's BOLA filter would be impossible (decision A, M1) | M3 |
| 6 | `ingested_files` carries a `last_line` checkpoint and a status (`in_progress`, `done`, `rejected`) | The spec says "commit per 1,000-row batch"; a checkpoint that commits with each batch is what makes a restart mid-file exactly-once (M9 chaos row) | M3 |
| 7 | A file with a bad header or undefined cp1252 bytes is rejected whole (one dead letter, line 1) | No row of a file we cannot decode can be trusted | M3 |
| 8 | `SFTP_HOST_KEY_ALIAS` setting (asyncssh `host_key_alias`) | Lets the poller run on the host against `localhost:2222` while verifying the single `sftp` pin | M3 |
| 9 | `render_xml` escapes `"` and `'` too, applies `!r`/format specs, and refuses XML 1.0-illegal characters | `saxutils.escape` covers only `& < >` (not enough in attribute values); the spec's helper silently dropped conversions/format specs and passed control characters that make the envelope ill-formed (PR #2 review) | M4 |
| 10 | A malformed, DTD-bearing (`forbid_dtd=True`) or bogus-encoding SOAP response is classified, never an unclassified exception: `UpstreamRejected` on 2xx/4xx, `RetryableError` on 5xx/429. The API mapping (502/503) arrives in M6 | The spec's code lets `ParseError` / `EntitiesForbidden` / `LookupError` escape, which would be a raw 500 and bypass the M5 taxonomy | M4 |
| 11 | `RateQuoteRequest` is `strict=True, extra="forbid"`, ZIP pattern `[0-9]{5}` (not `\d`) | Matches the contract exactly (`type: number`, `additionalProperties: false`), so M8 Schemathesis negative tests pass; `\d` would accept non-ASCII digits | M4 |
| 12 | `ShipmentRow` extras beyond `shipper_code`: `weight_lb` is `Field(ge=0, max_digits=12, decimal_places=2)` and must be plain digits; `parse_cyymmdd` requires exactly `[01]` + 6 digits | A weight that overflows `numeric(12,2)` must dead-letter one row, not fail the batch; the spec's `int()`-based parsing accepted `'12609 1'`, `'12609249'` (8th digit dropped) and C=2..9 (year 2826); `Decimal` accepted `1e2` and `1_000` (PR #2 review) | M3 |
| 13 | Lines are split on LF only (CRLF/LF), not `str.splitlines()`; a line with a NUL byte or unparseable CSV is a dead letter | `splitlines()` also splits on `\x0b \x0c \x1c-\x1e` and a bare `\r`, which shifted every later `line_no`; Postgres text cannot hold NUL, which crashed the poller (PR #2 review) | M3 |
| 14 | The (name, size, mtime) fast path skips a finished file without downloading or hashing it; the (name, size, sha256) key stays the authoritative dedupe | FR-1 says "tracked by name, size and SHA-256"; hashing needs a download, and re-downloading every finished file every 60 s does not scale. mtime may only skip work, never decide identity | M3 |
| 15 | Within a batch the last line for a shipment wins (earlier ones counted as `superseded`); a file may not change a shipment's `client_id` (dead letter `owner change refused`); a file Postgres refuses (`DataError`) is marked `rejected` and the loop continues | Postgres cannot update one row twice in one `INSERT ... ON CONFLICT` (a crash loop, PR #2 review); an owner change is a tenant-boundary (BOLA) violation, not an update; one poisoned file must not block every later file | M3 |
| 16 | SOAP call: one total 3 s deadline (`asyncio.timeout`), every `httpx.TransportError` retryable, a 1 MiB response cap, `Server*` faults / other 5xx / 429 retryable, fault codes compared by QName local part; weights rendered as plain decimals | httpx's read timeout restarts per chunk (a slow drip took 10 s); `RemoteProtocolError` escaped unclassified; the spec's mapping made HTML 500s and generic `Server` faults non-retryable, so the M5 breaker would never open; `endswith` matched `evil:NotServer.Busy`; `str(1e-05)` is not `xs:decimal` (PR #2 review) | M4 |
| 17 | `QuoteInput` Protocol types the adapter's `q` parameter | The SOAP layer must not import the API layer | M4 |
| 18 | `CircuitOpenError` carries `retry_after` (time left until the half-open trial), and the breaker exposes `retry_after()` | The contract promises `Retry-After` on every 503; a circuit-open 503 should say when a trial will be admitted, not a constant | M5 |
| 19 | `Bulkhead.in_flight` counter alongside the semaphore | Tests and the section 8 bulkhead gauge need to read occupancy; the semaphore stays the only limit | M5 |
| 20 | `POST /v1/rate-quotes` exists from M5, without auth or idempotency storage; `Location` points at a `GET` that M6 adds | The M5 gate (503 in under 10 ms, k6 burst) needs a real HTTP surface; M6 adds the key store and GET, M8 auth | M5 |
| 21 | Retry-After: `2` for a saturated bulkhead and for retries exhausted (ADR-P01-1), ceil(time to trial) for an open circuit | The contract requires the header; RFC 9110 delay-seconds are whole numbers, so round up and never send 0 | M5 |

## Ingest data flow (M3)

```text
IBM i job ──writes──> X.csv, then X.csv.done  (SFTP drop, read-only to us)
                               │
sftp-poller, every 60 s (5 s demo), one active replica (pg_try_advisory_lock on the working connection)
  1. list the drop; keep X.csv only if X.csv.done exists
  2. fast path: (name, size, mtime) of a finished file? -> skip without download
  3. download; sha256; INSERT ingested_files ... ON CONFLICT (name, size, sha256) -> done? skip
  4. decode cp1252 -> header check -> ShipmentRow per line (resume after last_line)
  5. per 1,000 lines, ONE transaction:
       last line per shipment wins -> owner check (FOR UPDATE; a change is a dead letter)
       -> upsert shipments (no-op if unchanged) + dead_letters (row) + last_line checkpoint
     (Postgres DataError -> file rejected, loop continues)
  6. status = done
```
