# Interview notes: P01 Legacy Integration Gateway

Every number here was **measured in this lab**: Docker Compose on a 4-CPU, 16 GB cloud VM, with mocks standing in for Meridian's IBM i. The spec's targets are quoted only as targets. The M-number after each number points to the `docs/BUILD_LOG.md` section with the raw output.

## The 2-minute pitch

> "In a composite 3PL, an AS/400 dropped CSVs over SFTP, a SOAP rate service fell over above five concurrent calls, and nothing on the IBM i side could change. So everything lives in a gateway.
>
> I wrote the OpenAPI 3.1 contract first. Schemathesis fuzzes the running service against it on every build: about 3,000 generated cases, zero failures.
>
> Files are ingested exactly once, by content hash plus a per-batch checkpoint that commits with the rows. I SIGKILLed the poller in the middle of a 20,000-row file, and it resumed from line 3,001 and ended with exactly 19,960 shipments and 40 dead letters, with no duplicate events. Bad rows are dead-lettered for an audited replay.
>
> The SOAP service sits behind a bulkhead of four, full-jitter retries and a circuit breaker. Under a 50-VU burst at the spec's 2.8-second latency, the mock never saw more than 4 concurrent calls. When I killed it, the breaker opened in 1.2 seconds and closed itself 30 seconds after recovery.
>
> Postgres idempotency keys meant 20 concurrent duplicates cost Meridian exactly one call. Webhooks go through a transactional outbox with Standard Webhooks signatures and an SSRF guard: p99 delivery 4.5 seconds.
>
> Reads held a 26 ms p95 at 200 requests a second, and the one thing that broke that SLO was our own tracing, which I found by bisecting it."

## Metrics to quote (measured)

| Metric | Value | Conditions | Milestone |
|---|---|---|---|
| Read p95 | **26.05 ms** (p90 7.8 ms, max 166 ms) | 200 req/s for 5 min, constant arrival rate, 60,002 requests, 0 errors, 0 dropped; spec target < 150 ms | M9 |
| Peak upstream concurrency | **4** (the mock allows 5) | 50 VUs for 60 s, mock latency 2.8 s: 88 × 201 and 10,741 fast 503s with `Retry-After`, 0 other errors | M9 (also M5) |
| Duplicate upstream calls under 20 concurrent retries | **0**: one call, 20 identical bodies | the M6 burst: 20 clients, one key, `409 Retry-After` then replay | M6; the M9 smoke test also shows `calls: 1` |
| Breaker open after the upstream dies | **1.2 s** (spec < 10 s); closed again 30.5 s after recovery | `docker compose kill soap-mock` | M9 |
| Exactly-once under a mid-file crash | **19,960 / 19,960** shipments, **40 / 40** dead letters, **6,597 / 6,597** events | SIGKILL at line 3,001 with a batch uncommitted | M9 |
| Webhook delivery age | **p50 2.75 s, p99 4.53 s** (SLI 60 s) | 657 events from a 2,000-row file | M9 |
| Freshness (`.done` → committed) | **4.3 s** | 2,000-row file with the demo's 5 s poll; production polls every 60 s, which bounds it | M9 |
| Postgres down 30 s | **0 × 500**; `503` + not-ready; ready **0.6 s** after it returned; **0 restarts** | `docker compose stop postgres` | M9 |
| Contract conformance | **0 failures**, 3,125 of 3,125 cases | Schemathesis 4.28 `--checks all` | M8, M9 |
| Image | **179 MB** listed (136.5 MB unpacked), `USER 10001`, **0 CVEs** | Trivy 0.74.0 pinned by digest, after removing the base image's unused pip | M8, M9 |
| Branch coverage (ingest, resilience, webhooks) | **96%** (every module ≥ 91%) | spec ≥ 90%; it was 88% before M9 added worker-loop tests | M9 |

## The story behind two numbers

These are good follow-up material, because they show debugging rather than a result.

- **The read SLO failed first, and the cause was our observability.** The first 200 req/s run gave a p95 of **2.23 s** with 8,493 dropped iterations. Bisecting with the same image: without instrumentation, p95 was 3.4 ms at 200 req/s; with traces off (metrics on) 4.8 ms at 200 req/s; with metrics off (traces on) still 311 ms at only 100 req/s. So the cost was trace export: the batch span processor encodes and ships hundreds of spans per flush inside the process, holding Python's GIL. The fix was 10% head sampling with 500 ms flushes (p95 **26 ms**). The metrics still count every request, so the SLIs lose nothing.
- **Exactly-once had to be proven, not asserted.** A batch commits in about 150 ms, so "kill it mid-way" by hand is luck. The chaos script takes the shipment-writer advisory lock once 3,000 lines are committed, so the next batch blocks inside its transaction; then it SIGKILLs the poller. The worst case is guaranteed, not hoped for.

## Likely questions (strong-answer outlines)

1. **"Why not just retry harder?"** Retries multiply load on a failing system: 3 attempts × 50 clients against 5 slots. The Google Cloud incident of 2025-06-12 is a herd effect. Budget the retries, add jitter, and bound concurrency; the bulkhead of 4 is the contract with Meridian.
2. **"Is ingestion exactly-once?"** Effectively-once:
   - the file is deduplicated by its SHA-256;
   - each 1,000-row batch commits together with its checkpoint and its outbox rows;
   - upserts are idempotent.

   Show the 20k SIGKILL numbers.
