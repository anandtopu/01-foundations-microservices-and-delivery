"""M3: ingestion against real Postgres (and the live SFTP drop for the end-to-end test).

Uses a throwaway database, gateway_test, created fresh per test, so the lab data is never touched.
Needs `make mocks`; skips otherwise.
"""

import datetime
import importlib.util
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import asyncssh
import pytest
from psycopg import AsyncConnection

from gateway.config import Settings
from gateway.ingest import poller
from gateway.ingest.model import DeadRow, GoodRow, ShipmentRow
from gateway.ingest.poller import RemoteFile, ingest_bytes
from gateway.migrate import load, migrate

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
CSV = ROOT / "fixtures" / "csv"
ADMIN_URL = "postgresql://gateway:gateway@localhost:5432/gateway"
TEST_URL = "postgresql://gateway:gateway@localhost:5432/gateway_test"

_spec = importlib.util.spec_from_file_location("csv_generate", CSV / "generate.py")
assert _spec and _spec.loader
generate_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate_mod)
generate: Callable[..., bytes] = generate_mod.generate


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
async def db() -> AsyncIterator[AsyncConnection]:
    if not port_open(5432):
        pytest.skip("postgres not running (make mocks)")
    async with await AsyncConnection.connect(ADMIN_URL, autocommit=True) as admin:
        await admin.execute("DROP DATABASE IF EXISTS gateway_test WITH (FORCE)")
        await admin.execute("CREATE DATABASE gateway_test")
    await migrate(TEST_URL, load(ROOT / "migrations"))
    async with await AsyncConnection.connect(TEST_URL) as conn:
        yield conn


async def scalar(conn: AsyncConnection, query: str) -> Any:
    cur = await conn.execute(query)
    row = await cur.fetchone()
    await conn.commit()
    return row[0] if row else None


def remote(name: str, raw: bytes, mtime: int = 1) -> RemoteFile:
    return RemoteFile(name, len(raw), mtime)


async def test_golden_sample_3_shipments_2_dead_letters(db: AsyncConnection) -> None:
    raw = (CSV / "SHPSTS_20260924_0915.csv").read_bytes()
    res = await ingest_bytes(db, remote("SHPSTS_20260924_0915.csv", raw), raw, batch_size=1000)
    assert (res.status, res.rows_ok, res.rows_dead) == ("done", 3, 2)
    assert await scalar(db, "SELECT count(*) FROM shipments") == 3
    cur = await db.execute("SELECT line_no, reason, source->>'raw' FROM dead_letters ORDER BY 1")
    dead = await cur.fetchall()
    assert [(n, r) for n, r, _ in dead] == [
        (5, "status: unknown status code 'Q'"),
        (6, "shipment_id: blank key field"),
    ]
    assert dead[0][2].startswith('"SHP0000104"')
    # decision A: every shipment is owned by the shipper named in the file
    cur = await db.execute("SELECT client_id, count(*) FROM shipments GROUP BY 1 ORDER BY 1")
    assert await cur.fetchall() == [("ACME", 2), ("BOLT", 1)]


async def test_redrop_is_skipped_and_creates_no_duplicates(db: AsyncConnection) -> None:
    raw = (CSV / "SHPSTS_20260924_0915.csv").read_bytes()
    await ingest_bytes(db, remote("f.csv", raw, mtime=1), raw, batch_size=1000)
    before = await scalar(db, "SELECT max(updated_at) FROM shipments")

    again = await ingest_bytes(db, remote("f.csv", raw, mtime=2), raw, batch_size=1000)
    assert again.status == "skipped"
    # Same bytes under another name: processed, but the upsert changes nothing.
    other = await ingest_bytes(db, remote("g.csv", raw), raw, batch_size=1000)
    assert (other.status, other.inserted, other.updated) == ("done", [], [])

    assert await scalar(db, "SELECT count(*) FROM shipments") == 3
    assert await scalar(db, "SELECT max(updated_at) FROM shipments") == before
    # The re-drop refreshed mtime, so the next cycle's fast path skips it without a download.
    assert await scalar(db, "SELECT mtime FROM ingested_files WHERE file_name = 'f.csv'") == 2


