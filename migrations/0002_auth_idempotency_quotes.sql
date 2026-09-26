-- 0002: API keys, idempotency keys and stored quotes (M6). Additive only (spec section 6).

-- API keys (spec section 9: "stored as SHA-256 hashes, scoped per shipper, rotated with overlap;
-- ops scope separate"). Only the hash is stored, so a database leak does not leak usable keys.
-- Rotation with overlap = two active rows for one client_id; revoke the old one afterwards.
CREATE TABLE api_keys (
    key_hash    bytea       PRIMARY KEY CHECK (length(key_hash) = 32),
    client_id   text        NOT NULL,
    scope       text        NOT NULL CHECK (scope IN ('shipper', 'ops')),
    label       text        NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

-- The spec's table, verbatim (M6, ADR-P01-2).
CREATE TABLE idempotency_keys (
  client_id     text        NOT NULL,
  key           text        NOT NULL,
  request_hash  bytea       NOT NULL,
  status        text        NOT NULL CHECK (status IN ('in_progress', 'completed')),
  response_code int,
  response_body jsonb,
  locked_until  timestamptz NOT NULL DEFAULT now() + interval '30 seconds',
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, key)
);

-- Quotes, kept 15 minutes for GET /v1/rate-quotes/{quote_id} (FR-4). Every read filters on
-- client_id (section 9, BOLA): another shipper's quote is simply "not found".
CREATE TABLE rate_quotes (
    quote_id    uuid        PRIMARY KEY,
    client_id   text        NOT NULL,
    body        jsonb       NOT NULL,   -- the exact 201 body, so GET returns the same document
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);
CREATE INDEX rate_quotes_client ON rate_quotes (client_id, quote_id);
