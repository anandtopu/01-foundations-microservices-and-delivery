"""M5: bulkhead, full-jitter retry and circuit breaker, one state at a time. No network.

The breaker reads time.monotonic(); a fake clock makes "30 s later" instant and deterministic.
"""

import asyncio
import time
from collections.abc import Callable
from types import SimpleNamespace

import httpx
import pytest

from gateway import resilience
from gateway.api.rate_quotes import QuoteService, RateQuoteRequest
from gateway.resilience import (
    Bulkhead,
    BulkheadFull,
    CircuitBreaker,
    CircuitOpenError,
    RetryableError,
    State,
    retry_after_seconds,
    retry_full_jitter,
)
from gateway.soap.client import UpstreamRejected


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    # Replace the `time` NAME inside gateway.resilience only. Patching time.monotonic itself would
    # also freeze asyncio's event-loop clock (it reads the same function) and hang every sleep.
    c = Clock()
    monkeypatch.setattr(resilience, "time", SimpleNamespace(monotonic=c))
    return c


async def ok() -> str:
    return "ok"


async def busy() -> str:
    raise RetryableError("Server.Busy")


async def rejected() -> str:
    raise UpstreamRejected("soapenv:Client: bad zip")


async def fail(breaker: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        with pytest.raises(RetryableError):
            await breaker.call(busy)


# --- circuit breaker ---------------------------------------------------------------------------


async def test_closed_passes_calls_through(clock: Clock) -> None:
    breaker = CircuitBreaker()
    assert await breaker.call(ok) == "ok"
    assert (breaker.state, breaker.failures) == (State.CLOSED, 0)


async def test_opens_after_threshold_failures(clock: Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=5)
    await fail(breaker, 4)
    assert breaker.state is State.CLOSED
    await fail(breaker, 1)
    assert breaker.state is State.OPEN


async def test_success_resets_the_failure_count(clock: Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=5)
    await fail(breaker, 4)
    await breaker.call(ok)
    await fail(breaker, 4)
    assert (breaker.state, breaker.failures) == (State.CLOSED, 4)


async def test_non_retryable_errors_do_not_count(clock: Clock) -> None:
    # A Client fault is our request's fault, not a sick upstream: it must never open the breaker.
    breaker = CircuitBreaker(failure_threshold=2)
    for _ in range(5):
        with pytest.raises(UpstreamRejected):
            await breaker.call(rejected)
    assert (breaker.state, breaker.failures) == (State.CLOSED, 0)


async def test_open_fails_fast_without_calling(clock: Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_after=30)
    await fail(breaker, 1)
    calls = 0

    async def counted() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    clock.now += 10
    with pytest.raises(CircuitOpenError) as info:
        await breaker.call(counted)
    assert calls == 0
    assert info.value.retry_after == pytest.approx(20)  # Retry-After counts down to the trial


async def test_half_open_success_closes(clock: Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_after=30)
    await fail(breaker, 1)
    clock.now += 30
    assert await breaker.call(ok) == "ok"
    assert (breaker.state, breaker.failures) == (State.CLOSED, 0)


async def test_half_open_failure_reopens_at_once(clock: Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=5, reset_after=30)
    await fail(breaker, 5)
    clock.now += 30
    await fail(breaker, 1)  # one failed trial is enough, not another five
    assert breaker.state is State.OPEN
    assert breaker.opened_at == clock.now  # a fresh 30 s window


async def test_half_open_admits_one_trial_at_a_time(clock: Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_after=30)
    await fail(breaker, 1)
    clock.now += 30
    gate = asyncio.Event()

    async def slow_trial() -> str:
        await gate.wait()
        return "ok"

    trial = asyncio.create_task(breaker.call(slow_trial))
    await asyncio.sleep(0)  # let the trial start
    with pytest.raises(CircuitOpenError, match="trial in flight"):
        await breaker.call(ok)
    gate.set()
    assert await trial == "ok"
    assert breaker.state is State.CLOSED


async def test_cancelled_trial_does_not_wedge_half_open(clock: Clock) -> None:
    """The spec's `except BaseException` branch: a client disconnect cancels the trial call. Without
    resetting _trial, every later call would see 'trial in flight' forever."""
    breaker = CircuitBreaker(failure_threshold=1, reset_after=30)
    await fail(breaker, 1)
    clock.now += 30

    async def hangs() -> str:
        await asyncio.Event().wait()
        return "never"

    trial = asyncio.create_task(breaker.call(hangs))
    await asyncio.sleep(0)
    assert breaker.state is State.HALF_OPEN
    trial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trial
    assert await breaker.call(ok) == "ok"  # a new trial is admitted, and succeeds
    assert breaker.state is State.CLOSED


# --- full-jitter retry -------------------------------------------------------------------------


async def test_retry_recovers_from_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    ceilings: list[float] = []

    def uniform(low: float, high: float) -> float:
        ceilings.append(high)
        return 0.0  # no real sleeping

    monkeypatch.setattr(resilience.random, "uniform", uniform)
    outcomes = iter([RetryableError("busy"), RetryableError("busy"), "ok"])

    async def flaky() -> str:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert await retry_full_jitter(flaky, attempts=3, base=0.2, cap=2.0) == "ok"
    assert ceilings == [0.2, 0.4]  # full jitter draws from [0, min(cap, base * 2^attempt)]


async def test_retry_gives_up_after_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resilience.random, "uniform", lambda _a, _b: 0.0)
    calls = 0

    async def always_busy() -> str:
        nonlocal calls
        calls += 1
        raise RetryableError("busy")

    with pytest.raises(RetryableError):
        await retry_full_jitter(always_busy, attempts=3)
    assert calls == 3


