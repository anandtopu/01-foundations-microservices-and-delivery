"""M2: the legacy stand-ins behave like Meridian's systems. Needs `make mocks`; skips otherwise."""

import asyncio
import base64
import hashlib
import hmac
import secrets
import ssl
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

SOAP = "http://localhost:8080"
SINK = "https://localhost:9000"  # M7: HTTPS only, cert from the lab CA (`make certs`)
LAB_CA = Path(__file__).parents[2] / "secrets" / "webhook-ca.crt"
ENVELOPE = """<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:rq="urn:meridian:ratequote:v2">
  <soapenv:Body><rq:GetRateQuote>
    <rq:OriginZip>{origin}</rq:OriginZip><rq:DestZip>60601</rq:DestZip>
    <rq:WeightLb>1200</rq:WeightLb><rq:ServiceLevel>LTL_STANDARD</rq:ServiceLevel>
  </rq:GetRateQuote></soapenv:Body>
</soapenv:Envelope>"""


def reachable(url: str) -> bool:
    try:
        verify = ssl.create_default_context(cafile=LAB_CA) if url.startswith("https") else True
        return httpx.get(f"{url}/__health", timeout=1, verify=verify).status_code == 200
    except httpx.HTTPError, OSError:
        return False


pytestmark = pytest.mark.integration


@pytest.fixture
async def soap() -> AsyncIterator[httpx.AsyncClient]:
    if not reachable(SOAP):
        pytest.skip("soap-mock not running (make mocks)")
    async with httpx.AsyncClient(base_url=SOAP, timeout=10) as client:
        # Restore the fault settings we found, so no test leaves e.g. busy_rate 1.0 behind.
        saved = (await client.get("/__stats")).json()["faults"]
        await client.post(
            "/__faults", json={"latency_ms": 300, "busy_rate": 0, "max_concurrency": 5}
        )
        await client.post("/__reset")
        try:
            yield client
        finally:
            await client.post("/__faults", json=saved)
            await client.post("/__reset")


async def call(client: httpx.AsyncClient, origin: str = "30301") -> httpx.Response:
    return await client.post(
        "/RateQuoteService",
        content=ENVELOPE.format(origin=origin),
        headers={"Content-Type": "text/xml; charset=utf-8"},
    )


async def test_success_returns_quote(soap: httpx.AsyncClient) -> None:
    resp = await call(soap)
    assert resp.status_code == 200
    assert b"<rq:TotalCharge>" in resp.content and b"<rq:QuoteRef>MRQ-" in resp.content


async def test_above_five_concurrent_is_server_busy(soap: httpx.AsyncClient) -> None:
    await soap.post("/__faults", json={"latency_ms": 1000})
    results = await asyncio.gather(*(call(soap) for _ in range(8)))
    codes = sorted(r.status_code for r in results)
    assert codes == [200] * 5 + [500] * 3
    busy = [r for r in results if r.status_code == 500]
    assert all(b"<faultcode>soapenv:Server.Busy</faultcode>" in r.content for r in busy)
    stats = (await soap.get("/__stats")).json()
    # Counted on arrival. Rejections return in ~1 ms, so rejected calls barely overlap each
    # other and the peak is not 8. What IS guaranteed: the 6th call overlaps the 5 slow ones,
    # so any caller that exceeds the limit shows a peak of at least 6. That is why
    # "peak <= 4" is a sound M5 gate.
    assert stats["peak_concurrency"] >= 6
    assert (stats["ok"], stats["busy_faults"], stats["inflight"]) == (5, 3, 0)


async def test_client_fault_is_also_http_500(soap: httpx.AsyncClient) -> None:
    resp = await call(soap, origin="00000")
    assert resp.status_code == 500  # same status as Busy: callers must read the faultcode
    assert b"<faultcode>soapenv:Client</faultcode>" in resp.content


async def test_busy_rate_one_fails_everything(soap: httpx.AsyncClient) -> None:
    await soap.post("/__faults", json={"latency_ms": 0, "busy_rate": 1.0})
    results = [await call(soap) for _ in range(5)]
    assert all(b"Server.Busy" in r.content for r in results)


# --- webhook sink: an independent Standard Webhooks verifier -------------------------------------


def sign(secret: str, msg_id: str, ts: int, body: bytes) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    mac = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


@pytest.fixture
def sink() -> Iterator[httpx.Client]:
    if not reachable(SINK):
        pytest.skip("webhook-sink not running (make mocks)")
    client = httpx.Client(
        base_url=SINK, timeout=5, verify=ssl.create_default_context(cafile=LAB_CA)
    )
    client.post("/__reset")
    yield client
    # Leave the shared lab sink answering 200: a 503 left behind by the outage test made the next
    # live demo's deliveries fail for no visible reason (found re-running the M7 gate).
    client.post("/__reset")
    client.close()


def deliver(
    sink: httpx.Client, secret: str, body: bytes, ts: int, tamper: bool = False
) -> httpx.Response:
    msg_id = "msg_" + secrets.token_hex(8)
    sig = sign(secret, msg_id, ts, body)
    return sink.post(
        "/webhooks/acme",
        content=body + (b" " if tamper else b""),
        headers={"webhook-id": msg_id, "webhook-timestamp": str(ts), "webhook-signature": sig},
    )


def test_sink_accepts_valid_and_rejects_forged(sink: httpx.Client) -> None:
    secret = "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()
    other = "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()
    sink.post("/__secrets", json={"secrets": [secret]})
    body = b'{"type":"shipment.created"}'
    now = int(time.time())
    assert deliver(sink, secret, body, now).status_code == 200
    assert deliver(sink, secret, body, now, tamper=True).status_code == 400
    assert deliver(sink, secret, body, now - 301).status_code == 400  # outside the 5-minute window
    assert deliver(sink, other, body, now).status_code == 400  # wrong key
    got = sink.get("/__received").json()
    assert (got["total"], got["valid"], got["invalid"]) == (4, 1, 3)


def test_sink_mode_simulates_outage(sink: httpx.Client) -> None:
    secret = "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()
    sink.post("/__secrets", json={"secrets": [secret]})
    sink.post("/__mode", json={"status": 503})
    assert deliver(sink, secret, b"{}", int(time.time())).status_code == 503
