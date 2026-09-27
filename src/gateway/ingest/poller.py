"""sftp-poller: pull IBM i exports from the SFTP drop into Postgres (FR-1, FR-2).

    python -m gateway.ingest.poller          # poll forever, every SFTP_POLL_INTERVAL_S
    python -m gateway.ingest.poller --once   # one cycle, then exit (demo, tests)

Guarantees:
- A CSV is read only after its `.done` trigger exists (the IBM i job writes the CSV first).
- Exactly-once per file content: `ingested_files` is keyed by (name, size, sha256). Each batch of
  INGEST_BATCH_SIZE lines commits its upserts, its dead letters AND the file's `last_line`
  checkpoint in one transaction, so a crash resumes after the last committed batch: no row is
  applied twice and none is skipped.
- Within a batch, the last line for a shipment wins (earlier ones are superseded, not errors).
- A file never moves a shipment to another shipper: an owner change is dead-lettered (BOLA).
- No single file can stop the poller: a file Postgres refuses (DataError) is marked rejected with a
  dead letter, and the loop moves on to the next file.
- One active poller: a Postgres advisory lock per cycle; a second replica skips that cycle.
- The host key is always verified against the pinned `known_hosts` (never `known_hosts=None`).
"""

import argparse
import asyncio
import hashlib
import logging
import posixpath
import time
from dataclasses import dataclass, field
from itertools import islice
from typing import Any

import asyncssh
import psycopg
from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from gateway import telemetry
from gateway.config import Settings, get_settings
from gateway.ingest.model import DeadRow, FileRejected, GoodRow, parse_export
from gateway.webhooks import outbox

log = logging.getLogger("gateway.ingest")

POLLER_LOCK_KEY = 0x5E1D_0002


@dataclass(frozen=True, slots=True)
class RemoteFile:
    name: str
    size: int
    mtime: int | None
    done_mtime: int | None = None  # M9: when the IBM i job wrote the .done trigger (ingest lag)


@dataclass(slots=True)
class IngestResult:
    file_name: str
    status: str  # done | rejected | skipped
    rows_ok: int = 0
    rows_dead: int = 0
    rows_superseded: int = 0  # same shipment_id again later in the same batch; the later line won
    inserted: list[str] = field(default_factory=list)  # new shipment IDs
    updated: list[str] = field(default_factory=list)  # shipment IDs whose data changed
    events: int = 0  # webhook deliveries queued in the same transactions (M7 outbox)


# One statement per batch: arrays in, one row per changed shipment out. The WHERE clause makes a
# re-delivered identical row a no-op, so updated_at (the pagination key) only moves on real change.
# PostgreSQL 18's RETURNING old/new gives the previous status (for shipment.status_changed) and
# tells inserts from updates (old row absent) in the same statement. It replaced M3's
# (xmax = 0) trick, which relied on an undocumented implementation detail.
# Two rules the caller must keep: each shipment_id appears at most once per statement (Postgres
# refuses to update a row twice in one INSERT ... ON CONFLICT), and client_id never changes: it is
# never SET, and the WHERE refuses another owner's row even if the OWNERS check went stale (a new
# shipment inserted by a concurrent writer, e.g. a dead-letter replay racing the poller: PR #5).
UPSERT = """
INSERT INTO shipments AS s
       (shipment_id, client_id, order_no, status, ship_date, weight_lb, source_file, updated_at)
SELECT *, %(file)s, clock_timestamp()
  FROM unnest(%(ids)s::text[], %(clients)s::text[], %(orders)s::text[],
              %(statuses)s::text[], %(dates)s::date[], %(weights)s::numeric[])
ON CONFLICT (shipment_id) DO UPDATE
   SET order_no = EXCLUDED.order_no, status = EXCLUDED.status,
       ship_date = EXCLUDED.ship_date, weight_lb = EXCLUDED.weight_lb,
       source_file = EXCLUDED.source_file, updated_at = clock_timestamp()
 WHERE s.client_id = EXCLUDED.client_id
   AND (s.order_no, s.status, s.ship_date, s.weight_lb)
       IS DISTINCT FROM
       (EXCLUDED.order_no, EXCLUDED.status, EXCLUDED.ship_date, EXCLUDED.weight_lb)
RETURNING new.shipment_id, new.client_id, new.order_no, new.status, new.ship_date,
          new.weight_lb, new.updated_at, old.status AS previous_status,
          (old.shipment_id IS NULL) AS inserted
"""

