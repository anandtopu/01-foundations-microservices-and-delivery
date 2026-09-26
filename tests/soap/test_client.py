"""M4 gate: the SOAP adapter maps every recorded response to exactly one outcome.

Golden fixtures in fixtures/ were recorded from the mock (record_fixtures.py). The transport is
httpx.MockTransport, so these tests need no running service and never touch the network.
"""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from defusedxml import ElementTree as ET
from pydantic import ValidationError

from gateway.api.rate_quotes import RateQuoteRequest
from gateway.resilience import RetryableError
from gateway.soap.client import RQ_NS, UpstreamRejected, get_rate_quote, render_xml

FIXTURES = Path(__file__).parent / "fixtures"
QUOTE = RateQuoteRequest(
    origin_zip="30301", dest_zip="60601", weight_lb=1200, service_level="LTL_STANDARD"
)


def fixture(name: str) -> httpx.Response:
    """Replay a recorded fixture; its HTTP status is part of the file name."""
    path = next(FIXTURES.glob(f"{name}.*.xml"))
    status = int(path.suffixes[0].lstrip("."))
    return httpx.Response(status, content=path.read_bytes(), headers={"Content-Type": "text/xml"})


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://soap.test", transport=httpx.MockTransport(handler))


def replay(response: httpx.Response) -> httpx.AsyncClient:
    return client_for(lambda _request: response)


# --- the three golden fixtures -------------------------------------------------------------------


async def test_success_maps_to_dict() -> None:
    async with replay(fixture("success")) as client:
        result = await get_rate_quote(client, QUOTE)
    assert set(result) == {"QuoteRef", "TotalCharge", "Currency", "TransitDays"}
    assert result["QuoteRef"].startswith("MRQ-")
    assert (result["TotalCharge"], result["Currency"], result["TransitDays"]) == (
        "974.56",
        "USD",
        "5",
    )


async def test_client_fault_is_upstream_rejected() -> None:
    async with replay(fixture("client_fault")) as client:
        with pytest.raises(UpstreamRejected, match=r"^soapenv:Client: origin ZIP not served$"):
            await get_rate_quote(client, QUOTE)


async def test_server_busy_fault_is_retryable() -> None:
    async with replay(fixture("server_busy")) as client:
        with pytest.raises(RetryableError, match=r"Server\.Busy"):
            await get_rate_quote(client, QUOTE)


def test_busy_and_client_faults_look_identical_over_http() -> None:
    # Why the adapter must read <faultcode>: SOAP 1.1 sends both as HTTP 500.
    assert fixture("client_fault").status_code == fixture("server_busy").status_code == 500


# --- the request we send -------------------------------------------------------------------------


async def test_envelope_headers_and_values() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return fixture("success")

    async with client_for(handler) as client:
        await get_rate_quote(client, QUOTE)
    (request,) = seen
    assert request.url.path == "/RateQuoteService"
    assert request.headers["Content-Type"] == "text/xml; charset=utf-8"
    assert request.headers["SOAPAction"] == '"urn:meridian:ratequote:v2#GetRateQuote"'
    op = ET.fromstring(request.content).find(f".//{{{RQ_NS}}}GetRateQuote")
    assert op is not None
    fields = {child.tag.split("}")[1]: child.text for child in op}
    assert fields == {
        "OriginZip": "30301",
        "DestZip": "60601",
        "WeightLb": "1200.0",
        "ServiceLevel": "LTL_STANDARD",
    }


def test_render_xml_escapes_every_interpolation() -> None:
    evil = "</rq:OriginZip><rq:Admin>1</rq:Admin>&\"'"
    xml = render_xml(t"<a v='{evil}'>{evil}</a>")
    assert xml == (
        "<a v='&lt;/rq:OriginZip&gt;&lt;rq:Admin&gt;1&lt;/rq:Admin&gt;&amp;&quot;&apos;'>"
        "&lt;/rq:OriginZip&gt;&lt;rq:Admin&gt;1&lt;/rq:Admin&gt;&amp;&quot;&apos;</a>"
    )
    root = ET.fromstring(xml)  # still one element: nothing was injected
    assert (root.text, root.get("v"), len(root)) == (evil, evil, 0)


