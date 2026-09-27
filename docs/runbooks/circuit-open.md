# Runbook: SOAP circuit open

**Alert:** `CircuitOpen` (ticket), when the breaker has been open for more than 5 minutes:

```text
min_over_time(gateway_circuit_state[5m]) == 2
```

**What shippers see:** `POST /v1/rate-quotes` fails fast with `503` `circuit-open` and a `Retry-After`. Shipment reads and webhooks are not affected; `/readyz` still reports `ready`, with `"soap_circuit":"open"`.

The breaker opens after 5 retryable failures in a row (`Server*` SOAP faults, 5xx, 429, timeouts, connection errors). After 30 s it lets exactly one trial call through (half-open); success closes it, failure re-opens it for another 30 s.

## 1. Confirm, and see how long

```text
gateway_circuit_state                                   # 0 closed, 1 half-open, 2 open
max_over_time(gateway_upstream_inflight[15m])           # must never exceed 4
```

Grafana (lab: <http://127.0.0.1:3000>) → Explore → Prometheus. Or ask the API directly:

```bash
curl -s localhost:8000/readyz
```

## 2. Load or network? Read the fault classes

```bash
docker compose logs gateway-api --since 15m | grep -E "upstream still failing|Server.Busy|ConnectError|ConnectTimeout|timeout" | tail -20
```

- **`Server.Busy` faults: load.** Meridian is at its 5-call limit. Check that *our* side held the bulkhead: `max_over_time(gateway_upstream_inflight[15m])` must be ≤ 4. If it is, the extra load comes from Meridian's own callers, so call their integration on-call and share the time window. If it is above 4, that's our bug: page the gateway owner, because the bulkhead is the contract with Meridian (ADR-P01-1).
- **`ConnectError` / `ConnectTimeout` / total timeouts: network.** Before calling Meridian, test the path from the gateway's subnet. In production that is Direct Connect → the DMZ. In the lab: `docker compose exec gateway-api python -c "import socket; socket.create_connection(('soap-mock', 8080), 2); print('tcp ok')"`. If TCP fails from our side, it's a network ticket (routes, security groups, the Direct Connect virtual interface), not Meridian's.
- **`502 upstream-invalid-response` in our logs** is a different problem: Meridian answered with something we can't use (an HTML error page, an unexpected fault code). It doesn't open the breaker. Check whether its endpoint or certificate changed.

## 3. Do not

- **Do not raise the bulkhead or the retry count to "push through".** Retries multiply load on a failing system (3 attempts × 50 clients against 5 slots). That's the 2025-06-12 Google Cloud lesson in the spec.
- **Do not restart the API to "reset" the breaker.** Every replica starts closed and immediately sends 4 calls into the failure; the breaker is doing its job.

## 4. Recovery

When Meridian recovers, the next trial call closes the circuit by itself within 30 s: `gateway_circuit_state` goes 2 → 1 → 0. Idempotency keys make shipper retries safe, because a `503` stored nothing against the key.

## 5. Afterwards

Record the start and end times, the fault class, and the in-flight maximum in the incident notes. If Meridian's p99 or its concurrency limit changed, revisit ADR-P01-1 with their written confirmation.