OWNERS = "SELECT shipment_id, client_id FROM shipments WHERE shipment_id = ANY(%s) FOR UPDATE"

# Every shipment writer (a poller batch, a dead-letter replay) takes this lock for its transaction,
# and stamps updated_at with clock_timestamp() AFTER taking it (PR #6 review, reproduced): with
# now() (the transaction's START) and two concurrent writers, a transaction that started earlier
# but committed later made rows appear BEHIND a reader's cursor, so GET /v1/shipments and every
# updated_since feed skipped them forever. Serialised writers + post-lock timestamps mean a row
# becomes visible only with a timestamp later than everything committed before it.
SHIPMENT_WRITER_LOCK = 0x5348_4950  # "SHIP"

DEAD = """
INSERT INTO dead_letters (kind, reason, source, file_id, line_no)
VALUES ('row', %s, %s, %s, %s)
ON CONFLICT (file_id, line_no) WHERE kind = 'row' DO NOTHING
"""


async def claim_file(conn: AsyncConnection, f: RemoteFile, sha: bytes) -> tuple[int, str, int]:
    """Find or create this content's ingested_files row: (file_id, status, last_line).

    On a conflict we refresh `mtime`: a re-dropped identical file gets a new mtime, and without this
    the fast path in `already_finished` would miss forever and re-download it every cycle.
    (Each conflict still consumes an identity value; gaps in file_id are expected and harmless.)
    """
    async with conn.transaction():
        cur = await conn.execute(
            """INSERT INTO ingested_files (file_name, size_bytes, sha256, mtime)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (file_name, size_bytes, sha256) DO UPDATE SET mtime = EXCLUDED.mtime
               RETURNING file_id, status, last_line""",
            (f.name, f.size, sha, f.mtime),
        )
        row = await cur.fetchone()
    if row is None:  # impossible: INSERT ... ON CONFLICT DO UPDATE always returns the row
        raise RuntimeError(f"ingested_files row for {f.name} vanished")
    return row[0], row[1], row[2]


async def ingest_bytes(
    conn: AsyncConnection, f: RemoteFile, raw: bytes, *, batch_size: int
) -> IngestResult:
    """Ingest one downloaded file. Safe to call again for the same content at any point."""
    # Each batch must really commit on its own. Inside an already-open transaction every
    # conn.transaction() would be a savepoint, and the per-batch checkpoint would silently vanish.
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("ingest_bytes needs a connection with no open transaction")
    sha = hashlib.sha256(raw).digest()
    file_id, status, last_line = await claim_file(conn, f, sha)
    result = IngestResult(f.name, status)
    if status != "in_progress":
        result.status = "skipped"  # the dedupe: same name, size and sha256 already finished
        return result
    if last_line > 1:
        log.info("file=%r resuming after line %d", f.name, last_line)

    try:
        rows = parse_export(raw, start_after=last_line)
        while batch := list(islice(rows, batch_size)):
            await commit_batch(conn, file_id, f.name, batch, result)
            last_line = batch[-1].line_no
    except FileRejected as err:
        return await reject(conn, f, file_id, raw, str(err), result)
    except psycopg.DataError as err:
        # A poison pill the model did not anticipate (Postgres refused a value). Without this the
        # same file would crash the poller on every cycle and block every file after it.
        await conn.rollback()
        # sqlstate is None when psycopg refuses a value client-side (NUL) before sending it.
        code = err.sqlstate or type(err).__name__
        reason = f"database refused the batch after line {last_line}: {code}: {err}"
        return await reject(conn, f, file_id, raw, reason.splitlines()[0], result)

    await conn.execute(
        "UPDATE ingested_files SET status = 'done', finished_at = now() WHERE file_id = %s",
        (file_id,),
    )
    await conn.commit()
    result.status = "done"
    if f.done_mtime is not None:  # M9: the 5-minute freshness SLI, .done written -> committed
        telemetry.ingest_lag.record(max(0.0, time.time() - f.done_mtime))
    return result


