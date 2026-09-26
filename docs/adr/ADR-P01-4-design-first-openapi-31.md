---
status: accepted
date: 2026-09-26
decision-makers: Beacon FDE (P01)
consulted: three shipper integration teams
---

# ADR-P01-4: Design-first OpenAPI 3.1 contract, conformance-tested against the running service

## Context and Problem Statement

Three enterprise shippers must build against the gateway in parallel with its development, and success criterion 5 requires the published contract to pass automated conformance tests with zero failures on every build. Does the contract come from the code, or the code from the contract?

## Decision Drivers

* Shippers review and sign off before code exists
* The contract must be a test oracle, not documentation that drifts
* Tooling maturity for the chosen OpenAPI version

## Considered Options

* Code-first (FastAPI-generated `/openapi.json`)
* Design-first `contracts/openapi.yaml`, OpenAPI 3.1
* Design-first on OpenAPI 3.2

## Decision Outcome

Chosen option: "Design-first `contracts/openapi.yaml` on OpenAPI 3.1", linted by Redocly (`recommended` ruleset, per-location exceptions only), guarded by house-rule tests (`tests/unit/test_contract.py`), and conformance-tested by Schemathesis 4.28 against the running API. Code-first lets implementation details leak into the contract and makes review happen after the fact. OpenAPI 3.2 exists, but tooling support is uneven as of September 2026.

### Consequences

* Good, because shippers can generate clients and mock servers on day one.
* Good, because every error status the API can return must be listed; Schemathesis fails the build otherwise.
* Good, because 3.1's top-level `webhooks` section documents the outbound events and signature headers in the same file.
* Bad, because the contract and the FastAPI models are two sources that must agree; Schemathesis is what keeps them honest.
* Neutral: FastAPI's generated schema is still useful as a diff against the contract during development.

### Confirmation

M1 gate: `npx --yes @redocly/cli@2.54.3 lint contracts/openapi.yaml` reports zero errors. M8 gate: `schemathesis run ... --checks all` reports zero failures.