async def test_only_changed_rows_move_updated_at(db: AsyncConnection) -> None:
    raw = (CSV / "SHPSTS_20260924_0915.csv").read_bytes()
    await ingest_bytes(db, remote("a.csv", raw), raw, batch_size=1000)
    changed = raw.replace(
        b'"SHP0000102","ORD-77002 ","ACME  ","T"', b'"SHP0000102","ORD-77002 ","ACME  ","D"'
    )
    assert changed != raw
    res = await ingest_bytes(db, remote("b.csv", changed), changed, batch_size=1000)
    assert (res.inserted, res.updated) == ([], ["SHP0000102"])
    assert await scalar(db, "SELECT status FROM shipments WHERE shipment_id = 'SHP0000102'") == (
        "delivered"
    )


async def test_cp1252_round_trips_into_postgres(db: AsyncConnection) -> None:
    raw = (CSV / "SHPSTS_20260925_0730.csv").read_bytes()
    await ingest_bytes(db, remote("SHPSTS_20260925_0730.csv", raw), raw, batch_size=1000)
    cur = await db.execute("SELECT order_no FROM shipments ORDER BY shipment_id")
    assert [r[0] for r in await cur.fetchall()] == [
        "CAFÉ-8801",
        "PEÑA-8802",
        "£REF-8803",
        "€QT-8804",
    ]


async def test_rejected_file_is_recorded_once(db: AsyncConnection) -> None:
    raw = b"ID,ORDER,STATUS\r\n1,2,P\r\n"
    res = await ingest_bytes(db, remote("bad.csv", raw), raw, batch_size=1000)
    assert res.status == "rejected"
    again = await ingest_bytes(db, remote("bad.csv", raw), raw, batch_size=1000)
    assert again.status == "skipped"
    assert await scalar(db, "SELECT count(*) FROM dead_letters") == 1
    assert await scalar(db, "SELECT status FROM ingested_files") == "rejected"


HEADER = b"SHIPMENT_ID,ORDER_NO,SHIPPER_CODE,STATUS,SHIP_DATE,WEIGHT_LB\r\n"


def csv(*lines: str) -> bytes:
    return HEADER + "".join(f"{line}\r\n" for line in lines).encode("cp1252")


# --- PR #2 review: poison pills that used to crash the poller on every cycle ---------------------


@pytest.mark.parametrize("second_status", ["P", "L"], ids=["identical", "changed"])
async def test_same_shipment_twice_in_one_batch_last_line_wins(
    db: AsyncConnection, second_status: str
) -> None:
    raw = csv(
        '"S1","O1","ACME","P",1260924,1.00',
        '"S2","O2","ACME","P",1260924,1.00',
        f'"S1","O1","ACME","{second_status}",1260924,1.00',
    )
    res = await ingest_bytes(db, remote("dup.csv", raw), raw, batch_size=1000)
    assert (res.status, res.rows_ok, res.rows_superseded) == ("done", 2, 1)
    status = await scalar(db, "SELECT status FROM shipments WHERE shipment_id = 'S1'")
    assert status == {"P": "picked", "L": "loaded"}[second_status]


async def test_nul_bytes_are_dead_lettered_not_fatal(db: AsyncConnection) -> None:
    raw = csv(
        '"S1","O\x001","ACME","P",1260924,1.00',  # would be a good row, but text cannot hold NUL
        '"S2","O2","ACME","Q",1260924,1.0\x00',  # a dead row whose raw text holds a NUL
        '"S3","O3","ACME","P",1260924,1.00',
    )
    res = await ingest_bytes(db, remote("nul.csv", raw), raw, batch_size=1000)
    assert (res.status, res.rows_ok, res.rows_dead) == ("done", 1, 2)
    cur = await db.execute("SELECT reason, source->>'raw' FROM dead_letters ORDER BY line_no")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["line contains a NUL byte", "line contains a NUL byte"]
    assert "\\x00" in rows[0][1]  # the evidence survives, escaped


