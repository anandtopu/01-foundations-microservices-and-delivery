# CSV fixtures (IBM i `CPYTOIMPF` shape)

Byte-exact golden files: **Windows-1252**, **CRLF**, `"`-delimited and space-padded character fields,
`CYYMMDD` dates (century digit `1` = 20xx). `.gitattributes` marks them `-text` so git never rewrites them.

| File | Rows | Expected after ingest (M3 gate) |
|---|---|---|
| `SHPSTS_20260924_0915.csv` | header + 5 | 3 shipments; 2 dead letters: `unknown status code 'Q'` (line 5) and `blank key field` (line 6) |
| `SHPSTS_20260925_0730.csv` | header + 4 | 4 shipments, 0 dead letters. The cp1252 golden file: order numbers `CAFÉ-8801`, `PEÑA-8802`, `£REF-8803`, `€QT-8804` (bytes `0xC9`, `0xD1`, `0xA3`, `0x80`). `€` is the byte that proves cp1252, not Latin-1; line 4 has a `C=0` date (`0991231` = 1999-12-31) |

Columns: `SHIPMENT_ID, ORDER_NO, SHIPPER_CODE, STATUS, SHIP_DATE, WEIGHT_LB`.
`SHIPPER_CODE` is **not** in the spec's `ShipmentRow`. It was added in M1/M2 (decision A) so every row
can be attributed to a shipper for the BOLA control in spec section 9.

Drop a file the way the IBM i job does (the CSV first, then the `.done` trigger):

```bash
make drop F=fixtures/csv/SHPSTS_20260924_0915.csv
```
