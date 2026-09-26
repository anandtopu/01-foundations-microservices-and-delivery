"""M4 against the running SOAP mock: our rendered envelope is accepted, and the live responses
still map the way the recorded fixtures do (so the fixtures cannot silently drift).
Skips without the mock.
"""

from collections.abc import AsyncIterator

import httpx
import pytest

from gateway.api.rate_quotes import RateQuoteRequest
from gateway.resilience import RetryableError
from gateway.soap.client import UpstreamRejected, get_rate_quote

pytestmark = pytest.mark.integration
SOAP = "http://localhost:8080"


def request(origin: str = "30301") -> RateQuoteRequest:
    return RateQuoteRequest(
        origin_zip=origin, dest_zip="60601", weight_lb=1200.5, service_level="LTL_EXPEDITED"
    )


@pytest.fixture
async def mock() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=SOAP) as client:
        try:
            (await client.get("/__health", timeout=1)).raise_for_status()
        except httpx.HTTPError:
            pytest.skip("soap-mock not running (make mocks)")
        # Snapshot whatever fault settings are active and put them back afterwards, so running
        # this gate never clobbers a later gate's setup (e.g. M5's busy_rate 1.0).
        saved = (await client.get("/__stats")).json()["faults"]
        await client.post("/__faults", json={"latency_ms": 0, "busy_rate": 0, "max_concurrency": 5})
        await client.post("/__reset")
        try:
            yield client
        finally:
            await client.post("/__faults", json=saved)
            await client.post("/__reset")


async def test_live_success(mock: httpx.AsyncClient) -> None:
    result = await get_rate_quote(mock, request())
    assert result["Currency"] == "USD"
    assert result["QuoteRef"].startswith("MRQ-")
    stats = (await mock.get("/__stats")).json()
    assert (stats["ok"], stats["client_faults"]) == (1, 0)  # the mock parsed our envelope


async def test_live_client_fault(mock: httpx.AsyncClient) -> None:
    with pytest.raises(UpstreamRejected, match="soapenv:Client"):
        await get_rate_quote(mock, request(origin="00000"))


async def test_live_server_busy(mock: httpx.AsyncClient) -> None:
    await mock.post("/__faults", json={"busy_rate": 1.0})
    with pytest.raises(RetryableError, match=r"Server\.Busy"):
        await get_rate_quote(mock, request())
