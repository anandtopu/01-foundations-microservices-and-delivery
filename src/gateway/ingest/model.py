"""IBM i CSV export -> validated rows. Pure functions: no SFTP, no database (spec M3).

Every legacy quirk lives here, in Pydantic `mode="before"` validators: space-padded fields,
CYYMMDD dates, one-letter status codes, Windows-1252 bytes. The rest of the gateway sees only clean,
typed `ShipmentRow`s.

`ShipmentRow` is the spec's code verbatim, plus two lab additions marked below.
"""

import csv
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

STATUS = {"P": "picked", "L": "loaded", "T": "in_transit", "D": "delivered", "X": "exception"}

# The export's header, in order. SHIPPER_CODE is decision A (see fixtures/csv/README.md).
COLUMNS = ("SHIPMENT_ID", "ORDER_NO", "SHIPPER_CODE", "STATUS", "SHIP_DATE", "WEIGHT_LB")
FIELDS = ("shipment_id", "order_no", "shipper_code", "status", "ship_date", "weight_lb")


def parse_cyymmdd(raw: str) -> date:
    """IBM i CYYMMDD: C=0 -> 19xx, C=1 -> 20xx. '1260924' -> 2026-09-24."""
    v = raw.strip().zfill(7)
    return date(1900 + int(v[0]) * 100 + int(v[1:3]), int(v[3:5]), int(v[5:7]))


class ShipmentRow(BaseModel):
    shipment_id: str
    order_no: str
    # --- lab addition (decision A): which shipper owns the row; becomes shipments.client_id ---
    shipper_code: str
    status: Literal["picked", "loaded", "in_transit", "delivered", "exception"]
    ship_date: date
    # (lab: constraints mirror numeric(12,2) CHECK >= 0, so a bad weight dead-letters one row
    #  instead of failing the whole batch's INSERT)
    weight_lb: Decimal = Field(ge=0, max_digits=12, decimal_places=2)

    # (lab: shipper_code added to the spec's list, so a blank owner is rejected like a blank key)
    @field_validator("shipment_id", "order_no", "shipper_code", mode="before")
    @classmethod
    def strip_padding(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("blank key field")
        return v

    @field_validator("status", mode="before")
    @classmethod
    def map_status(cls, v: str) -> str:
        try:
            return STATUS[v.strip().upper()]
        except KeyError:
            raise ValueError(f"unknown status code {v!r}") from None

    @field_validator("ship_date", mode="before")
    @classmethod
    def ibm_date(cls, v: str) -> date:
        return parse_cyymmdd(v)

    # --- lab addition: numeric fields arrive right-aligned (" 845.00") ---
    @field_validator("weight_lb", mode="before")
    @classmethod
    def strip_number(cls, v: str) -> str:
        return v.strip()


@dataclass(frozen=True, slots=True)
class GoodRow:
    line_no: int
    row: ShipmentRow


@dataclass(frozen=True, slots=True)
class DeadRow:
    line_no: int
    raw: str
    reason: str


class FileRejected(Exception):
    """The whole file is unusable (wrong encoding or header); no row can be trusted."""


def reason_of(err: ValidationError) -> str:
    """E.g. "status: unknown status code 'Q'": the field, then the message sans Pydantic prefix."""
    parts = []
    for e in err.errors():
        field = ".".join(str(p) for p in e["loc"]) or "row"
        parts.append(f"{field}: {e['msg'].removeprefix('Value error, ')}")
    return "; ".join(parts)


def decode(raw: bytes) -> str:
    """The IBM i job writes Windows-1252. Bytes cp1252 leaves undefined (0x81, 0x8D, 0x8F, 0x90,
    0x9D) mean the file is not what we think it is, so reject it rather than guess."""
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError as e:
        raise FileRejected(f"not valid Windows-1252 at byte {e.start}") from None


def parse_export(raw: bytes, *, start_after: int = 1) -> Iterator[GoodRow | DeadRow]:
    """Yield one result per data line, in file order. Line numbers are physical lines, header = 1.

    `start_after` skips lines already committed (the resume checkpoint). CPYTOIMPF never emits a
    newline inside a quoted field, so one physical line is one record; that keeps `line_no` and
    `raw` exact for the ops team.
    """
    lines = decode(raw).splitlines()
    if not lines:
        raise FileRejected("empty file")
    header = tuple(h.strip().upper() for h in next(csv.reader([lines[0]])))
    if header != COLUMNS:
        raise FileRejected(f"unexpected header {header!r}; expected {COLUMNS!r}")

    for line_no, line in enumerate(lines[1:], start=2):
        if line_no <= start_after or not line.strip():
            continue
        values = next(csv.reader([line]))
        if len(values) != len(FIELDS):
            yield DeadRow(line_no, line, f"expected {len(FIELDS)} fields, got {len(values)}")
            continue
        try:
            row = ShipmentRow.model_validate(dict(zip(FIELDS, values, strict=True)))
        except ValidationError as err:
            yield DeadRow(line_no, line, reason_of(err))
        else:
            yield GoodRow(line_no, row)
