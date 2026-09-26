---
status: accepted
date: 2026-09-26
decision-makers: Beacon FDE (P01)
consulted: Meridian security architect (webhook egress)
---

# ADR-P01-3: Deliver webhooks through a transactional outbox in Postgres

## Context and Problem Statement

Success criterion 4: 99% of webhooks reach a healthy subscriber within 60 s; failures retry for 72 h, then land in a dead-letter queue ops can replay. A shipment upsert and "tell the subscribers" must not diverge: no event for a change that rolled back, and no lost event for a change that committed. How are webhooks made reliable?

## Decision Drivers

* No lost events on a crash between the state change and the send
* Long retry horizon (72 h) with backoff, independent of the request path
* Several dispatcher replicas must never send the same attempt twice concurrently
* No new infrastructure at this volume

## Considered Options

* Fire-and-forget from the request or ingest path
* In-process background tasks
* Transactional outbox table polled by a dispatcher worker

## Decision Outcome

Chosen option: "Transactional outbox": the transaction that upserts `shipments` also inserts one `webhook_deliveries` row per matching subscription; a dispatcher claims due rows with `FOR UPDATE SKIP LOCKED`, signs per Standard Webhooks, and retries with full jitter from 30 s to a 6 h cap. The other options lose events on crash and cannot hold a 72-hour retry schedule.

### Consequences

* Good, because the event exists if and only if the state change committed.
* Good, because `SKIP LOCKED` lets replicas share the work without a broker.
* Bad, because delivery is at-least-once: subscribers must deduplicate on `webhook-id` (documented in the contract).
* Bad, because polling adds up to one poll interval of latency (well inside the 60 s SLI).
* Neutral: Kafka or a queue becomes worth it at a much higher volume or when many consumers need the same stream.

### Confirmation

M7 gate: 100% `signature=valid` at the sink; stopping the sink shows growing jittered gaps; with `WEBHOOK_MAX_AGE=120s` the delivery dead-letters, and a replay delivers it.
