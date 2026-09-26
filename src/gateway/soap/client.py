"""The SOAP 1.1 adapter for Meridian's RateQuoteService (spec M4).

The spec's code, plus lab additions marked "lab:" (ARCHITECTURE.md differences 9-10, 12-16):
- `q` is typed with a Protocol, so this module does not import the API layer;
- `render_xml` escapes quotes too, honours `!r`/format specs, and refuses XML-illegal characters;
- every transport failure (not only timeouts and refused connections) is retryable;
- one total deadline per call (httpx's read timeout restarts on every chunk, so a slow-drip
  response could hold a bulkhead slot for 10 s+), and a response-size cap;
- a response that is not well-formed or uses DTDs/entities (XXE, billion laughs) is rejected;
- classification the M5 breaker can see: `Server*` faults, other 5xx and 429 are retryable;
  only `Client` faults and other 4xx mean "our request was wrong" (`UpstreamRejected`).

Every outcome is exactly one of: a result dict, `UpstreamRejected` (never retried) or
`RetryableError` (the M5 retry loop may try again; the breaker counts it).
"""

import asyncio
import re
from decimal import Decimal
from string.templatelib import Interpolation, Template, convert
from typing import Protocol
from xml.etree.ElementTree import ParseError
from xml.sax.saxutils import escape

import httpx
from defusedxml import DefusedXmlException
from defusedxml import ElementTree as ET

from gateway.resilience import RetryableError

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
RQ_NS = "urn:meridian:ratequote:v2"

DEADLINE_S = 3.0  # lab: total per call; Meridian's p99 is 2.8 s (ADR-P01-1)
MAX_RESPONSE_BYTES = 1 << 20  # lab: a quote response is < 1 KB; 1 MiB is generous, not unbounded

# lab: escape() only handles & < >, which is enough for element text but not for attribute values.
_QUOTES = {'"': "&quot;", "'": "&apos;"}
# lab: characters XML 1.0 forbids outright; escaping cannot make them legal.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


class UpstreamRejected(Exception):
    """SOAP Client fault: our request was wrong. Never retried."""


class QuoteInput(Protocol):  # lab: what the adapter needs; RateQuoteRequest satisfies it
    @property
    def origin_zip(self) -> str: ...
    @property
    def dest_zip(self) -> str: ...
    @property
    def weight_lb(self) -> float: ...
    @property
    def service_level(self) -> str: ...


def render_xml(template: Template) -> str:
    return "".join(_render(part) if isinstance(part, Interpolation) else part for part in template)


def _render(part: Interpolation[object]) -> str:
    # lab: apply !r/!s/!a and the format spec like an f-string would, then escape the result.
    text = format(convert(part.value, part.conversion), part.format_spec)
    if _XML_ILLEGAL.search(text):
        raise ValueError(f"value for {part.expression!r} contains a character XML 1.0 forbids")
    return escape(text, _QUOTES)


def xsd_decimal(value: float) -> str:
    """lab: 1e-05 -> '0.00001'. str(float) can use exponent notation, which is not xs:decimal."""
    return format(Decimal(repr(value)), "f")


def fault_class(code: str) -> str:
    """lab: 'soapenv:Server.Busy' -> 'Server.Busy'. Compare the QName's local part exactly, instead
    of endswith(), which would also match 'evil:NotServer.Busy'."""
    return code.strip().rpartition(":")[2]


async def get_rate_quote(client: httpx.AsyncClient, q: QuoteInput) -> dict[str, str]:
    body = render_xml(t"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="{SOAP_NS}" xmlns:rq="{RQ_NS}">
  <soapenv:Body><rq:GetRateQuote>
    <rq:OriginZip>{q.origin_zip}</rq:OriginZip><rq:DestZip>{q.dest_zip}</rq:DestZip>
    <rq:WeightLb>{xsd_decimal(q.weight_lb)}</rq:WeightLb><rq:ServiceLevel>{q.service_level}</rq:ServiceLevel>
  </rq:GetRateQuote></soapenv:Body>
</soapenv:Envelope>""")
    try:
        async with asyncio.timeout(DEADLINE_S):
            status, content = await _post(client, body)
    except TimeoutError as exc:  # lab: the total deadline, whatever phase was slow
        raise RetryableError(f"no complete response within {DEADLINE_S} s") from exc
    except httpx.TransportError as exc:  # lab: was only TimeoutException and ConnectError
        raise RetryableError(type(exc).__name__) from exc
    if status in (502, 503, 504):
        raise RetryableError(f"http {status}")
    upstream_trouble = status >= 500 or status == 429  # lab: not our request's fault
    try:
        root = ET.fromstring(content, forbid_dtd=True)
    except (ParseError, DefusedXmlException, LookupError, ValueError) as exc:
        # lab: garbage (an HTML error page), hostile XML, or a bogus encoding declaration.
        if upstream_trouble:
            raise RetryableError(f"http {status}, unparseable body") from exc
        raise UpstreamRejected(
            f"unparseable response (http {status}): {type(exc).__name__}"
        ) from exc
    fault = root.find(f".//{{{SOAP_NS}}}Fault")
    if fault is not None:
        # SOAP 1.1 faultcode is unqualified; accept a qualified one too rather than lose the code.
        code = (
            fault.findtext("faultcode") or fault.findtext(f"{{{SOAP_NS}}}faultcode") or ""
        ).strip()
        local = fault_class(code)
        if local == "Server" or local.startswith("Server."):  # includes Server.Busy
            raise RetryableError(code)
        raise UpstreamRejected(f"{code}: {fault.findtext('faultstring')}")
    if upstream_trouble:
        raise RetryableError(f"http {status} without a SOAP fault")
    result = root.find(f".//{{{RQ_NS}}}GetRateQuoteResult")
    if result is None:
        raise UpstreamRejected(f"response has no GetRateQuoteResult (http {status})")
    # lab: rpartition, so an unqualified child element cannot raise IndexError
    return {child.tag.rpartition("}")[2]: (child.text or "").strip() for child in result}


async def _post(client: httpx.AsyncClient, body: str) -> tuple[int, bytes]:
    """POST and read the body, refusing to buffer more than MAX_RESPONSE_BYTES."""
    async with client.stream(
        "POST",
        "/RateQuoteService",
        content=body,
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": '"urn:meridian:ratequote:v2#GetRateQuote"',
        },
        timeout=httpx.Timeout(DEADLINE_S, connect=0.5),
    ) as resp:
        if resp.status_code in (502, 503, 504):
            return resp.status_code, b""  # no need to read an error page we will not parse
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise UpstreamRejected(f"response larger than {MAX_RESPONSE_BYTES} bytes")
            chunks.append(chunk)
        return resp.status_code, b"".join(chunks)
