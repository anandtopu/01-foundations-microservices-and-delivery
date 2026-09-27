"""M7: Standard Webhooks signing and the SSRF guard. No network: every URL is an IP literal or
localhost, so getaddrinfo answers locally."""

import importlib.util
import time
from pathlib import Path

import pytest

from gateway.webhooks.signing import sign, verify
from gateway.webhooks.ssrf import assert_public_https, guard

# The published Standard Webhooks test vector (standard-webhooks README / reference libraries):
# an independent check that our signer is the standard, not merely self-consistent.
VECTOR_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"  # noqa: S105 - published test vector
VECTOR_ID, VECTOR_TS, VECTOR_BODY = (
    "msg_p5jXN8AQM9LWM0D4loKWxJek",
    1614265330,
    b'{"test": 2432232314}',
)
VECTOR_SIG = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="

SECRET = (
    "whsec_" + "c2VjcmV0LWtleS1mb3ItdGVzdHMtMDAwMDAwMDA="
)  # base64("secret-key-for-tests-00000000")


def headers(sig: str, ts: int | None = None, msg_id: str = "msg_1") -> dict[str, str]:
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts if ts is not None else int(time.time())),
        "webhook-signature": sig,
    }


def test_sign_matches_the_published_test_vector() -> None:
    assert sign(VECTOR_SECRET, VECTOR_ID, VECTOR_TS, VECTOR_BODY) == VECTOR_SIG


def test_verify_accepts_a_fresh_valid_signature() -> None:
    ts, body = int(time.time()), b'{"type":"shipment.created"}'
    assert verify(SECRET, headers(sign(SECRET, "msg_1", ts, body), ts), body)


@pytest.mark.parametrize(
    ("tamper", "why"),
    [
        (lambda h, b: (h, b + b" "), "one extra byte in the body"),
        (lambda h, b: (h | {"webhook-id": "msg_2"}, b), "another message id"),
        (lambda h, b: (h | {"webhook-timestamp": str(int(h["webhook-timestamp"]) + 1)}, b), "ts+1"),
        (lambda h, b: (h | {"webhook-signature": "v1,AAAA"}, b), "garbage signature"),
        (lambda h, b: (h | {"webhook-signature": "v2," + h["webhook-signature"][3:]}, b), "v2"),
    ],
    ids=["body", "msg_id", "timestamp", "garbage", "unknown_version"],
)
def test_verify_rejects_any_tampering(tamper: object, why: str) -> None:
    ts, body = int(time.time()), b'{"a":1}'
    h, b = tamper(headers(sign(SECRET, "msg_1", ts, body), ts), body)  # type: ignore[operator]
    assert not verify(SECRET, h, b), why


def test_verify_rejects_the_wrong_secret() -> None:
    ts, body = int(time.time()), b"{}"
    other = "whsec_" + "b3RoZXItc2VjcmV0LWtleS0wMDAwMDAwMDAwMDA="
    assert not verify(other, headers(sign(SECRET, "msg_1", ts, body), ts), body)


@pytest.mark.parametrize("skew", [-301, 301], ids=["stale", "future"])
def test_verify_enforces_the_5_minute_replay_window(skew: int) -> None:
    ts, body = int(time.time()) + skew, b"{}"
    assert not verify(SECRET, headers(sign(SECRET, "msg_1", ts, body), ts), body)


def test_rotation_two_signatures_in_one_header() -> None:
    """During a 24 h rotation the gateway signs with old and new secrets; a receiver that knows
    either one must accept the message."""
    new = "whsec_" + "bmV3LXNlY3JldC1rZXktMDAwMDAwMDAwMDAwMDAwMA=="
    ts, body = int(time.time()), b"{}"
    both = f"{sign(SECRET, 'msg_1', ts, body)} {sign(new, 'msg_1', ts, body)}"
    assert verify(SECRET, headers(both, ts), body)
    assert verify(new, headers(both, ts), body)


def test_the_lab_sink_agrees_with_our_signer() -> None:
    """The webhook sink verifies with its OWN implementation (mocks/webhook-sink/app.py), so a bug
    shared by our signer and our verifier cannot hide."""
    path = Path(__file__).parents[2] / "mocks" / "webhook-sink" / "app.py"
    spec = importlib.util.spec_from_file_location("sink_app", path)
    assert spec and spec.loader
    sink = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sink)
    ts, body = int(time.time()), b'{"x":"\xc3\xa9"}'
    sig = sign(SECRET, "msg_9", ts, body)
    assert sink.verify([SECRET], "msg_9", str(ts), body, sig)
    assert not sink.verify([SECRET], "msg_9", str(ts), body + b"!", sig)


# --- SSRF -----------------------------------------------------------------------------------------

