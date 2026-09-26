"""M3: the IBM i quirks, parsed in one place (gateway.ingest.model). No I/O."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from gateway.ingest.model import (
    DeadRow,
    FileRejected,
    GoodRow,
    ShipmentRow,
    parse_cyymmdd,
    parse_export,
)

CSV = Path(__file__).parents[2] / "fixtures" / "csv"
HEADER = b"SHIPMENT_ID,ORDER_NO,SHIPPER_CODE,STATUS,SHIP_DATE,WEIGHT_LB\r\n"


def one(line: str) -> GoodRow | DeadRow:
    results = list(parse_export(HEADER + line.encode("cp1252") + b"\r\n"))
    assert len(results) == 1
    return results[0]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1260924", date(2026, 9, 24)),  # C=1 -> 20xx
        ("0991231", date(1999, 12, 31)),  # C=0 -> 19xx
        ("991231", date(1999, 12, 31)),  # C=0 with the leading zero suppressed
        (" 1260101 ", date(2026, 1, 1)),  # padding
    ],
)
def test_cyymmdd(raw: str, expected: date) -> None:
    assert parse_cyymmdd(raw) == expected


@pytest.mark.parametrize("raw", ["1261399", "12609", "abcdefg"])
def test_cyymmdd_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_cyymmdd(raw)


def test_row_strips_padding_and_maps_status() -> None:
    row = ShipmentRow.model_validate(
        {
            "shipment_id": " SHP1 ",
            "order_no": "ORD-1 ",
            "shipper_code": "ACME  ",
            "status": "t",
            "ship_date": "1260924",
            "weight_lb": "   845.00",
        }
    )
    assert (row.shipment_id, row.order_no, row.shipper_code) == ("SHP1", "ORD-1", "ACME")
    assert row.status == "in_transit"
    assert row.weight_lb == Decimal("845.00")


def test_golden_sample_gives_3_good_and_2_dead() -> None:
    results = list(parse_export((CSV / "SHPSTS_20260924_0915.csv").read_bytes()))
    good = [r for r in results if isinstance(r, GoodRow)]
    dead = [r for r in results if isinstance(r, DeadRow)]
    assert [g.row.shipment_id for g in good] == ["SHP0000101", "SHP0000102", "SHP0000103"]
    assert [(d.line_no, d.reason) for d in dead] == [
        (5, "status: unknown status code 'Q'"),
        (6, "shipment_id: blank key field"),
    ]
    # The raw row is kept verbatim (padding included) so ops sees exactly what the IBM i sent.
    assert dead[0].raw == '"SHP0000104","ORD-77004 ","BOLT  ","Q",1260924,  310.00'


def test_cp1252_golden_file() -> None:
    raw = (CSV / "SHPSTS_20260925_0730.csv").read_bytes()
    results = list(parse_export(raw))
    assert all(isinstance(r, GoodRow) for r in results)
    rows = [r.row for r in results if isinstance(r, GoodRow)]
    assert [r.order_no for r in rows] == ["CAFÉ-8801", "PEÑA-8802", "£REF-8803", "€QT-8804"]
    assert rows[2].ship_date == date(1999, 12, 31)
    # Why the decode matters: the same bytes are not valid UTF-8, and Latin-1 loses the euro sign.
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    assert "€" not in raw.decode("latin-1")


def test_resume_skips_committed_lines() -> None:
    raw = (CSV / "SHPSTS_20260924_0915.csv").read_bytes()
    assert [r.line_no for r in parse_export(raw, start_after=4)] == [5, 6]


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ('"S1","O1","ACME","P",1260924,-5.00', "weight_lb: Input should be greater than or equal"),
        ('"S1","O1","ACME","P",1260924,1.234', "weight_lb: Decimal input should have no more than"),
        ('"S1","O1","ACME","P",1260924,abc', "weight_lb: Input should be a valid decimal"),
        ('"S1","O1","ACME","P",1261399,1.00', "ship_date: "),
        ('"S1","O1","      ","P",1260924,1.00', "shipper_code: blank key field"),
        ('"S1","O1","ACME","P",1260924', "expected 6 fields, got 5"),
    ],
)
def test_bad_rows_become_dead_letters(line: str, reason: str) -> None:
    result = one(line)
    assert isinstance(result, DeadRow)
    assert result.reason.startswith(reason), result.reason
    assert result.line_no == 2


def test_several_errors_are_all_reported() -> None:
    result = one('"  ","O1","ACME","Q",1260924,1.00')
    assert isinstance(result, DeadRow)
    assert result.reason == "shipment_id: blank key field; status: unknown status code 'Q'"


def test_blank_lines_are_ignored() -> None:
    assert list(parse_export(HEADER + b"\r\n  \r\n")) == []


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "empty file"),
        (b"ID,ORDER,STATUS\r\n", "unexpected header"),
        (HEADER + b'"S1","O\x81","ACME","P",1260924,1.00\r\n', "not valid Windows-1252"),
    ],
)
def test_unusable_files_are_rejected_whole(raw: bytes, message: str) -> None:
    with pytest.raises(FileRejected, match=message):
        list(parse_export(raw))


def test_model_rejects_unknown_status_directly() -> None:
    with pytest.raises(ValidationError, match="unknown status code 'Q'"):
        ShipmentRow.model_validate(
            {
                "shipment_id": "S",
                "order_no": "O",
                "shipper_code": "A",
                "status": "Q",
                "ship_date": "1260924",
                "weight_lb": "1",
            }
        )
