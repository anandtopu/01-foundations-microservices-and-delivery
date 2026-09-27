# M9 section 7 / section 6 exercise, run from the repo root against the Compose lab.
"""Chaos 1: kill soap-mock; the breaker must open in < 10 s and re-close after recovery."""

import json
import time
import urllib.error
import urllib.request
import uuid

from _lab import compose


def quote() -> str:
    body = json.dumps(
        {"origin_zip": "30301", "dest_zip": "60601", "weight_lb": 1200, "service_level": "FTL"}
    ).encode()
    req = urllib.request.Request(
        "http://localhost:8000/v1/rate-quotes",
        body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": "dev-shipper-key",
            "Idempotency-Key": f"chaos-{uuid.uuid4()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return str(r.status)
    except urllib.error.HTTPError as e:
        try:
            kind = json.load(e).get("type", "").rsplit("/", 1)[-1]
        except ValueError:
            kind = "(not JSON)"
        return f"{e.code} {kind}"
    except OSError as e:  # a timeout or refused connection is a result to print, not a crash
        return type(e).__name__


def circuit() -> str:
    with urllib.request.urlopen("http://localhost:8000/readyz", timeout=5) as r:
        return json.load(r)["soap_circuit"]


print("before:", quote(), circuit())
t0 = time.monotonic()
try:
    compose("kill", "soap-mock")
    print("t=0.0 soap-mock killed")
    opened = None
    while time.monotonic() - t0 < 30:
        s = quote()
        t = time.monotonic() - t0
        print(f"t={t:4.1f}s {s}  circuit={circuit()}")
        if s.endswith("circuit-open") and opened is None:
            opened = t
            break
        time.sleep(0.5)
    print(f"BREAKER OPEN after {opened:.1f} s" if opened is not None else "BREAKER NEVER OPENED")
finally:  # never leave the lab without its upstream, whatever happened above
    compose("start", "soap-mock")
t1 = time.monotonic()
print("soap-mock started again")
closed = None
while time.monotonic() - t1 < 90:
    s = quote()
    t = time.monotonic() - t1
    c = circuit()
    if s == "201":
        closed = t
        print(f"t+{t:4.1f}s {s} circuit={c}")
        break
    time.sleep(2)
print(
    f"BREAKER RE-CLOSED {closed:.1f} s after restart" if closed is not None else "NEVER RE-CLOSED"
)
print("after:", circuit())