REFUSED = [
    "https://127.0.0.1/x",
    "https://10.0.0.5/",
    "https://172.16.3.4/",
    "https://192.168.1.1/",
    "https://169.254.169.254/latest/meta-data",  # cloud metadata
    "https://[::1]/",
    "https://[fc00::1]/",  # IPv6 ULA
    "https://[fe80::1]/",  # IPv6 link-local
    "https://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6
    "https://0.0.0.0/",
    "https://100.64.0.1/",  # carrier-grade NAT
    "https://2130706433/",  # 127.0.0.1 as a decimal integer
    "https://0x7f000001/",  # 127.0.0.1 in hex
    "https://localhost/",
    "https://evil@127.0.0.1/",  # userinfo does not change the host
    "http://93.184.216.34/",  # plain HTTP
    "ftp://93.184.216.34/",
    "https://",
]


@pytest.mark.parametrize("url", REFUSED)
async def test_ssrf_guard_refuses(url: str) -> None:
    with pytest.raises(ValueError, match=r"https|non-public"):
        await assert_public_https(url)


@pytest.mark.parametrize(
    "url", ["https://93.184.216.34/hook", "https://[2606:2800:220:1:248:1893:25c8:1946]/hook"]
)
async def test_ssrf_guard_allows_public_https(url: str) -> None:
    await assert_public_https(url)


async def test_dev_allowance_is_exact_and_still_https_only() -> None:
    await guard("https://localhost:9000/webhooks/acme", {"localhost"})  # the lab sink
    with pytest.raises(ValueError, match="non-public"):
        await guard("https://127.0.0.1:9000/", {"localhost"})  # not the same NAME
    with pytest.raises(ValueError, match="credentials"):  # userinfo is refused outright
        await guard("https://localhost.evil.test@127.0.0.1/", {"localhost"})
    with pytest.raises(ValueError, match="https"):
        await guard("http://localhost:9000/", {"localhost"})
    with pytest.raises(ValueError, match="non-public"):
        await guard("https://localhost/", ())  # no allowance configured: the default


@pytest.mark.parametrize(
    "url",
    [
        "https://[64:ff9b::a9fe:a9fe]/",  # NAT64 of 169.254.169.254
        "https://[64:ff9b::a00:5]/",  # NAT64 of 10.0.0.5
        "https://[64:ff9b:1::a00:5]/",  # local-use NAT64 prefix
        "https://[::127.0.0.1]/",  # IPv4-compatible IPv6
        "https://[::a00:5]/",
    ],
)
async def test_guard_refuses_embedded_private_ipv4(url: str) -> None:
    with pytest.raises(ValueError, match="non-public"):
        await guard(url)


async def test_the_specs_check_alone_misses_nat64() -> None:
    """Why guard() adds a check: Python's is_global says True for the well-known NAT64 prefix, so
    the spec's verbatim check lets metadata-via-NAT64 through (PR #5 review: the API said 201)."""
    await assert_public_https("https://[64:ff9b::a9fe:a9fe]/")


async def test_guard_allows_nat64_of_a_public_address() -> None:
    await guard("https://[64:ff9b::5db8:d822]/hook")  # NAT64 of 93.184.216.34


@pytest.mark.parametrize(
    "url",
    [
        "https://93.184.216.34/hook\x01x",  # httpx refuses it: it crashed the dispatcher
        "https://1.1.1.1\t/",  # urlsplit silently drops the tab
        "https://93.184.216.34/a b",
        "https://93.184.216.34/\n",
        "https://user:pass@93.184.216.34/",  # would be sent as Basic auth
    ],
    ids=["ctrl", "tab", "space", "newline", "userinfo"],
)
async def test_guard_refuses_urls_the_client_would_read_differently(url: str) -> None:
    with pytest.raises(ValueError):
        await guard(url)


@pytest.mark.parametrize(
    ("name", "answers"),
    [
        ("internal.meridian.test", ["10.0.0.5"]),  # the spec's section 7 case
        ("metadata.evil.test", ["169.254.169.254"]),
        ("mixed.evil.test", ["93.184.216.34", "10.0.0.5"]),  # ONE private answer is enough
        ("v6.evil.test", ["fd00::5"]),  # IPv6 ULA
    ],
)
async def test_a_name_that_resolves_inside_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str, answers: list[str]
) -> None:
    """The check is on what the NAME resolves to, not on the URL's text (spec section 7)."""
    import asyncio
    import socket

    async def fake_getaddrinfo(host: str, *_a: object, **_k: object) -> list[object]:
        assert host == name
        fam = {True: socket.AF_INET6, False: socket.AF_INET}
        return [(fam[":" in ip], socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in answers]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="non-public"):
        await guard(f"https://{name}/hook")
