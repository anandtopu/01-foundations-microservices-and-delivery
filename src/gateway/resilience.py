"""Resilience primitives for the SOAP call path (spec M5, ADR-P01-1).

Composed outside-in by the caller: bulkhead -> retry (full jitter) -> circuit breaker -> per-attempt
timeout. The breaker sits INSIDE the retry loop, so once it opens, CircuitOpenError (not retryable)
ends the retries immediately instead of burning the remaining attempts.

`retry_full_jitter`, `State` and `CircuitBreaker` are the spec's code (line-wrapped) with the
changes marked "lab:" (ARCHITECTURE differences 18, 22, 23; PR #3 review).
`Bulkhead` is written from the spec's prose ("wraps asyncio.Semaphore(4), acquiring via
asyncio.wait_for(..., timeout=0.2), raising BulkheadFull on timeout and releasing in finally").
Lab additions are marked "lab:".
"""

import asyncio
import math
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from enum import Enum


class RetryableError(Exception):
    """A transient upstream failure (Server.Busy, timeout, 5xx): safe to retry, because
    GetRateQuote has no side effects."""


class CircuitOpenError(Exception):
    """The breaker is open (or a half-open trial is in flight): fail fast, do not call upstream."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after  # lab: seconds until a trial call is allowed (Retry-After)


class BulkheadFull(Exception):
    """All slots are busy and none freed up within the acquire timeout."""


async def retry_full_jitter[T](
    op: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base: float = 0.2,
    cap: float = 2.0,
    deadline: float = 4.0,
) -> T:
    if attempts < 1:  # lab: range(0) would fall through to the "unreachable" assertion
        raise ValueError("attempts must be >= 1")
    loop = asyncio.get_running_loop()
    stop_at = loop.time() + deadline
    for attempt in range(attempts):
        try:
            # lab: the deadline bounds every attempt too. The spec only checked it before a sleep,
            # so a late attempt could run its full 3 s past the budget (worst case ~6.2 s holding
            # a bulkhead slot). Hitting the budget mid-attempt counts as one more retryable failure.
            try:
                async with asyncio.timeout_at(stop_at):
                    return await op()
            except TimeoutError as exc:
                raise RetryableError(f"retry budget of {deadline} s used up") from exc
        except RetryableError:
            delay = random.uniform(0, min(cap, base * 2**attempt))  # noqa: S311  "full jitter"
            if attempt == attempts - 1 or loop.time() + delay >= stop_at:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


class State(Enum):
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, reset_after: float = 30.0) -> None:
        self.failure_threshold, self.reset_after = failure_threshold, reset_after
        self.state, self.failures, self.opened_at, self._trial = State.CLOSED, 0, 0.0, False
        # lab: bumped every time the breaker opens. A call remembers the generation it was
        # admitted in; a result from an older generation is stale and must not move the state
        # (PR #3 review: a call admitted while CLOSED that succeeded after the breaker opened
        # used to close it at once, skipping the cool-down and the single half-open trial).
        self._gen = 0

    async def call[T](self, op: Callable[[], Awaitable[T]]) -> T:
        if self.state is State.OPEN:
            if time.monotonic() - self.opened_at < self.reset_after:
                raise CircuitOpenError("rate-quote circuit open", self.retry_after())  # lab: arg
            self.state = State.HALF_OPEN
        trial = False  # lab: only the call that owns the trial may clear the flag
        if self.state is State.HALF_OPEN:
            if self._trial:
                raise CircuitOpenError("half-open trial in flight", 1.0)  # lab: Retry-After 1
            self._trial = trial = True
        gen = self._gen  # lab
        try:
            result = await op()
        except RetryableError:
            if trial:
                self._trial = False
            if gen == self._gen:  # lab: a stale failure neither counts nor pushes back opened_at
                self.failures += 1
                if trial or self.failures >= self.failure_threshold:
                    self._open()
            raise
        except BaseException:  # includes CancelledError: never leave a stuck trial
            if trial:
                self._trial = False
            raise
        if trial:
            self._trial = False
        if gen == self._gen:  # lab: a stale success does not close a breaker opened since
            self.state, self.failures = State.CLOSED, 0
        return result

    def _open(self) -> None:
        self.state, self.opened_at = State.OPEN, time.monotonic()
        self._gen += 1

    def retry_after(self) -> float:
        """lab: seconds until the breaker will admit a trial call (for the Retry-After header)."""
        return max(0.0, self.reset_after - (time.monotonic() - self.opened_at))


class Bulkhead:
    """At most `size` operations in flight; a caller waits up to `acquire_timeout` for a slot."""

    def __init__(self, size: int = 4, acquire_timeout: float = 0.2) -> None:
        self.size, self.acquire_timeout = size, acquire_timeout
        self._sem = asyncio.Semaphore(size)
        self.in_flight = 0  # lab: for tests and the M9 gauge; the semaphore is the real limit

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.acquire_timeout)
        except TimeoutError:
            raise BulkheadFull(f"all {self.size} upstream slots busy") from None
        self.in_flight += 1
        try:
            yield
        finally:
            self.in_flight -= 1
            self._sem.release()


def retry_after_seconds(seconds: float) -> int:
    """lab: Retry-After is whole seconds (RFC 9110 delay-seconds); round up, never 0."""
    return max(1, math.ceil(seconds))