async def test_retry_never_retries_other_errors() -> None:
    calls = 0

    async def wrong() -> str:
        nonlocal calls
        calls += 1
        raise UpstreamRejected("Client fault")

    with pytest.raises(UpstreamRejected):
        await retry_full_jitter(wrong, attempts=3)
    assert calls == 1


async def test_retry_stops_when_the_next_sleep_would_pass_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resilience.random, "uniform", lambda _a, high: high)
    calls = 0

    async def always_busy() -> str:
        nonlocal calls
        calls += 1
        raise RetryableError("busy")

    start = time.perf_counter()
    with pytest.raises(RetryableError):
        await retry_full_jitter(always_busy, attempts=10, base=0.2, cap=2.0, deadline=0.5)
    # sleeps 0.2 (t=0.2), then 0.4 would end at 0.6 > 0.5: give up after 2 calls, not 10
    assert calls == 2
    assert time.perf_counter() - start < 0.45


@pytest.mark.parametrize("seed", range(5))
async def test_jitter_really_is_random(seed: int) -> None:
    # Many clients retrying at the same instant must spread out, not stampede together.
    resilience.random.seed(seed)
    delays = {resilience.random.uniform(0, min(2.0, 0.2 * 2**1)) for _ in range(50)}
    assert len(delays) == 50
    assert all(0 <= d <= 0.4 for d in delays)


async def test_open_breaker_ends_the_retries_immediately(clock: Clock) -> None:
    """Composition (spec M5): the breaker is INSIDE the retry loop, so CircuitOpenError (not a
    RetryableError) ends the loop instead of spending the remaining attempts."""
    breaker = CircuitBreaker(failure_threshold=1, reset_after=30)
    calls = 0

    async def counted_busy() -> str:
        nonlocal calls
        calls += 1
        raise RetryableError("busy")

    with pytest.raises(CircuitOpenError):
        await retry_full_jitter(lambda: breaker.call(counted_busy), attempts=3, base=0.001)
    assert calls == 1  # attempt 1 opened the breaker; attempt 2 was refused without a call


# --- bulkhead ------------------------------------------------------------------------------------


