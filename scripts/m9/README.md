# M9 exercises (spec sections 6 and 7)

Run from the repository root, against the running Compose lab (`make up`). Each script prints what it measured; the numbers in `docs/BUILD_LOG.md` (M9) came from these runs.

| Script | Section 7 / 6 row | Command |
|---|---|---|
| `chaos_kill_soap.py` | Chaos: kill `soap-mock`; the breaker opens in < 10 s and re-closes | `python3 scripts/m9/chaos_kill_soap.py` |
| `chaos_stop_postgres.py` | Chaos: stop Postgres 30 s; 503s (never 500), no restarts, recovery | `python3 scripts/m9/chaos_stop_postgres.py` |
| `chaos_kill_poller_midfile.py` | Chaos: SIGKILL the poller mid-way through a 20,000-row file; exactly-once | `uv run python scripts/m9/chaos_kill_poller_midfile.py <path/to/SHPSTS_...csv>` (a 20k-row file from `fixtures/csv/generate.py`, named `SHPSTS_20260927_0200.csv`) |
| `rollback.py` | Section 6 rollback to an older image tag and forward again | `python3 scripts/m9/rollback.py <old-sha> <new-sha>` |

Load: `k6 run load/reads.js` (200 req/s for 5 min) and the quote burst `DURATION=60s k6 run load/quotes.js` with the mock at 2.8 s (`curl -X POST localhost:8080/__faults -d '{"latency_ms":2800}' -H 'Content-Type: application/json'`).
