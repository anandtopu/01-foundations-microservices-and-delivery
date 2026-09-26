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

## Ingest data flow (M3)

```text
IBM i job ──writes──> X.csv, then X.csv.done  (SFTP drop, read-only to us)
                               │
sftp-poller, every 60 s (5 s demo), one active replica (pg_try_advisory_lock)
  1. list the drop; keep X.csv only if X.csv.done exists
  2. fast path: (name, size, mtime) of a finished file? -> skip without download
  3. download; sha256; INSERT ingested_files ... ON CONFLICT (name, size, sha256) -> done? skip
  4. decode cp1252 -> header check -> ShipmentRow per line (resume after last_line)
  5. per 1,000 lines, ONE transaction:
       upsert shipments (no-op if unchanged) + dead_letters (row) + last_line checkpoint
  6. status = done
```

### M4 additions to the differences log

| # | Difference | Why | Milestone |
|---|---|---|---|
| 9 | `render_xml` also escapes `"` and `'` | `saxutils.escape` covers only `& < >`, which is safe in element text but not in attribute values; the spec's helper would be unsafe the day someone interpolates into an attribute | M4 |
| 10 | A malformed or DTD/entity-bearing SOAP response raises `UpstreamRejected` (API: 502), never retried | The spec's code lets `ParseError` / `EntitiesForbidden` escape unclassified, which would surface as a raw 500 and bypass the M5 error taxonomy | M4 |
| 11 | `RateQuoteRequest` is `strict=True, extra="forbid"`, ZIP pattern `[0-9]{5}` (not `\d`) | Matches the contract exactly (`type: number`, `additionalProperties: false`), so M8 Schemathesis negative tests pass; `\d` would accept non-ASCII digits | M4 |