async def test_bulkhead_caps_concurrency_and_rejects_the_overflow() -> None:
    bulkhead = Bulkhead(size=4, acquire_timeout=0.05)
    release = asyncio.Event()
    peak = 0

    async def hold() -> None:
        nonlocal peak
        async with bulkhead.slot():
            peak = max(peak, bulkhead.in_flight)
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(4)]
    await asyncio.sleep(0.01)
    with pytest.raises(BulkheadFull, match="all 4"):
        async with bulkhead.slot():
            pass
    release.set()
    await asyncio.gather(*holders)
    assert (peak, bulkhead.in_flight) == (4, 0)


async def test_bulkhead_waits_briefly_for_a_slot() -> None:
    bulkhead = Bulkhead(size=1, acquire_timeout=0.5)

    async def short() -> None:
        async with bulkhead.slot():
            await asyncio.sleep(0.05)

    first = asyncio.create_task(short())
    await asyncio.sleep(0.01)
    async with bulkhead.slot():  # frees up within the 0.5 s wait: no BulkheadFull
        pass
    await first


@pytest.mark.parametrize("error", [RuntimeError("boom"), asyncio.CancelledError()])
async def test_bulkhead_releases_the_slot_on_error_and_cancel(error: BaseException) -> None:
    bulkhead = Bulkhead(size=1, acquire_timeout=0.05)
    with pytest.raises(type(error)):
        async with bulkhead.slot():
            raise error
    async with bulkhead.slot():  # the slot is free again
        assert bulkhead.in_flight == 1


@pytest.mark.parametrize(("seconds", "header"), [(0, 1), (0.2, 1), (1.0, 1), (1.01, 2), (29.5, 30)])
def test_retry_after_is_whole_seconds_rounded_up(seconds: float, header: int) -> None:
    assert retry_after_seconds(seconds) == header


# --- the composed call path (QuoteService) ------------------------------------------------------

Q = RateQuoteRequest(origin_zip="30301", dest_zip="60601", weight_lb=1200, service_level="FTL")
BUSY = (
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Body>'
    "<soapenv:Fault><faultcode>soapenv:Server.Busy</faultcode><faultstring>busy</faultstring>"
    "</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
)


def service(handler: Callable[[httpx.Request], object], **kw: object) -> QuoteService:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return QuoteService(
        client=httpx.AsyncClient(base_url="http://soap.test", transport=transport),
        bulkhead=kw.pop("bulkhead", Bulkhead(4, 0.2)),  # type: ignore[arg-type]
        breaker=kw.pop("breaker", CircuitBreaker(5, 30)),  # type: ignore[arg-type]
    )


async def test_spec_gate_logic_breaker_opens_on_the_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The M5 gate, in miniature: busy_rate 1.0 -> call 1 spends 3 attempts, call 2 hits the 5th
    failure and opens the breaker, call 3 fails fast without touching the upstream."""
    monkeypatch.setattr(resilience.random, "uniform", lambda _a, _b: 0.0)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text=BUSY)

    svc = service(handler)
    with pytest.raises(RetryableError):
        await svc.quote(Q)
    assert (calls, svc.breaker.state) == (3, State.CLOSED)
    with pytest.raises(CircuitOpenError):
        await svc.quote(Q)
    assert (calls, svc.breaker.state) == (5, State.OPEN)

    start = time.perf_counter()
    with pytest.raises(CircuitOpenError):
        await svc.quote(Q)
    assert calls == 5
    assert time.perf_counter() - start < 0.01  # "every later call returns 503 in under 10 ms"


async def test_upstream_never_sees_more_than_4_even_with_retries() -> None:
    """20 concurrent quotes against a slow, busy upstream (so every request retries): never more
    than 4 calls reach it at once; the overflow is shed as BulkheadFull."""
    in_flight = peak = 0

    async def slow(_: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(500, text=BUSY)  # busy, so every request retries

    svc = service(slow, bulkhead=Bulkhead(4, 0.01), breaker=CircuitBreaker(1000, 30))
    results = await asyncio.gather(*(svc.quote(Q) for _ in range(20)), return_exceptions=True)
    assert peak == 4
    assert {type(r) for r in results} == {RetryableError, BulkheadFull}
