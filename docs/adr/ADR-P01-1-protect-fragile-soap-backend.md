---
status: accepted
date: 2026-09-26
decision-makers: Beacon FDE (P01), Meridian VP Customer Integration
consulted: Meridian IBM i team lead, Meridian security architect
---

# ADR-P01-1: Protect the fragile SOAP backend with a global bulkhead, bounded retries and a circuit breaker

## Context and Problem Statement

Meridian's SOAP 1.1 `RateQuoteService` accepts at most 5 concurrent requests (p99 2.8 s) and returns HTTP 500 with a SOAP Fault for both "bad request" and "busy". A shipper's retry loop took it down in a previous peak season. Success criterion 2: no shipper behaviour, including aggressive retries, may push more than 4 concurrent requests onto it. How does the gateway guarantee that?

## Decision Drivers

* A hard ceiling on upstream concurrency, independent of how many shippers or gateway requests there are
* Fail fast and tell the client when to come back, rather than queue and time out
* Retries must not multiply load on an already failing upstream (the 2025-06-12 Google Cloud herd effect)
* Change-frozen upstream: every protection lives in the gateway

## Considered Options

* Rate limit per shipper
* Global semaphore (bulkhead) + bounded full-jitter retries + circuit breaker
* Queue and asynchronous reply (`202` + webhook)

## Decision Outcome

Chosen option: "Global bulkhead of 4 + bounded full-jitter retries + circuit breaker", composed `bulkhead → retry → breaker → per-attempt timeout`, with a fast `503` + `Retry-After` on saturation or an open circuit. Per-shipper rate limits bound *request rate*, not *concurrency*: five shippers each under their limit can still exceed 5 in flight. A queue is the better long-term design (see Consequences), but it changes the shipper-facing API.

### Consequences

* Good, because upstream concurrency is capped at 4 by construction, leaving 1 slot for Meridian's own callers.
* Good, because the breaker sits *inside* the retry loop, so `CircuitOpenError` (not retryable) ends retries immediately.
* Bad, because shippers see `503 Retry-After: 2` at peak; the limit is therefore documented in `contracts/openapi.yaml`.
* Bad, because one slow shipper can occupy slots other shippers need (per-shipper fair share is extension T3-2).
* Neutral: an async quote API (`202` + `rate_quote.completed` webhook) is the recommended follow-up, because the upstream p99 will not improve.

### Confirmation

M5 gate: with the mock's `busy_rate` at 1.0 the breaker opens on the second call and later calls return `503` in < 10 ms; `/__stats` peak concurrency stays ≤ 4 during a 50-VU k6 burst.
