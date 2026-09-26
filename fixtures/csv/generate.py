"""Generate a large IBM i-style export (cp1252, CRLF, padded fields, CYYMMDD dates).

    uv run python fixtures/csv/generate.py 20000 > var/sftp-drop/SHPSTS_20260926_0100.csv

Deterministic for a given (rows, seed, bad_every), so a test can predict exactly which lines are
dead letters: every `bad_every`-th data row gets status 'Q' (unknown status code).
"""

import random
import sys

STATUSES = "PLTDX"
SHIPPERS = ("ACME", "BOLT", "CRUX")


def generate(rows: int, *, seed: int = 7, bad_every: int = 50, id_offset: int = 0) -> bytes:
    rng = random.Random(seed)  # noqa: S311 - test data, not crypto
    out = ["SHIPMENT_ID,ORDER_NO,SHIPPER_CODE,STATUS,SHIP_DATE,WEIGHT_LB"]
    for i in range(1, rows + 1):
        status = "Q" if bad_every and i % bad_every == 0 else rng.choice(STATUSES)
        out.append(
            f'"SHP{id_offset + i:07d}","ORD-{rng.randrange(10**6):06d} ",'
            f'"{rng.choice(SHIPPERS):<6}","{status}",12609{rng.randrange(1, 29):02d},'
            f"{rng.randrange(1, 40000) / 100:>9.2f}"
        )
    return ("\r\n".join(out) + "\r\n").encode("cp1252")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20_000
    sys.stdout.buffer.write(generate(n))
