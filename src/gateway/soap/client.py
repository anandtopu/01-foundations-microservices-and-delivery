"""The SOAP 1.1 adapter for Meridian's RateQuoteService (spec M4).

The spec's code, verbatim, plus three lab additions marked "lab:":
- `q` is typed with a Protocol, so this module does not import the API layer;
- `render_xml` also escapes quotes, so an interpolation is safe inside an attribute value too;
- a response that is not well-formed, or that uses DTDs/entities (XXE, billion laughs), is mapped to
  `UpstreamRejected` instead of escaping as an unclassified exception (a raw 500 at the API).

Every outcome is exactly one of: a result dict, `UpstreamRejected` (never retried) or
`RetryableError` (the M5 retry loop may try again).
"""

from string.templatelib import Interpolation, Template
from typing import Protocol
from xml.etree.ElementTree import ParseError
from xml.sax.saxutils import escape

import httpx
from defusedxml import DefusedXmlException
from defusedxml import ElementTree as ET

from gateway.resilience import RetryableError

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
RQ_NS = "urn:meridian:ratequote:v2"

# lab: escape() only handles & < >, which is enough for element text but not for attribute values.
_QUOTES = {'"': "&quot;", "'": "&apos;"}


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
    return "".join(
        escape(str(part.value), _QUOTES) if isinstance(part, Interpolation) else part
        for part in template
    )


async def get_rate_quote(client: httpx.AsyncClient, q: QuoteInput) -> dict[str, str]:
    body = render_xml(t"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="{SOAP_NS}" xmlns:rq="{RQ_NS}">
  <soapenv:Body><rq:GetRateQuote>
    <rq:OriginZip>{q.origin_zip}</rq:OriginZip><rq:DestZip>{q.dest_zip}</rq:DestZip>
    <rq:WeightLb>{q.weight_lb}</rq:WeightLb><rq:ServiceLevel>{q.service_level}</rq:ServiceLevel>
  </rq:GetRateQuote></soapenv:Body>
</soapenv:Envelope>""")
    try:
        resp = await client.post(
            "/RateQuoteService",
            content=body,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": '"urn:meridian:ratequote:v2#GetRateQuote"',
            },
            timeout=httpx.Timeout(3.0, connect=0.5),
        )
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        raise RetryableError(type(exc).__name__) from exc
    if resp.status_code in (502, 503, 504):
        raise RetryableError(f"http {resp.status_code}")
    try:
        root = ET.fromstring(resp.content)
    except (ParseError, DefusedXmlException) as exc:  # lab: garbage or hostile XML, never retried
        raise UpstreamRejected(
            f"unparseable response (http {resp.status_code}): {type(exc).__name__}"
        ) from exc
    fault = root.find(f".//{{{SOAP_NS}}}Fault")
    if fault is not None:
        code = (fault.findtext("faultcode") or "").strip()
        if code.endswith("Server.Busy"):
            raise RetryableError(code)
        raise UpstreamRejected(f"{code}: {fault.findtext('faultstring')}")
    result = root.find(f".//{{{RQ_NS}}}GetRateQuoteResult")
    if result is None:
        raise UpstreamRejected("response has no GetRateQuoteResult")
    return {child.tag.split("}")[1]: (child.text or "").strip() for child in result}