async def reject(
    conn: AsyncConnection,
    f: RemoteFile,
    file_id: int,
    raw: bytes,
    reason: str,
    result: IngestResult,
) -> IngestResult:
    """Mark the file rejected with one dead letter at line 1. Batches already committed stay."""
    async with conn.transaction():
        first = raw[:200].decode("cp1252", errors="replace").split("\n", 1)[0].removesuffix("\r")
        await conn.execute(DEAD, (reason, Jsonb(source(f.name, 1, first)), file_id, 1))
        await conn.execute(
            """UPDATE ingested_files SET status = 'rejected', finished_at = now(),
                      rows_dead = rows_dead + 1 WHERE file_id = %s""",
            (file_id,),
        )
    log.warning("file=%r rejected: %s", f.name, reason)
    result.status, result.rows_dead = "rejected", result.rows_dead + 1
    return result


def source(file_name: str, line_no: int, raw: str) -> dict[str, object]:
    # jsonb cannot hold \u0000: keep the evidence visible instead of losing the whole dead letter.
    return {"file_name": file_name, "line_no": line_no, "raw": raw.replace("\x00", "\\x00")}


async def apply_rows(
    conn: AsyncConnection, rows: list[GoodRow], source_file: str
) -> tuple[list[tuple[str, bool]], list[DeadRow], int]:
    """Owner check, upsert and outbox for validated rows, inside the CALLER's transaction.

    Returns (changed [(shipment_id, inserted)], refused rows as dead letters, events queued).
    Shared by the poller and the dead-letter replay (M7), so both follow the same rules.
    """
    # The last line for a shipment wins; a dict keeps one entry per ID (and the UPSERT needs that).
    latest: dict[str, GoodRow] = {}
    for g in rows:
        latest[g.row.shipment_id] = g
    refused: list[DeadRow] = []
    if latest:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SHIPMENT_WRITER_LOCK,))
        # Tenant boundary (section 9): a file may not hand a shipment to another shipper.
        # FOR UPDATE holds these rows until the batch commits, so the check cannot go stale.
        cur = await conn.execute(OWNERS, (list(latest),))
        for shipment_id, owner in await cur.fetchall():
            g = latest[shipment_id]
            if owner != g.row.shipper_code:
                del latest[shipment_id]
                reason = f"owner change refused: {owner!r} -> {g.row.shipper_code!r}"
                refused.append(DeadRow(g.line_no, g.raw, reason))
    good = [g.row for g in latest.values()]
    if not good:
        return [], refused, 0
    cur = await conn.execute(
        UPSERT,
        {
            "file": source_file,
            "ids": [r.shipment_id for r in good],
            "clients": [r.shipper_code for r in good],
            "orders": [r.order_no for r in good],
            "statuses": [r.status for r in good],
            "dates": [r.ship_date for r in good],
            "weights": [r.weight_lb for r in good],
        },
    )
    changed = await cur.fetchall()
    # A row that came back neither inserted nor updated is either unchanged (fine) or, if a
    # concurrent writer inserted it for ANOTHER shipper after our OWNERS check, refused by the
    # UPSERT's WHERE: find those and dead-letter them like any owner change.
    silent = [sid for sid in latest if sid not in {row[0] for row in changed}]
    if silent:
        cur = await conn.execute(
            "SELECT shipment_id, client_id FROM shipments WHERE shipment_id = ANY(%s)", (silent,)
        )
        for shipment_id, owner in await cur.fetchall():
            g = latest[shipment_id]
            if owner != g.row.shipper_code:
                reason = f"owner change refused: {owner!r} -> {g.row.shipper_code!r}"
                refused.append(DeadRow(g.line_no, g.raw, reason))
    events = [e for row in changed if (e := shipment_event(row)) is not None]
    # The outbox insert rides in the same transaction as the upsert (spec M7).
    queued = await outbox.enqueue(conn, events)
    return [(row[0], row[8]) for row in changed], refused, queued


