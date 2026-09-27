# Runbook: dead-letter spike

**Alert:** `DeadLettersGrowing` (ticket), when more than 50 new dead letters arrive in 1 hour:

```text
increase(gateway_ingest_rows_total{outcome="dead_lettered"}[1h])
  + clamp_min(delta(gateway_dead_letters_open{kind="webhook"}[1h]), 0) > 50
```

The first term counts every new row dead letter (whole-file rejections included). Webhook dead letters have no counter, so the second term is the growth of the open webhook backlog, which under-counts if some were replayed in the same hour.

The backlog by kind is `gateway_dead_letters_open{kind="row"|"webhook"}`.

**Row** dead letters are CSV lines the gateway refused. **Webhook** dead letters are deliveries that were still failing after `WEBHOOK_MAX_AGE` (72 h). Both are listed and replayed through the ops API. Every replay attempt is audited, including rejected ones, along with the ops key that made it.

## 1. Group by reason

```bash
curl -s 'localhost:8000/v1/dead-letters?resolved=false&limit=200' -H "X-API-Key: $OPS_KEY" \
  | python3 -c "import json,sys,collections; d=json.load(sys.stdin)['data']; print(collections.Counter((x['kind'], x['reason'].split(':')[0]) for x in d).most_common())"
```

Or in SQL:

```sql
SELECT kind, split_part(reason, ':', 1) AS reason, count(*) FROM dead_letters
 WHERE resolved_at IS NULL GROUP BY 1, 2 ORDER BY 3 DESC;
```

The `WebhookBacklogOld` check (the oldest pending delivery is older than 15 min) has no metric yet (ARCHITECTURE difference 67); until it has one, it is this query:

```sql
SELECT now() - min(created_at) AS oldest_pending FROM webhook_deliveries WHERE status = 'pending';
```

## 2. Decide by reason

| Reason | What happened | Action |
|---|---|---|
| `status: unknown status code 'Q'` (a new code) | The IBM i team added a status code | Agree the mapping with Meridian, add it to the status map (a code change with a test), deploy, then **replay** (step 3) |
| `owner change refused: 'A' -> 'B'` | A file tried to move a shipment to another shipper | **Do not replay.** It's a data or tenancy question for Meridian: which shipper owns it? Only a correction from them fixes it |
| a date or weight parse error | A malformed export | Ask Meridian to re-export; do not hand-edit rows |
| `unexpected header ...`, `not valid Windows-1252 at byte N` or `empty file` (line 1; the file's status is `rejected`) | Bad header or encoding for the whole file | Check the file (cp1252? the header changed?). A schema change needs a gateway change first |
| webhook `max age 72h exceeded (last: HTTP 5xx / ConnectError)` | A shipper's endpoint was down for 3 days | Confirm with the shipper that it's back, then replay |
| webhook `... (last: ssrf: ...)` | The subscription's host now resolves to a private address | **Do not replay.** Treat it as suspicious and contact the shipper |

## 3. Replay (ops key only)

One dead letter:

```bash
curl -s -X POST "localhost:8000/v1/dead-letters/<dead_letter_id>:replay" -H "X-API-Key: $OPS_KEY"
```

The outcomes:
- **Row:** `200 applied`, or `422 replay-rejected` (it still fails, or would change the owner).
- **Webhook:** `202 queued`. It is resolved only when the dispatcher actually delivers it.
- **`409`:** already resolved, or already queued.

Replay a batch by looping over the IDs from step 1 for one reason at a time, and stop at the first unexpected `422`. Replaying an old row line has no staleness check: if a newer file has already moved that shipment on, the replay moves its status back. Check `updated_at` first for large batches.

## 4. Verify

```text
gateway_dead_letters_open                                          # falls as replays resolve
increase(gateway_ingest_rows_total{outcome="dead_lettered"}[15m])  # stops growing
```

```sql
SELECT outcome, ops_key_id, count(*) FROM dead_letter_replays
 WHERE replayed_at > now() - interval '1 hour' GROUP BY 1, 2;
```

The second query is the audit trail: who replayed what.

## 5. Afterwards

A new status code means the contract's `ShipmentStatus` enum may also need the new value. That is a shipper-visible change, so announce it first.
