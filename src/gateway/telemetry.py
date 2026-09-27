"""The spec's section 8 custom metrics (M9).

Only the OpenTelemetry *API* is used here. Without an SDK every instrument is a no-op, so tests and
host runs pay nothing. In the containers, `opentelemetry-instrument` (the distro) configures the SDK
from OTEL_* variables and exports over OTLP to grafana/otel-lgtm, alongside the automatic FastAPI,
httpx and psycopg instrumentation (OTEL_SEMCONV_STABILITY_OPT_IN=http: stable HTTP metric names).

- gateway.upstream.inflight (gauge): the Bulkhead, on every acquire and release
- gateway.circuit.state (gauge, 0/1/2): the CircuitBreaker, on every transition
- gateway.ingest.rows (counter by `outcome`): the poller, once per committed batch
- gateway.ingest.lag (histogram, s): the poller, per file, from the .done mtime to the commit
- gateway.webhook.delivery.age (histogram, s): the dispatcher, per 2xx, from event to delivery
- gateway.dead_letters.open (gauge by `kind`): the dispatcher, refreshed every 30 s
"""

from opentelemetry import metrics

meter = metrics.get_meter("gateway", "1.0.0")

upstream_inflight = meter.create_gauge(
    "gateway.upstream.inflight",
    unit="{request}",
    description="Calls to Meridian's rate service in flight (the bulkhead holds it at <= 4)",
)
circuit_state = meter.create_gauge(
    "gateway.circuit.state",
    description="SOAP circuit breaker: 0 closed, 1 half-open, 2 open",
)
ingest_rows = meter.create_counter(
    "gateway.ingest.rows",
    unit="{row}",
    description="CSV rows by outcome: upserted, dead_lettered, duplicate (unchanged)",
)
ingest_lag = meter.create_histogram(
    "gateway.ingest.lag",
    unit="s",
    description="From the file's .done trigger (its mtime) to the commit of its last batch",
)
webhook_delivery_age = meter.create_histogram(
    "gateway.webhook.delivery.age",
    unit="s",
    description="From the event's outbox row to the receiver's 2xx",
)
dead_letters_open = meter.create_gauge(
    "gateway.dead_letters.open",
    unit="{dead_letter}",
    description="Unresolved dead letters by kind (row, webhook)",
)

CIRCUIT_CODES = {"closed": 0, "half_open": 1, "open": 2}