def shipment_event(row: tuple[Any, ...]) -> outbox.Event | None:
    """shipment.created for an insert, shipment.status_changed when the status moved, else none
    (an order_no or weight correction is not an event in the contract)."""
    sid, client, order_no, status, ship_date, weight, updated_at, previous, inserted = row
    if not inserted and status == previous:
        return None
    data = {
        "shipment_id": sid,
        "order_no": order_no,
        "status": status,
        "ship_date": ship_date.isoformat(),
        "weight_lb": f"{weight:f}",  # decimal as a string (contract)
        "updated_at": updated_at.isoformat(),
        "previous_status": None if inserted else previous,
    }
    return outbox.Event(client, "shipment.created" if inserted else "shipment.status_changed", data)


async def commit_batch(
    conn: AsyncConnection,
    file_id: int,
    file_name: str,
    batch: list[GoodRow | DeadRow],
    result: IngestResult,
) -> None:
    dead = [r for r in batch if isinstance(r, DeadRow)]
    goods = [r for r in batch if isinstance(r, GoodRow)]
    superseded = len(goods) - len({g.row.shipment_id for g in goods})
    async with conn.transaction():
        changed, refused, queued = await apply_rows(conn, goods, file_name)
        dead += refused
        for d in dead:
            await conn.execute(
                DEAD, (d.reason, Jsonb(source(file_name, d.line_no, d.raw)), file_id, d.line_no)
            )
        # The checkpoint commits with the rows it describes: that is the exactly-once guarantee.
        rows_ok = len(goods) - superseded - len(refused)
        await conn.execute(
            """UPDATE ingested_files SET last_line = %s, rows_ok = rows_ok + %s,
                      rows_dead = rows_dead + %s WHERE file_id = %s""",
            (batch[-1].line_no, rows_ok, len(dead), file_id),
        )
    # M9 (section 8): the data-quality trend per file.
    telemetry.ingest_rows.add(len(changed), {"outcome": "upserted"})
    telemetry.ingest_rows.add(len(dead), {"outcome": "dead_lettered"})
    telemetry.ingest_rows.add(rows_ok - len(changed) + superseded, {"outcome": "duplicate"})
    result.rows_ok += rows_ok
    result.rows_dead += len(dead)
    result.rows_superseded += superseded
    result.events += queued
    for shipment_id, inserted in changed:
        (result.inserted if inserted else result.updated).append(shipment_id)
    for d in dead:
        log.info("file=%r line=%d dead_letter reason=%r", file_name, d.line_no, d.reason)


async def already_finished(conn: AsyncConnection, f: RemoteFile) -> bool:
    """Fast path: same name, size and mtime as a finished file -> skip without downloading.
    Only a shortcut; the authoritative dedupe is the sha256 in claim_file."""
    if f.mtime is None:
        return False
    cur = await conn.execute(
        """SELECT 1 FROM ingested_files WHERE file_name = %s AND size_bytes = %s AND mtime = %s
              AND status IN ('done', 'rejected') LIMIT 1""",
        (f.name, f.size, f.mtime),
    )
    found = await cur.fetchone() is not None
    await conn.commit()
    return found