3. **"Two requests with the same key at once?"** One wins the `INSERT`; the other gets `409` with `Retry-After`. A 30 s lease with a fencing token handles a crashed or slow owner, and a stale owner cannot overwrite the new one.
4. **"Why Postgres rather than Kafka for webhooks?"** At 250k rows a day, a `SKIP LOCKED` outbox is transactional with the change it announces and adds no new system to operate. Name the volume, or the fan-out to other consumers, that would change your mind.
5. **"How do you know the SOAP limit is 5?"** It was measured in a joint test window and confirmed in writing. The bulkhead is 4, to leave headroom for Meridian's own callers.
6. **"What did review find?"** Each milestone had three independent review agents. The findings worth telling:
   - one tenant's bad URL or gzip bomb could stop webhooks for everyone;
   - a dead-letter replay could race the poller and write into another shipper's shipment;
   - `updated_at = now()` let a late-committing writer hide rows from an `updated_since` feed;
   - the fuzzer itself registered real internet domains as webhook targets.

   Each is fixed, with a test that fails if the fix is reverted.

## 10-minute demo script (spec section 11)

Before the demo:
```bash
bash scripts/cloud-setup.sh
```

```bash
make up
```

Check it is ready:
```bash
curl -s localhost:8000/readyz
```

Grafana is at <http://127.0.0.1:3000>, then Explore and Prometheus.

1. **Diagram and trust boundary (1 min).** `docs/ARCHITECTURE.md`: the target diagram, the lab topology, and the differences log (the build is honest about where it deviates).
2. **Contract and Problem Details (1 min).** `contracts/openapi.yaml`, then a Problem Details body:
   ```bash
   curl -s localhost:8000/v1/shipments/NOPE -H 'X-API-Key: dev-shipper-key'
   ```
   This is a `404`, identical for another shipper's shipment (BOLA).
3. **CSV with a bad status code (1.5 min).** On a fresh stack; on an old one the same content is skipped by its hash, which is itself worth showing.
   ```bash
   make drop F=fixtures/csv/SHPSTS_20260924_0915.csv
   ```
   Then show 3 rows and 2 dead letters:
   ```bash
   curl -s 'localhost:8000/v1/dead-letters?resolved=false' -H 'X-API-Key: dev-ops-key'
   ```
4. **One quote twice with the same `Idempotency-Key` (1 min).** Run the spec's smoke command twice, then `curl -s localhost:8080/__stats`: `calls` stays at 1, and the second response carries `Idempotent-Replayed: true`.
5. **Overload (2 min).**
   ```bash
   curl -s -X POST localhost:8080/__faults -H 'Content-Type: application/json' -d '{"busy_rate":1.0}'
   ```
   ```bash
   make load-quotes
   ```
   Show `503` with `Retry-After`, then `max_over_time(gateway_upstream_inflight[5m])` in Grafana: flat at 4. Kill the mock (`python3 scripts/m9/chaos_kill_soap.py`): `gateway_circuit_state` goes 0 → 2 → 1 → 0. Reset with `{"busy_rate":0}`.
6. **Webhooks (2 min).** Stop the sink (`docker compose stop webhook-sink`), drop a file, and show the dispatcher's jittered, growing retry gaps (`docker compose logs -f webhook-dispatcher`). Then start the sink and show `signature=valid`. Replay a dead letter with the ops key: `202`, then delivered and resolved, plus an audit row with the key's fingerprint.
7. **SSRF table and what I would change (1.5 min).** Run `uv run pytest tests/unit/test_webhooks_signing_ssrf.py -q`: loopback, RFC 1918, `169.254.169.254`, `[::1]`, NAT64, a name that resolves to `10.0.0.5`, and a `302` to the metadata service are all refused. Then close with the section below.

**Artifacts to bring:**
- the diagram and the ADRs (`docs/adr/`);
- the OpenAPI file;
- the k6 summaries (reads: p95 26 ms at 200 req/s; quotes: peak 4);
- the breaker open/close screenshot from Grafana;
- a one-page postmortem of the poller killed mid-file (the M9 BUILD_LOG section has the numbers).

## What I would do differently

- **Offer an asynchronous quote API** (`202` plus a `rate_quote.completed` webhook). The upstream p99 of 2.8 s will not improve, and synchronous quotes hold a bulkhead slot for all of it.
- **Put the egress proxy in place on day one.** The DNS-rebinding window between our check and the connection stays open until then (ARCHITECTURE difference 44).
- **Get the SOAP concurrency limit in writing first.** The whole resilience design depends on that one number.
- **Budget observability like any other dependency.** Exporting every trace turned a 3.4 ms read p95 into 2.23 s at 200 req/s. Sampling should be a design decision with a measured budget, not a default.
- **Run more than one API replica before calling a rollback safe.** The single-replica rollback here was about 20 s of unavailability, because Compose stops the old container before the new one is ready.
