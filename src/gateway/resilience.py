"""Resilience primitives for the SOAP call path (ADR-P01-1).

M4 needs only the error taxonomy. M5 adds the bulkhead, full-jitter retry and circuit breaker here.
"""


class RetryableError(Exception):
    """A transient upstream failure (Server.Busy, timeout, 502/503/504): safe to retry, because
    GetRateQuote has no side effects."""
