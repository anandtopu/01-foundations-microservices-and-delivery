-- 0001_init: the ingest tables (M3). Migrations are additive only (spec section 6, rollback).
-- Applied in one transaction by `python -m gateway.migrate`, which records it in schema_migrations.

-- One row per shipment, owned by exactly one shipper. client_id comes from the CSV's SHIPPER_CODE
-- (decision A) and is the column every API query filters on (section 9, BOLA).
CREATE TABLE shipments (
    shipment_id  text          PRIMARY KEY,
    client_id    text          NOT NULL,
    order_no     text          NOT NULL,
    status       text          NOT NULL
                 CHECK (status IN ('picked', 'loaded', 'in_transit', 'delivered', 'exception')),
    ship_date    date          NOT NULL,
    weight_lb    numeric(12,2) NOT NULL CHECK (weight_lb >= 0),
    source_file  text          NOT NULL,
    created_at   timestamptz   NOT NULL DEFAULT now(),
    updated_at   timestamptz   NOT NULL DEFAULT now()
);

-- Serves GET /v1/shipments: WHERE client_id = $1 ORDER BY (updated_at, shipment_id), cursor-paginated.
CREATE INDEX shipments_client_page ON shipments (client_id, updated_at, shipment_id);

-- One row per distinct file content we have seen. The drop is read-only to us (internal-sftp -R),
-- so we cannot delete or rename a processed file: this table is our memory of what is done.
CREATE TABLE ingested_files (
    file_id       bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_name     text        NOT NULL,
    size_bytes    bigint      NOT NULL,
    sha256        bytea       NOT NULL CHECK (length(sha256) = 32),
    mtime         bigint,                          -- fast path only; never the dedupe key
    status        text        NOT NULL DEFAULT 'in_progress'
                  CHECK (status IN ('in_progress', 'done', 'rejected')),
    -- Checkpoint: the last physical line whose batch committed. Updated in the SAME transaction as
    -- that batch's upserts and dead letters, so a crash resumes exactly after it (exactly-once).
    last_line     integer     NOT NULL DEFAULT 1,  -- line 1 is the header
    rows_ok       integer     NOT NULL DEFAULT 0,
    rows_dead     integer     NOT NULL DEFAULT 0,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    UNIQUE (file_name, size_bytes, sha256)
);

-- Rows (M3) and webhook deliveries (M7) that could not be processed, for ops to inspect and replay.
CREATE TABLE dead_letters (
    dead_letter_id   uuid        PRIMARY KEY DEFAULT uuidv7(),   -- PG 18 built-in, time-ordered
    kind             text        NOT NULL CHECK (kind IN ('row', 'webhook')),
    reason           text        NOT NULL,
    source           jsonb       NOT NULL,   -- row: file_name, line_no, raw
    file_id          bigint      REFERENCES ingested_files (file_id),
    line_no          integer,
    created_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at      timestamptz,
    replay_count     integer     NOT NULL DEFAULT 0,
    last_replayed_at timestamptz,
    CHECK (kind <> 'row' OR (file_id IS NOT NULL AND line_no IS NOT NULL))
);

-- Belt and braces for exactly-once: one dead letter per (file, line), whatever the poller does.
CREATE UNIQUE INDEX dead_letters_row_once ON dead_letters (file_id, line_no) WHERE kind = 'row';
-- The ops list (GET /v1/dead-letters) shows open items newest first.
CREATE INDEX dead_letters_open ON dead_letters (kind, created_at DESC) WHERE resolved_at IS NULL;
