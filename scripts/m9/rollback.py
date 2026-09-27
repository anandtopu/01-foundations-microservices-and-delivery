# M9 section 7 / section 6 exercise, run from the repo root against the Compose lab.
"""Spec section 6 rollback: GATEWAY_TAG=<older sha> docker compose up -d (the three services)."""

import os
import subprocess
import sys
import threading
import time
import urllib.request

from _lab import REPO


def running():
    out = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.Config.Image}}",
            "meridian-gateway-api-1",
            "meridian-sftp-poller-1",
            "meridian-webhook-dispatcher-1",
        ],
        capture_output=True,
        text=True,
    ).stdout.split()
    return out


def has_pip():
    # A file check, not `python -c "import pip"`: the venv's interpreter cannot see the base image's
    # site-packages, so the import fails even on images that still contain pip (M9: a wrong marker).
    return (
        subprocess.run(
            [
                "docker",
                "exec",
                "meridian-gateway-api-1",
                "test",
                "-d",
                "/usr/local/lib/python3.14/site-packages/pip",
            ],
            capture_output=True,
        ).returncode
        == 0
    )


def probe(stop, tally):
    while not stop.is_set():
        try:
            with urllib.request.urlopen("http://localhost:8000/healthz", timeout=1) as r:
                tally["ok"] += r.status == 200
        except Exception:
            tally["fail"] += 1
        time.sleep(0.1)


def switch(tag):
    stop, tally = threading.Event(), {"ok": 0, "fail": 0}
    t = threading.Thread(target=probe, args=(stop, tally))
    t.start()
    t0 = time.monotonic()
    try:
        # check=True: an image never tagged locally makes `up --no-build` fail while the OLD
        # containers keep answering /readyz, which would read as an instant, successful rollback.
        subprocess.run(
            [
                "docker",
                "compose",
                "up",
                "-d",
                "--no-build",
                "gateway-api",
                "sftp-poller",
                "webhook-dispatcher",
            ],
            cwd=REPO,
            env=os.environ | {"GATEWAY_TAG": tag, "SFTP_POLL_INTERVAL_S": "5"},
            check=True,
            capture_output=True,
        )
        deadline = t0 + 120
        while True:
            if time.monotonic() > deadline:
                raise SystemExit(f"GATEWAY_TAG={tag}: not ready after 120 s")
            try:
                with urllib.request.urlopen("http://localhost:8000/readyz", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                pass
            time.sleep(0.2)
        ready = time.monotonic() - t0
        time.sleep(1)
    finally:
        stop.set()
        t.join()
    images = sorted(set(running()))
    if images != [f"meridian-gateway:{tag}"]:
        raise SystemExit(f"GATEWAY_TAG={tag}: running {images}, not the requested image")
    print(f"GATEWAY_TAG={tag}: ready {ready:.1f} s after `compose up`")
    print(f"  running {images}; pip present: {has_pip()}")
    print(f"  health probes ok/failed during the switch: {tally['ok']}/{tally['fail']}")


if __name__ == "__main__":
    old_tag, new_tag = sys.argv[1], sys.argv[2]  # e.g. 7e01ebd f76b9b2 (tagged by `make image`)
    print("before:", sorted(set(running())), "pip present:", has_pip())
    switch(old_tag)  # roll BACK to the previous release
    switch(new_tag)  # roll FORWARD again
    # The switches ran with SFTP_POLL_INTERVAL_S=5 (the demo value); `make up` restores .env's.
    print("note: the poller now polls every 5 s; run `make up` to return to the .env settings")
