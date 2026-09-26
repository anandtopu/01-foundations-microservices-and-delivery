---
status: accepted
date: 2026-09-26
decision-makers: Beacon FDE (P01)
---

# ADR-P01-2: Keep idempotency state in Postgres, keyed by (client_id, key) with a request hash

## Context and Problem Statement

Success criterion 3: retried `POST /v1/rate-quotes` calls with the same `Idempotency-Key` never produce a second upstream call. Shippers retry on timeouts, so two identical requests can arrive concurrently, on different gateway replicas, or after a restart. Where does the "have I seen this key?" state live?

## Decision Drivers

* Must survive restarts and work across multiple API replicas
* Must be atomic under concurrent identical requests (exactly one wins)
* Must recover a key whose owner crashed mid-flight
* No new infrastructure for 250k rows/day

## Considered Options

* In-memory cache in the API process
* Redis (`SET NX` with TTL)
* Postgres table `idempotency_keys` with `INSERT ... ON CONFLICT`

## Decision Outcome

Chosen option: "Postgres table", primary key `(client_id, key)`, storing a SHA-256 of the canonical request body, a state (`in_progress` or `completed`), the stored response and a `locked_until` lease. In-memory state fails across replicas and restarts; Redis would work but adds a component and cannot share a transaction with the quote row.

### Consequences

* Good, because `ON CONFLICT` makes "first one wins" atomic; losers get `409` + `Retry-After` or the stored replay.
* Good, because `locked_until` lets a new request take over a key whose worker crashed, instead of `409` forever.
* Good, because the same key with a different body is detected (`422`), which catches client bugs.
* Bad, because every quote costs one extra write (negligible at this volume).
* Neutral: the hash must be over canonical JSON (sorted keys, no whitespace), or reordered keys cause false `422`s.

### Confirmation

M6 gate: 20 concurrent identical requests with one key → `/__stats` shows `calls == 1` and all 20 bodies are identical.
