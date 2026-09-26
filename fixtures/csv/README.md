# CSV fixtures (IBM i `CPYTOIMPF` shape)

Byte-exact golden files: **Windows-1252**, **CRLF**, `"`-delimited and space-padded character fields,
`CYYMMDD` dates (century digit `1` = 20xx). `.gitattributes` marks them `-text` so git never rewrites them.

| File | Rows | Expected after ingest (M3 gate) |
|---|---|---|
| `SHPSTS_20260924_0915.csv` | header + 5 | 3 shipments; 2 dead letters: `unknown status code 'Q'` (line 5) and `blank key field` (line 6) |

Columns: `SHIPMENT_ID, ORDER_NO, SHIPPER_CODE, STATUS, SHIP_DATE, WEIGHT_LB`.
`SHIPPER_CODE` is **not** in the spec's `ShipmentRow`. It was added in M1/M2 (decision A) so every row
can be attributed to a shipper for the BOLA control in spec section 9.

Drop a file the way the IBM i job does (the CSV first, then the `.done` trigger):

```bash
make drop F=fixtures/csv/SHPSTS_20260924_0915.csv
```
