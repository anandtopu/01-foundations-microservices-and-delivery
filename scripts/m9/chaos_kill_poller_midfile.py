# M9 section 7 / section 6 exercise, run from the repo root against the Compose lab.
"""Chaos: SIGKILL the poller in the middle of a 20,000-row file, then prove exactly-once.

    uv run python scripts/m9/chaos_kill_poller_midfile.py path/to/SHPSTS_20260927_0200.csv

"Mid-way" is made deterministic: once 3,000 lines are committed, this script takes the shipment
writer's advisory lock, so the poller's NEXT batch blocks inside its open transaction; the poller is
then SIGKILLed with that batch uncommitted (the worst case), the lock released, and the poller
started again. It must resume from its checkpoint and end with every row exactly once.
"""

import pathlib
import subprocess
import sys
import time

import psycopg
from _lab import REPO, compose

DSN = "postgresql://gateway:gateway@localhost:5432/gateway"
WRITER_LOCK = 0x5348_4950  # gateway.ingest.poller.SHIPMENT_WRITER_LOCK
IDS = "shipment_id BETWEEN 'SHP8000001' AND 'SHP8020000'"  # the 20k file's id range


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, cwd=REPO, check=True, capture_output=True, text=True).stdout.strip()


def main(path: str) -> None:
    name = pathlib.Path(path).name
    with (
        psycopg.connect(DSN, autocommit=True) as watch,
        psycopg.connect(DSN, autocommit=True) as blocker,
    ):

        def one(sql: str, *args: object) -> tuple:  # type: ignore[type-arg]
            row = watch.execute(sql, args).fetchone()
            assert row is not None
            return row

        def checkpoint() -> tuple | None:  # type: ignore[type-arg]
            return watch.execute(
                "SELECT status, last_line, rows_ok, rows_dead FROM ingested_files"
                " WHERE file_name = %s",
                (name,),
            ).fetchone()

        sh("make", "-s", "drop", f"F={path}")
        t0 = time.monotonic()
        print(f"dropped {name}")
        while not ((row := checkpoint()) and row[1] >= 3000):
            if time.monotonic() - t0 > 120:
                raise SystemExit("the file never started")
            time.sleep(0.005)
        blocker.execute("SELECT pg_advisory_lock(%s)", (WRITER_LOCK,))  # next batch now blocks
        try:
            time.sleep(0.5)  # let the poller reach the lock inside its next, uncommitted batch
            q = "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            waiting = one(q)
            compose("kill", "sftp-poller")  # SIGKILL, mid-batch
            print(f"SIGKILL poller at checkpoint {checkpoint()}; writers blocked: {waiting[0]}")
        finally:  # whatever happened, release the writers and bring the poller back
            blocker.execute("SELECT pg_advisory_unlock(%s)", (WRITER_LOCK,))
            compose("start", "sftp-poller")
        t1 = time.monotonic()
        while (row := checkpoint()) is None or row[0] != "done":
            if time.monotonic() - t1 > 180:
                raise SystemExit(f"never finished: {row}")
            time.sleep(0.5)
        print(f"done {time.monotonic() - t1:.1f} s after the restart: {row}")
        files = one("SELECT count(*) FROM ingested_files WHERE file_name = %s", name)
        print("ingested_files rows for this name:", files[0])
        print(
            "shipments (total, distinct):",
            one(f"SELECT count(*), count(DISTINCT shipment_id) FROM shipments WHERE {IDS}"),
        )
        dead_sql = (
            "SELECT count(*), count(DISTINCT line_no) FROM dead_letters d"
            " JOIN ingested_files f USING (file_id) WHERE f.file_name = %s"
        )
        print("dead letters (total, distinct lines):", one(dead_sql, name))
        events_sql = (
            "SELECT count(*), count(DISTINCT payload->'data'->>'shipment_id')"
            " FROM webhook_deliveries WHERE event_type = 'shipment.created'"
            " AND payload->'data'->>'shipment_id' BETWEEN 'SHP8000001' AND 'SHP8020000'"
        )
        print("shipment.created deliveries (total, distinct shipments):", one(events_sql))
        print(
            "ACME shipments in the file:",
            one(f"SELECT count(*) FROM shipments WHERE client_id = 'ACME' AND {IDS}")[0],
        )


if __name__ == "__main__":
    main(sys.argv[1])
