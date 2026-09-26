"""Re-record the SOAP golden fixtures from the running mock.

    uv run python tests/soap/record_fixtures.py

Each fixture is the exact response body; the HTTP status is in the file name (`*.500.xml`), because
SOAP 1.1 sends every fault as HTTP 500 and the tests must replay that faithfully. The success
fixture's QuoteRef is random per call, which is why the tests assert its shape, not its value.
Needs `make mocks`. Not collected by pytest (no test_ prefix).
"""

from pathlib import Path

import httpx

SOAP = "http://localhost:8080"
OUT = Path(__file__).parent / "fixtures"
ENVELOPE = """<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:rq="urn:meridian:ratequote:v2">
  <soapenv:Body><rq:GetRateQuote>
    <rq:OriginZip>{origin}</rq:OriginZip><rq:DestZip>60601</rq:DestZip>
    <rq:WeightLb>1200</rq:WeightLb><rq:ServiceLevel>LTL_STANDARD</rq:ServiceLevel>
  </rq:GetRateQuote></soapenv:Body>
</soapenv:Envelope>"""


def call(client: httpx.Client, origin: str) -> httpx.Response:
    return client.post(
        "/RateQuoteService",
        content=ENVELOPE.format(origin=origin),
        headers={"Content-Type": "text/xml; charset=utf-8"},
    )


def save(name: str, resp: httpx.Response) -> None:
    path = OUT / f"{name}.{resp.status_code}.xml"
    path.write_bytes(resp.content)
    print(f"{path}  ({len(resp.content)} bytes)")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with httpx.Client(base_url=SOAP, timeout=10) as client:
        client.post("/__faults", json={"latency_ms": 0, "busy_rate": 0, "max_concurrency": 5})
        save("success", call(client, "30301"))
        save("client_fault", call(client, "00000"))  # the mock's stable Client-fault trigger
        client.post("/__faults", json={"busy_rate": 1.0})
        try:
            save("server_busy", call(client, "30301"))
        finally:
            # Restore the compose defaults so other tests see a healthy mock.
            client.post("/__faults", json={"latency_ms": 300, "busy_rate": 0})
            client.post("/__reset")


if __name__ == "__main__":
    main()