async def test_binary_file_is_rejected_not_fatal(db: AsyncConnection) -> None:
    raw = b"\x1f\x8b\x08\x00" + bytes(range(1, 60))  # a gzip header dropped as .csv
    res = await ingest_bytes(db, remote("gz.csv", raw), raw, batch_size=1000)
    assert res.status == "rejected"
    assert await scalar(db, "SELECT status FROM ingested_files") == "rejected"


async def test_owner_change_is_refused(db: AsyncConnection) -> None:
    first = csv('"S1","O1","ACME","P",1260924,1.00')
    await ingest_bytes(db, remote("a.csv", first), first, batch_size=1000)
    takeover = csv('"S1","O1","BOLT","D",1260924,1.00', '"S9","O9","BOLT","P",1260924,1.00')
    res = await ingest_bytes(db, remote("b.csv", takeover), takeover, batch_size=1000)
    assert (res.rows_ok, res.rows_dead, res.inserted) == (1, 1, ["S9"])
    cur = await db.execute("SELECT client_id, status FROM shipments WHERE shipment_id = 'S1'")
    assert await cur.fetchone() == ("ACME", "picked")  # untouched
    reason = await scalar(db, "SELECT reason FROM dead_letters")
    assert reason == "owner change refused: 'ACME' -> 'BOLT'"