def test_render_xml_leaves_the_literal_parts_alone() -> None:
    assert render_xml(t"<a>{1}</a><b/>") == "<a>1</a><b/>"


# --- validation happens before any XML exists ----------------------------------------------------


@pytest.mark.parametrize("zip_code", ["<x/>", "3030", "303011", "3030a", "٣٠٣٠١", "30301\n"])
async def test_bad_origin_zip_rejected_before_xml(zip_code: str) -> None:
    def must_not_be_called(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"an envelope was sent: {request.content!r}")

    async with client_for(must_not_be_called) as client:
        with pytest.raises(ValidationError, match="origin_zip"):
            # The way the API will call it (M6): validate the body, then build the envelope.
            q = RateQuoteRequest(
                origin_zip=zip_code, dest_zip="60601", weight_lb=1200, service_level="LTL_STANDARD"
            )
            await get_rate_quote(client, q)  # never reached: validation raised first


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"weight_lb": 0}, "weight_lb"),
        ({"weight_lb": 45000.01}, "weight_lb"),
        ({"weight_lb": float("nan")}, "weight_lb"),
        ({"weight_lb": "1200"}, "weight_lb"),  # strict: a string is not a number
        ({"service_level": "OVERNIGHT"}, "service_level"),
        ({"surprise": 1}, "surprise"),  # additionalProperties: false
    ],
)
def test_request_model_matches_the_contract(patch: dict[str, object], field: str) -> None:
    body = {"origin_zip": "30301", "dest_zip": "60601", "weight_lb": 1200}
    body |= {"service_level": "LTL_STANDARD"} | patch
    with pytest.raises(ValidationError, match=field):
        RateQuoteRequest.model_validate(body)


def test_request_model_accepts_json_integers_for_weight() -> None:
    body = '{"origin_zip":"30301","dest_zip":"60601","weight_lb":1200,"service_level":"FTL"}'
    assert RateQuoteRequest.model_validate_json(body).weight_lb == 1200


# --- transport failures --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_gateway_errors_are_retryable(status: int) -> None:
    async with replay(httpx.Response(status, text="<html>bad gateway</html>")) as client:
        with pytest.raises(RetryableError, match=f"http {status}"):
            await get_rate_quote(client, QUOTE)


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")]
)
async def test_timeouts_and_refusals_are_retryable(exc: httpx.HTTPError) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    async with client_for(handler) as client:
        with pytest.raises(RetryableError, match=type(exc).__name__):
            await get_rate_quote(client, QUOTE)


async def test_response_without_result_is_rejected() -> None:
    empty = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soapenv:Body/></soapenv:Envelope>"
    )
    async with replay(httpx.Response(200, text=empty)) as client:
        with pytest.raises(UpstreamRejected, match="no GetRateQuoteResult"):
            await get_rate_quote(client, QUOTE)


# --- hostile or broken responses (hand-written: a well-behaved mock cannot produce them) --------

XXE = b"""<?xml version="1.0"?>
<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Body>
<rq:GetRateQuoteResult xmlns:rq="urn:meridian:ratequote:v2"><rq:QuoteRef>&xxe;</rq:QuoteRef>
</rq:GetRateQuoteResult></soapenv:Body></soapenv:Envelope>"""

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol">
<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>
<lolz>&lol3;</lolz>"""


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (XXE, "EntitiesForbidden"),
        (BILLION_LAUGHS, "EntitiesForbidden"),
        (b"<html><body>502 from a proxy that lied about its status</body>", "ParseError"),
        (b"", "ParseError"),
    ],
    ids=["xxe", "billion_laughs", "html", "empty"],
)
async def test_hostile_or_malformed_responses_are_rejected(body: bytes, kind: str) -> None:
    async with replay(httpx.Response(200, content=body)) as client:
        with pytest.raises(UpstreamRejected, match=kind):
            await get_rate_quote(client, QUOTE)
