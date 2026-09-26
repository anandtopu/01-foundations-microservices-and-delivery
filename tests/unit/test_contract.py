"""House rules for contracts/openapi.yaml that a generic linter cannot know (M1).

Redocly checks that the document is valid OpenAPI 3.1. These tests check that it says what our
requirements and ADRs promise, so a reviewer cannot miss a regression.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

CONTRACT = Path(__file__).parents[2] / "contracts" / "openapi.yaml"
METHODS = {"get", "post", "put", "patch", "delete"}
HEALTH = {"/healthz", "/readyz"}

spec: dict[str, Any] = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def resolve(node: dict[str, Any]) -> dict[str, Any]:
    """Follow one local $ref (#/components/...)."""
    while "$ref" in node:
        target: Any = spec
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        node = target
    return node


def operations() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, op)
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if method in METHODS
    ]


OPS = operations()
IDS = [f"{m.upper()} {p}" for p, m, _ in OPS]


def test_is_openapi_31() -> None:
    assert spec["openapi"].startswith("3.1.")  # ADR-P01-4: stay on 3.1, not 3.2


def test_covers_fr3_to_fr7() -> None:
    op_ids = {op["operationId"] for _, _, op in OPS}
    assert {
        "listShipments",
        "getShipment",  # FR-3
        "createRateQuote",
        "getRateQuote",  # FR-4
        "createWebhookSubscription",
        "deleteWebhookSubscription",  # FR-5
        "listDeadLetters",
        "replayDeadLetter",  # FR-7
    } <= op_ids
    # FR-6: every event type we emit is described as an OpenAPI 3.1 webhook
    events = set(resolve(spec["components"]["schemas"]["EventType"])["enum"])
    assert events == set(spec["webhooks"])


@pytest.mark.parametrize(("path", "method", "op"), OPS, ids=IDS)
def test_every_error_is_problem_details(path: str, method: str, op: dict[str, Any]) -> None:
    """FR-8: every 4xx/5xx is RFC 9457 application/problem+json (health probes excepted)."""
    for code, response in op["responses"].items():
        if int(code) < 400 or path in HEALTH:
            continue
        content = resolve(response).get("content", {})
        assert list(content) == ["application/problem+json"], f"{code} is not Problem Details"
        assert content["application/problem+json"]["schema"] == {
            "$ref": "#/components/schemas/Problem"
        }


@pytest.mark.parametrize(("path", "method", "op"), OPS, ids=IDS)
def test_authenticated_ops_document_401_429_500(path: str, method: str, op: dict[str, Any]) -> None:
    if op.get("security") == []:
        return
    assert {"401", "429", "500"} <= set(op["responses"]), (
        "auth, rate-limit and 500 must be documented"
    )


@pytest.mark.parametrize(("path", "method", "op"), OPS, ids=IDS)
def test_503_and_409_carry_retry_after(path: str, method: str, op: dict[str, Any]) -> None:
    """ADR-P01-1: fast 503 + Retry-After; idempotency 409 also tells the client when to retry."""
    for code in ("503", "409", "429"):
        if code in op["responses"] and path not in HEALTH and path.startswith("/v1/rate-quotes"):
            headers = resolve(op["responses"][code]).get("headers", {})
            assert "Retry-After" in headers, f"{code} lacks Retry-After"


def test_rate_quote_requires_idempotency_key() -> None:
    op = spec["paths"]["/v1/rate-quotes"]["post"]
    (param,) = [resolve(p) for p in op["parameters"] if resolve(p)["name"] == "Idempotency-Key"]
    assert param["in"] == "header" and param["required"] is True
    assert (param["schema"]["minLength"], param["schema"]["maxLength"]) == (16, 128)
    assert {"201", "409", "422", "503"} <= set(op["responses"])
    assert "Location" in op["responses"]["201"]["headers"]


def test_page_size_capped_at_200() -> None:
    limit = spec["components"]["parameters"]["Limit"]["schema"]
    assert limit["maximum"] == 200  # FR-3


def test_ops_endpoints_document_403() -> None:
    for path, _method, op in OPS:
        if path.startswith("/v1/dead-letters"):
            assert "403" in op["responses"]  # FR-7: shipper keys are refused


def test_request_bodies_reject_unknown_fields() -> None:
    schemas = spec["components"]["schemas"]
    for name in ("RateQuoteRequest", "WebhookSubscriptionRequest"):
        assert schemas[name]["additionalProperties"] is False, name


def test_patterns_compile_in_python() -> None:
    """Schemathesis generates data from these with Python's re; ECMA-only syntax would break it."""
    for match in re.finditer(r"pattern: '([^']*)'", CONTRACT.read_text(encoding="utf-8")):
        re.compile(match.group(1))