async def ready_files(sftp: asyncssh.SFTPClient, remote_dir: str) -> list[RemoteFile]:
    """CSV files whose `.done` trigger exists, oldest name first (the names carry a timestamp)."""
    entries = {text(e.filename): e.attrs for e in await sftp.readdir(remote_dir)}
    return sorted(
        (
            RemoteFile(name, attrs.size or 0, attrs.mtime, entries[f"{name}.done"].mtime)
            for name, attrs in entries.items()
            if name.lower().endswith(".csv") and f"{name}.done" in entries
        ),
        key=lambda f: f.name,
    )


def text(name: str | bytes) -> str:
    # asyncssh types SFTP names as str | bytes; with a str path they are str (UTF-8 on the wire).
    return name if isinstance(name, str) else name.decode("utf-8", errors="replace")


async def poll_once(cfg: Settings) -> list[IngestResult]:
    results: list[IngestResult] = []
    async with await AsyncConnection.connect(cfg.database_url) as conn:
        # The lock lives on the SAME connection that does the work: if that connection dies, the
        # work stops and the lock is released together (session-level lock, survives commits).
        cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (POLLER_LOCK_KEY,))
        got = await cur.fetchone()
        await conn.commit()
        if not got or not got[0]:
            log.info("another poller holds the lock; skipping this cycle")
            return results
        async with (
            asyncssh.connect(
                cfg.sftp_host,
                cfg.sftp_port,
                username=cfg.sftp_user,
                client_keys=[str(cfg.sftp_key_path)],
                known_hosts=str(cfg.sftp_known_hosts),  # pinned; never None (section 12)
                host_key_alias=cfg.sftp_host_key_alias,
                agent_path=None,
                config=[],  # hermetic: ignore ~/.ssh/config on whatever host runs us
            ) as ssh,
            ssh.start_sftp_client() as sftp,
        ):
            for f in await ready_files(sftp, cfg.sftp_remote_dir):
                if await already_finished(conn, f):
                    log.debug("file=%r skipped: name, size and mtime match a finished file", f.name)
                    continue
                path = posixpath.join(cfg.sftp_remote_dir, f.name)
                async with sftp.open(path, "rb") as fh:
                    raw = await fh.read()
                if not isinstance(raw, bytes):  # "rb" always yields bytes; this narrows the type
                    raise TypeError("SFTP read returned text in binary mode")
                if len(raw) != f.size:  # changed between listing and download: next cycle
                    log.warning("file=%r size changed during download; retrying later", f.name)
                    continue
                res = await ingest_bytes(conn, f, raw, batch_size=cfg.ingest_batch_size)
                results.append(res)
                if res.status == "skipped":
                    log.info("file=%r skipped: this content (sha256) was already ingested", f.name)
                else:
                    log.info(
                        "file=%r status=%s rows_ok=%d rows_dead=%d superseded=%d inserted=%d"
                        " updated=%d",
                        f.name, res.status, res.rows_ok, res.rows_dead, res.rows_superseded,
                        len(res.inserted), len(res.updated),
                    )  # fmt: skip
    return results


async def run(cfg: Settings, *, once: bool) -> None:
    log.info(
        "polling sftp://%s@%s:%d%s every %ss",
        cfg.sftp_user, cfg.sftp_host, cfg.sftp_port, cfg.sftp_remote_dir, cfg.sftp_poll_interval_s,
    )  # fmt: skip
    while True:
        try:
            await poll_once(cfg)
        except (OSError, asyncssh.Error, psycopg.OperationalError) as err:
            # SFTP or Postgres unreachable, or a host-key mismatch: log and retry next cycle.
            # Committed batches are safe; the checkpoint resumes the file where it stopped.
            if once:
                raise
            log.error("poll failed: %s: %s", type(err).__name__, err)
        if once:
            return
        await asyncio.sleep(cfg.sftp_poll_interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="run one poll cycle and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    asyncio.run(run(get_settings(), once=args.once))


if __name__ == "__main__":
    main()
