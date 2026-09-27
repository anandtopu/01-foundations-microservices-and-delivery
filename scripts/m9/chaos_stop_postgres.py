# M9 section 7 / section 6 exercise, run from the repo root against the Compose lab.
"""Chaos: stop Postgres for 30 s. Expect 503s (never 500), not-ready, recovery, no restarts."""

import collections
import subprocess
import time
import urllib.error
import urllib.request

from _lab import compose


def code(url, key=True):
    req = urllib.request.Request(url, headers={"X-API-Key": "dev-shipper-key"} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return type(e).__name__


def restarts():
    out = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.Name}} {{.RestartCount}} {{.State.Status}}",
            "meridian-gateway-api-1",
            "meridian-sftp-poller-1",
            "meridian-webhook-dispatcher-1",
        ],
        capture_output=True,
        text=True,
    ).stdout
    return out.strip().replace("/meridian-", "")


print("before:", restarts())
seen = collections.Counter()
t0 = time.monotonic()
try:
    compose("stop", "-t", "5", "postgres")
    print("postgres stopped")
    while time.monotonic() - t0 < 30:
        seen[("readyz", code("http://localhost:8000/readyz", False))] += 1
        seen[("shipments", code("http://localhost:8000/v1/shipments?limit=1"))] += 1
        time.sleep(1)
    print("during the outage:", dict(seen))
finally:  # Ctrl-C or a crash must not leave the lab without its database
    compose("start", "postgres")
t1 = time.monotonic()
print(f"postgres started after {t1 - t0:.0f} s down")
while time.monotonic() - t1 < 90:
    if (
        code("http://localhost:8000/readyz", False) == 200
        and code("http://localhost:8000/v1/shipments?limit=1") == 200
    ):
        print(f"API ready and serving {time.monotonic() - t1:.1f} s after Postgres started")
        break
    time.sleep(0.5)
print("after:", restarts())
