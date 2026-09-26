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
| `webhook-sink` (FastAPI) | A shipper's webhook receiver | 9000 (HTTPS since M7) | M2 | built (M2), TLS with a lab CA (M7) |
| `gateway-api` | `meridian-gateway-api` | 8000 | M8 | built (M8): `meridian-gateway` image, default command (uvicorn); `make api-local` still runs it on the host |
| `sftp-poller` | sftp-poller worker | none | M8 | built (M8): same image, `python -m gateway.ingest.poller`; the only service holding the SFTP key |
| `webhook-dispatcher` | webhook-dispatcher worker | none | M8 | built (M8): same image, `python -m gateway.webhooks.dispatcher`; the only service trusting the lab CA |

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
| 20 | `POST /v1/rate-quotes` exists from M5 (auth, idempotency and `GET` arrived in M6) | The M5 gate (503 in under 10 ms, k6 burst) needs a real HTTP surface | M5 |
| 21 | Retry-After: `2` for a saturated bulkhead and for retries exhausted (ADR-P01-1), ceil(time to trial) for an open circuit, `1` while a half-open trial is in flight | The contract requires the header; RFC 9110 delay-seconds are whole numbers, so round up and never send 0 | M5 |
| 22 | Circuit breaker: a call remembers the generation it was admitted in (bumped on every open); a stale result does not change the state, and only the call that owns the half-open trial may clear the trial flag | The spec's breaker let a call admitted while CLOSED that succeeded after the breaker opened close it at once, skipping the cool-down and the single trial; stale failures pushed `opened_at` back; any finishing call could admit a second trial (PR #3 review, reproduced) | M5 |
| 23 | `retry_full_jitter` bounds each attempt by the remaining deadline (`asyncio.timeout_at`), and rejects `attempts < 1` | The spec checked the deadline only before a sleep, so a late attempt ran its full 3 s past it: worst case about 6.2 s holding a bulkhead slot instead of 4 s (PR #3 review) | M5 |
| 24 | A success response whose TotalCharge/Currency/TransitDays break the contract is `502 upstream-invalid-response` (not the shipper's fault); TotalCharge is normalised to 2 decimals; a junk QuoteRef is omitted | `int()`/pass-through let `"1e3"`, `"usd"`, `-4` and a 23-digit day count into a 201 (PR #3 review) | M5 |
| 25 | `RequestGuard` middleware: `413` over 64 KiB (also for chunked bodies), `415` for a non-JSON body; 404/405 and malformed JSON (`400`) are Problem Details; `errors[]` capped at 20 with RFC 6901 pointers; `instance` on 500 | FR-8 "every error"; spec section 9's 64 KB limit; a 10.5 MB body produced a 22 MB 422 (PR #3 review) | M5 |
| 26 | No `/openapi.json`, no `server` header | FastAPI's generated schema differs from the design-first contract; the server header is needless disclosure | M5 |
| 27 | API-key auth arrives in M6, not M8: `api_keys` table (SHA-256 hash, client_id, scope `shipper`/`ops`, revocable, several active keys per client for rotation with overlap), `python -m gateway.auth add` (key from `$API_KEY`, never argv) and `make dev-keys` | Idempotency is keyed by `(client_id, key)` (ADR-P01-2), so the gateway must know the caller before M6 can work | M6 |
| 28 | A failed upstream call deletes the in-progress key (`release`), so the same key can be retried; only a 201 is stored for replay | The contract's 503 promises "nothing was stored against your Idempotency-Key"; replaying a 503 would pin a transient failure to the key for 24 h | M6 |
| 29 | Quote responses (first, replay and GET) are rendered as canonical JSON (sorted keys, compact) | Replays come back from `jsonb`, which reorders object keys: without this the M6 gate saw 2 distinct bodies for 20 clients | M6 |
| 30 | `rate_quotes` stores the exact 201 body; `GET /v1/rate-quotes/{id}` filters on `client_id` and `expires_at` | The GET must return the same document as the POST (byte-identical), and another shipper's or an expired quote is "not found" (BOLA) | M6 |
| 31 | Idempotency fencing: `begin` returns the lease's `locked_until` as a token (typed `Owned`); `complete` and `release` require it | Without it an owner whose lease expired could overwrite the new owner's result or delete its claim, letting a third request call Meridian concurrently (PR #4 review, reproduced) | M6 |
| 32 | Circuit-open fast path: when the breaker would refuse, the route only *reads* the key (`peek`: replay / 409 / 422) and fails fast, claiming nothing; valid API keys are cached in-process for 10 s (revocation takes effect within 10 s) | M6 put auth + claim + release in front of the breaker: circuit-open 503s went from 1.2 ms to a 7.1 ms median with a 10.9 ms max, breaking M5's "under 10 ms" (PR #4 review); now median 2.5 ms, max 6.0 ms over 300 calls | M6 |
| 33 | Shipper endpoints require the `shipper` scope (`shipper_principal`, 403 for ops keys) | Spec section 9 "ops scope separate": a leaked ops key must not also quote as a phantom shipper (PR #4 review) | M6 |
| 34 | `gateway.auth add` never re-activates or moves an existing key (ON CONFLICT DO NOTHING, non-zero exit), refuses an empty `$API_KEY` and keys under 32 characters unless `--allow-weak` | It used to resurrect a revoked (leaked) key, even as another tenant's ops key, and silently register an unknown random key when `$API_KEY` was empty (PR #4 review, reproduced) | M6 |
| 35 | Pool wait 5 s (default 30 s) and `PoolTimeout` → `503 service-unavailable`; a failed `release` is logged, never masks the real error | A starved pool or a database hiccup during cleanup turned a 503/502 into a 500 (PR #4 review) | M6 |
| 36 | Known contract gaps: idempotency keys are not purged after 24 h (a completed key replays a quote whose `Location` is 404 after 15 min); the per-key `429` rate limit is not implemented | Planned for M9 (retention job) and later (rate limiting); recorded so the contract's promises are not mistaken for the lab's behaviour | M6 |
| 37 | `WEBHOOK_DEV_ALLOW_HOSTS` (exact hostnames, empty by default) skips the SSRF *address* check for those names only; the lab sets `localhost` in `make api-local` / `make dispatch-local` | The lab sink is on 127.0.0.1, which `assert_public_https` rightly refuses; HTTPS is still enforced, and production leaves the setting empty | M7 |
| 38 | The webhook sink serves HTTPS with a lab CA (`make certs`, EC P-256, `secrets/sink-tls/`); only the dispatcher trusts it (`WEBHOOK_CA_BUNDLE`, added to the system store) | The spec requires HTTPS webhooks; a real shipper's endpoint has a public certificate | M7 |
| 39 | Webhook secrets are stored in plaintext in `webhook_subscriptions.secret` (returned once, never again by the API) | HMAC signing needs the secret itself, so it cannot be hashed like an API key; production encrypts it with KMS (envelope encryption) | M7 |
| 40 | The dispatcher's claim is a 60 s lease committed *before* the HTTP call (not a lock held across it); a crashed dispatcher's rows are retried after the lease: at-least-once, receivers de-duplicate on `webhook-id` (the stable `delivery_id`) | Holding `FOR UPDATE` across a 5 s network call pins a connection and a transaction per delivery | M7 |
| 41 | The ingest upsert reports insert vs update with Postgres 18 `RETURNING old.*, new.*` (not the `xmax = 0` trick) | Needed to emit `shipment.created` vs `shipment.status_changed` with `previous_status`; documented, not an implementation detail | M7 |
| 42 | Every dead-letter replay attempt is audited in `dead_letter_replays`, including rejected and already-resolved ones; a webhook replay re-queues the *same* delivery and is resolved only when the dispatcher delivers it | Spec section 9 "Repudiation"; a 202 is not a delivery | M7 |
| 43 | `rate_quote.completed` is emitted only when a quote is stored (`complete()`), never on a replay, a failure or a released key | One event per quote, in the same transaction as the quote row | M7 |
| 44 | Known gap, not closed: a DNS-rebinding window remains between the SSRF check's lookup and httpx's own connect. A hostile name server can answer "public" then "internal"; TLS verification stops the request body, but the TCP connect and ClientHello reach the internal address (blind port probing). Per-attempt checks only defeat *slow* DNS changes | Closing it needs connecting to the checked IP (a custom network backend) or the spec's egress proxy; production relies on the allow-listing egress proxy (PR #5 review, reproduced) | M7 |
| 45 | `ssrf.guard()` adds checks on top of the spec's verbatim `assert_public_https`: NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`) and IPv4-compatible (`::/96`) addresses are judged by their embedded IPv4; URLs with whitespace, control characters or userinfo are refused (parsed with httpx's own parser); the lookup shares the attempt's 5 s budget (2 s in the API); a refusal's 422 detail is fixed text | Python's `is_global` is True for NAT64 of 169.254.169.254; `urlsplit` silently drops `\t\n\r` so the guard and the client read different URLs; the old 422 detail revealed internal DNS answers (PR #5 review, all reproduced) | M7 |
| 46 | Signing-side secret rotation is not built: one secret per subscription, one `v1,` signature. The verifier already accepts several signatures | The spec's 24 h dual-signing needs a second secret column and an API to add it; planned, not needed for the gate | M7 |
| 47 | A `410` disables the subscription, but siblings already in the same concurrent batch are still sent; their outcomes are ignored (410s are recorded first, and a cancelled row is never retried or dead-lettered) | Batch attempts run concurrently; grouping by subscription would serialise a shipper's deliveries (PR #5 review) | M7 |
| 48 | `DELETE /v1/webhook-subscriptions/{id}` removes the subscription's deliveries, history included (`ON DELETE CASCADE`); its open webhook dead letters stay listed but replay answers `422` | The contract said "cancelled"; now it says what happens. Keeping history would need soft deletes | M7 |
| 49 | Dispatcher hardening: the response body is never read (`client.stream`, `Accept-Encoding: identity`); `attempt()` never raises and each `record()` is isolated; every outcome write is fenced by the claim's lease (`next_attempt_at`) and `status = 'pending'`; the max age is checked by the database clock and the last retry is clamped to the end of the window; rows of a disabled subscription are cancelled at claim time | One receiver could crash the dispatcher for every tenant (InvalidURL, DecodingError) or exhaust its memory (gzip bomb); a late outcome could flip `delivered` to `dead`; DST made the age wrong in a zoned session (PR #5 review, all reproduced) | M7 |
| 50 | At most 25 subscriptions per shipper (`422 subscription-limit-reached`); a second webhook replay while one is queued or in flight is `409 already-queued`; the replay audit records the ops key's fingerprint (`ops_key_id`, migration 0004); the ingest upsert never updates another owner's row (`WHERE s.client_id = EXCLUDED.client_id`) | Fan-out amplification; two replicas sending one row; every ops key shares one client_id; a replay racing the poller wrote one shipper's data into another's shipment (PR #5 review, reproduced) | M7 |
| 51 | Known limits: a row replay has no staleness check (replaying an old line can move a status backwards); a quote whose `complete()` lost its lease is returned to the caller but emits no `rate_quote.completed`; the dev host allowance ignores the port; the lab CA has no name constraints | Recorded for the runbook and later milestones (PR #5 review) | M7 |
| 52 | A SOAP Client fault (Meridian refuses to quote, e.g. an origin ZIP it does not serve) is `422 upstream-rejected`, not `502`; `502` now means only `upstream-invalid-response` | The shipper's input caused it and must change: a `5xx` invites retries and pages us for their typos. Found by Schemathesis (`not_a_server_error`), changed in the contract before any shipper integrated | M8 |
| 53 | `schemathesis.toml` declares, for 4 operations only, the one documented rejection a JSON Schema cannot express as expected by `positive_data_acceptance`: a made-up cursor (`400`, 2 list operations), an SSRF-refused URL or the subscription limit (`422`), a reused `Idempotency-Key` or an upstream refusal (`422`). Every other check and status keeps its default | These depend on server state, DNS or Meridian, not on the request's shape; the spec's gate command runs unchanged (the file is read from the working directory) | M8 |
| 54 | The image is `python:3.14-alpine` (musl), not Debian slim, and the venv's shared objects are stripped in a throwaway Debian stage | `python:3.14-slim` is 125 MB unpacked before 98 MB of locked dependencies, so a slim image cannot pass "under 200 MB"; every binary dependency has a musllinux cp314 wheel. The strip stage is Debian because the lab allowlist blocks `dl-cdn.alpinelinux.org` | M8 |
| 55 | `docker image ls` on this VM reports DISK USAGE = unpacked + compressed layers (containerd image store, difference 1): the gateway image shows **179 MB** there, is 130 MB unpacked and 42 MB compressed | The gate is read with the spec's own command; the other two numbers are recorded so the classic-store figure (~130 MB) is not mistaken for a different result | M8 |
| 56 | `/readyz` checks the SFTP drop with a credential-free probe (TCP connect + `SSH-2.0-` banner), not an SFTP login | The API container never holds the SFTP private key; the poller's own errors and the ingest-age alert cover "our key no longer works" | M8 |
| 57 | Gateway containers run with a read-only root filesystem, `tmpfs /tmp`, `cap_drop: [ALL]` and `no-new-privileges`; each gets only its own secrets (poller: SFTP key + `known_hosts`; dispatcher: the lab CA); `make keys` chowns the SFTP key to uid 10001 | Compose file-secrets are bind mounts that keep host ownership: a root-owned `0600` key is unreadable to `USER 10001` | M8 |

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

## Webhook data flow (M7)

```text
poller batch / rate-quote complete()          ONE transaction
  upsert shipments (RETURNING old/new) ──> event? created | status_changed | rate_quote.completed
  outbox.enqueue: INSERT webhook_deliveries per matching ACTIVE subscription (fan-out)
                               │ commit (no event without its change, no change without its event)
webhook-dispatcher, every 1 s
  1. claim: due pending rows, FOR UPDATE SKIP LOCKED, next_attempt_at = now() + 60 s (the lease,
     also the fencing token for step 3); COMMIT. A disabled subscription's rows are cancelled, not sent
  2. per row: SSRF guard -> sign(secret, delivery_id, ts, exact body) -> POST, 5 s total for both,
     no redirects, status code only (the body is never read)
  3. record (only if the row still holds OUR lease; 410s first):
              2xx -> delivered (+ resolve an open webhook dead letter)
              410 -> subscription disabled, its pending rows cancelled
              else -> retry in uniform(0, min(6 h, 30 s * 2^n)); past WEBHOOK_MAX_AGE -> dead + dead_letters
ops: POST /v1/dead-letters/{id}:replay -> same delivery re-queued (audit row always)
```
