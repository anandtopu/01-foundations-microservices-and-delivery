# Runbook: SFTP host key mismatch

**Symptom:** the `sftp-poller` logs `Host key is not trusted` (asyncssh `HostKeyNotVerifiable`) on every cycle. No files are ingested, and `IngestStale` pages after 30 minutes. `/readyz` may still say `"sftp":"ok"`: its probe only reads the SSH banner and never checks the key.

**The key could have changed because** Meridian rebuilt or re-keyed the DMZ SFTP host. **Or** someone is intercepting the connection. From the gateway you cannot tell these apart, which is exactly why the key is pinned.

## 1. Never disable verification

Not "just for now", and not with `known_hosts=None`, `StrictHostKeyChecking=no` or an empty `known_hosts`. Doing so would hand shipment data, and our key's authentication, to whoever answered.

## 2. Collect what the server now presents (read-only)

```bash
ssh-keyscan -t ed25519 -p 22 <meridian-sftp-host> 2>/dev/null | ssh-keygen -lf -
```

In the lab:

```bash
ssh-keyscan -t ed25519 -p 2222 localhost 2>/dev/null | ssh-keygen -lf -
```

Write down the SHA256 fingerprint. **Do not install it yet:** it came over the same channel you no longer trust.

## 3. Confirm through a second channel

- Contact Meridian's security architect (not the person who reported the outage), and ask whether the host was rebuilt or re-keyed, and when.
- Get the new fingerprint from them **out of band**: a call where you read it back, or a signed ticket. Compare it character by character with step 2.
- **If they did not change anything, or the fingerprints differ, stop.** Treat it as a security incident: keep the poller failing and escalate to our security on-call.

## 4. Update the pin (only after step 3 matches)

The pin lives in `secrets/known_hosts` under the name the workers connect to (`sftp` in the lab; the production DNS name in production), and the poller mounts it read-only.

In the lab, the spec's command reads the key from inside the container, which is the trusted path there:

```bash
docker compose exec -T sftp sh -c 'echo "sftp $(cut -d" " -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"' > secrets/known_hosts
```

```bash
ssh-keygen -lf secrets/known_hosts
```

In production: update the secret in the secret store (never a file on a laptop), then roll the poller so it re-reads it:

```bash
docker compose up -d --force-recreate sftp-poller
```

## 5. Verify

```bash
docker compose logs sftp-poller --since 5m | grep -E "file=|not trusted" | tail
```

Look for the next cycle listing the drop. Then `gateway_ingest_lag_seconds_count` should increase and `gateway_ingest_rows_total` should grow.

## 6. Afterwards

Record who confirmed the fingerprint, through which channel, and when. Ask Meridian to announce future re-keys in advance, with the new fingerprint, so this becomes a planned change rather than an outage.