async def test_database_refusal_rejects_the_file_and_keeps_earlier_batches(
    db: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safety net: if something the model did not anticipate reaches Postgres and it refuses it
    (DataError), that file is rejected and the poller carries on, keeping committed batches."""
    real = poller.parse_export

    def poisoned(raw: bytes, *, start_after: int = 1) -> Iterator[GoodRow | DeadRow]:
        yield from real(raw, start_after=start_after)
        bad = ShipmentRow.model_construct(
            shipment_id="S\x00", order_no="O", shipper_code="ACME", status="picked",
            ship_date=datetime.date(2026, 9, 24), weight_lb=1,
        )  # fmt: skip
        yield GoodRow(99, bad, "poison")

    monkeypatch.setattr(poller, "parse_export", poisoned)
    raw = csv('"S1","O1","ACME","P",1260924,1.00')
    res = await ingest_bytes(db, remote("p.csv", raw), raw, batch_size=1)
    assert res.status == "rejected"
    assert await scalar(db, "SELECT count(*) FROM shipments") == 1  # batch 1 was committed
    reason = await scalar(db, "SELECT reason FROM dead_letters")
    assert reason == (
        "database refused the batch after line 2: DataError: "
        "PostgreSQL text fields cannot contain NUL (0x00) bytes"
    )


async def test_ingest_refuses_a_connection_inside_a_transaction(db: AsyncConnection) -> None:
    await db.execute("SELECT 1")  # opens an implicit transaction
    raw = csv('"S1","O1","ACME","P",1260924,1.00')
    with pytest.raises(RuntimeError, match="no open transaction"):
        await ingest_bytes(db, remote("t.csv", raw), raw, batch_size=1000)
    await db.rollback()


class Crash(Exception):
    """Stands in for the process dying (OOM kill, docker restart) mid-batch."""


class DiesAfterUpsert:
    """Wraps a connection: runs the shipments upsert, then 'dies' before anything else happens.

    No outer transaction is added, so whatever the code under test already committed stays
    committed and whatever it had not is rolled back, exactly as when a real process is killed.
    """

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def execute(self, query: Any, params: Any = None) -> Any:
        cur = await self._conn.execute(query, params)
        if query == poller.UPSERT:
            raise Crash
        return cur


async def test_crash_mid_batch_resumes_exactly_once(
    db: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = generate(2500, bad_every=50)  # 2,450 good rows, 50 with status 'Q'
    f = remote("big.csv", raw)
    original = poller.commit_batch
    calls = 0

    async def crash_in_third_batch(conn: AsyncConnection, *args: Any) -> None:
        nonlocal calls
        calls += 1
        await original(DiesAfterUpsert(conn) if calls == 3 else conn, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(poller, "commit_batch", crash_in_third_batch)
    with pytest.raises(Crash):
        await ingest_bytes(db, f, raw, batch_size=1000)

    # After the crash: exactly two batches are visible, and the checkpoint agrees with them.
    async with await AsyncConnection.connect(TEST_URL) as fresh:
        cur = await fresh.execute(
            "SELECT status, last_line, rows_ok, rows_dead FROM ingested_files"
        )
        assert await cur.fetchone() == ("in_progress", 2001, 1960, 40)
        assert await scalar(fresh, "SELECT count(*) FROM shipments") == 1960

    # A new process picks the file up again and resumes after line 2001.
    monkeypatch.setattr(poller, "commit_batch", original)
    async with await AsyncConnection.connect(TEST_URL) as fresh:
        res = await ingest_bytes(fresh, f, raw, batch_size=1000)
        assert (res.status, res.rows_ok, res.rows_dead) == ("done", 490, 10)
        assert await scalar(fresh, "SELECT count(*) FROM shipments") == 2450
        assert await scalar(fresh, "SELECT count(*) FROM dead_letters") == 50
        cur = await fresh.execute(
            "SELECT status, last_line, rows_ok, rows_dead FROM ingested_files"
        )
        assert await cur.fetchone() == ("done", 2501, 2450, 50)


async def test_second_poller_skips_its_cycle_while_locked(db: AsyncConnection) -> None:
    async with await AsyncConnection.connect(TEST_URL, autocommit=True) as other:
        await other.execute("SELECT pg_advisory_lock(%s)", (poller.POLLER_LOCK_KEY,))
        # poll_once returns before touching SFTP, so no SFTP settings are needed here.
        assert await poller.poll_once(Settings(database_url=TEST_URL)) == []


def sftp_ready() -> bool:
    return port_open(2222) and (ROOT / "secrets" / "known_hosts").exists()


async def test_end_to_end_through_sftp(db: AsyncConnection, tmp_path: Path) -> None:
    """The real poller cycle: pinned host key, key auth, .done trigger, read-only drop.

    Uses a private subdirectory of the drop: a lab poller (make poll-local) lists only the top
    level, so it can never ingest these test files into the lab database.
    """
    if not sftp_ready():
        pytest.skip("sftp not running or host key not pinned (make mocks pin-hostkey)")
    drop = ROOT / "var" / "sftp-drop" / "_e2e"
    drop.mkdir(parents=True, exist_ok=True)
    name = "SHPSTS_20260101_0000_e2e.csv"
    (drop / name).write_bytes((CSV / "SHPSTS_20260924_0915.csv").read_bytes())
    pending = drop / "SHPSTS_20260101_0001_nodone.csv"  # no .done trigger: must be ignored
    pending.write_bytes((CSV / "SHPSTS_20260925_0730.csv").read_bytes())
    try:
        (drop / f"{name}.done").touch()
        cfg = Settings(
            database_url=TEST_URL,
            sftp_host="localhost",
            sftp_port=2222,
            sftp_host_key_alias="sftp",
            sftp_key_path=ROOT / "secrets" / "gateway_ed25519",
            sftp_known_hosts=ROOT / "secrets" / "known_hosts",
            sftp_remote_dir="/outbound/shipments/_e2e",
        )
        results = {r.file_name: r for r in await poller.poll_once(cfg)}
        assert (results[name].status, results[name].rows_ok, results[name].rows_dead) == (
            "done",
            3,
            2,
        )
        assert pending.name not in results
        again = await poller.poll_once(cfg)
        assert name not in {r.file_name for r in again}  # fast path: no re-download
    finally:
        for p in (drop / name, drop / f"{name}.done", pending):
            p.unlink(missing_ok=True)
        drop.rmdir()


async def test_wrong_host_key_is_refused(db: AsyncConnection, tmp_path: Path) -> None:
    """Section 12: a host-key mismatch must stop the poller, never be ignored."""
    if not sftp_ready():
        pytest.skip("sftp not running or host key not pinned")
    fake = tmp_path / "known_hosts"
    other = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
    fake.write_text(f"sftp {other}")
    cfg = Settings(
        database_url=TEST_URL,
        sftp_host="localhost",
        sftp_port=2222,
        sftp_host_key_alias="sftp",
        sftp_key_path=ROOT / "secrets" / "gateway_ed25519",
        sftp_known_hosts=fake,
    )
    with pytest.raises(asyncssh.HostKeyNotVerifiable):
        await poller.poll_once(cfg)
