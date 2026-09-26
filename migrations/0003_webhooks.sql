-- 0003: signed webhooks (M7): subscriptions, the delivery outbox, replay audit. Additive only.

-- A shipper's endpoint. The signing secret must be stored (HMAC needs the key itself); it is shown
-- to the shipper once, at creation. Production: encrypt it at rest (KMS / envelope encryption).
CREATE TABLE webhook_subscriptions (
    subscription_id uuid        PRIMARY KEY,
    client_id       text        NOT NULL,
    url             text        NOT NULL CHECK (url LIKE 'https://%'),
    event_types     text[]      NOT NULL CHECK (cardinality(event_types) > 0),
    secret          text        NOT NULL CHECK (secret LIKE 'whsec\_%'),
    status          text        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    disabled_reason text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX webhook_subscriptions_fanout ON webhook_subscriptions (client_id) WHERE status = 'active';

-- The transactional outbox (spec M7): one row per (event, matching subscription), inserted in the
-- SAME transaction as the change that caused it, so an event is never lost and never invented.
-- delivery_id is also the Standard Webhooks `webhook-id`: stable across retries and replays, so a
-- receiver can de-duplicate.
CREATE TABLE webhook_deliveries (
    delivery_id     uuid        PRIMARY KEY DEFAULT uuidv7(),
    subscription_id uuid        NOT NULL REFERENCES webhook_subscriptions ON DELETE CASCADE,
    event_type      text        NOT NULL,
    payload         jsonb       NOT NULL,
    status          text        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'delivered', 'dead', 'cancelled')),
    attempts        integer     NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    window_start    timestamptz NOT NULL DEFAULT now(),  -- WEBHOOK_MAX_AGE counts from here
    last_attempt_at timestamptz,
    last_result     text,                                 -- "HTTP 503", "ConnectError", ...
    delivered_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
-- The dispatcher's claim query: due, pending rows, oldest first.
CREATE INDEX webhook_deliveries_due ON webhook_deliveries (next_attempt_at) WHERE status = 'pending';

-- Webhook dead letters point at their delivery; one OPEN dead letter per delivery.
ALTER TABLE dead_letters ADD COLUMN delivery_id uuid REFERENCES webhook_deliveries ON DELETE SET NULL;
CREATE UNIQUE INDEX dead_letters_webhook_open
    ON dead_letters (delivery_id) WHERE kind = 'webhook' AND resolved_at IS NULL;

-- Section 9 "Repudiation": every replay attempt is audited (ops key's client, dead letter, time,
-- outcome), whether or not it succeeded.
CREATE TABLE dead_letter_replays (
    replay_id      bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dead_letter_id uuid        NOT NULL REFERENCES dead_letters,
    ops_client_id  text        NOT NULL,
    outcome        text        NOT NULL,   -- applied | queued | rejected: <reason> | already_resolved
    replayed_at    timestamptz NOT NULL DEFAULT now()
);
