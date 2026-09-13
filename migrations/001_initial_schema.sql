-- RateRadar initial schema
-- Applied by: python -m rateradar.cli migrate
-- Conventions:
--   * all timestamps are timestamptz, stored UTC
--   * "observed" times are ours; "effective" times come from the bank's payload
--   * nothing is deleted; state changes are recorded

CREATE TABLE IF NOT EXISTS schema_migration (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Who we poll
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brand (
    brand_id            text PRIMARY KEY,              -- CDR dataHolderBrandId
    brand_name          text NOT NULL,
    legal_entity_name   text,
    abn                 text,
    public_base_uri     text NOT NULL,
    industries          text[] NOT NULL DEFAULT '{}',
    -- collection control
    is_enabled          boolean NOT NULL DEFAULT false, -- v1 collects an allowlist
    is_active_in_cdr    boolean NOT NULL DEFAULT true,  -- still listed on the register
    -- health
    consecutive_failures    integer NOT NULL DEFAULT 0,
    circuit_open_until      timestamptz,
    last_success_at         timestamptz,
    negotiated_api_version  integer,
    -- provenance
    discovered_at       timestamptz NOT NULL DEFAULT now(),
    left_register_at    timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS brand_enabled_idx ON brand (is_enabled) WHERE is_enabled;

-- ---------------------------------------------------------------------------
-- Every run, and every brand's outcome within it.
-- This is what lets the dataset say "we didn't look" instead of "no change".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collection_run (
    run_id          bigserial PRIMARY KEY,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    trigger         text NOT NULL,                     -- schedule | manual | backfill
    git_sha         text,                              -- which code produced this run
    status          text NOT NULL DEFAULT 'running',   -- running | completed | failed
    brands_total    integer NOT NULL DEFAULT 0,
    brands_ok       integer NOT NULL DEFAULT 0,
    brands_failed   integer NOT NULL DEFAULT 0,
    products_seen   integer NOT NULL DEFAULT 0,
    snapshots_new   integer NOT NULL DEFAULT 0,
    changes_emitted integer NOT NULL DEFAULT 0,
    quarantined     integer NOT NULL DEFAULT 0,
    notes           text
);

CREATE INDEX IF NOT EXISTS collection_run_started_idx ON collection_run (started_at DESC);

CREATE TABLE IF NOT EXISTS collection_run_brand (
    run_id          bigint NOT NULL REFERENCES collection_run(run_id) ON DELETE CASCADE,
    brand_id        text   NOT NULL REFERENCES brand(brand_id),
    status          text   NOT NULL,                   -- ok | partial | failed | skipped_circuit_open
    api_version     integer,
    products_seen   integer NOT NULL DEFAULT 0,
    snapshots_new   integer NOT NULL DEFAULT 0,
    http_requests   integer NOT NULL DEFAULT 0,
    duration_ms     integer,
    error_kind      text,                              -- timeout | http_5xx | http_4xx | schema | transport
    error_detail    text,
    PRIMARY KEY (run_id, brand_id)
);

CREATE INDEX IF NOT EXISTS crb_brand_idx ON collection_run_brand (brand_id, run_id DESC);

-- ---------------------------------------------------------------------------
-- Immutable, content-addressed product versions.
-- One row per (product, distinct canonical content).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_snapshot (
    snapshot_id     bigserial PRIMARY KEY,
    brand_id        text NOT NULL REFERENCES brand(brand_id),
    product_id      text NOT NULL,                     -- bank-assigned, unique within brand
    content_hash    char(64) NOT NULL,                 -- sha256 of canonical json
    canonical       jsonb NOT NULL,                    -- normalised, diffable
    raw             jsonb,                             -- as received (nullable: dropped on prune)
    api_version     integer,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    first_run_id    bigint REFERENCES collection_run(run_id),
    UNIQUE (brand_id, product_id, content_hash)
);

CREATE INDEX IF NOT EXISTS snapshot_product_idx  ON product_snapshot (brand_id, product_id, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS snapshot_first_seen_idx ON product_snapshot (first_seen_at DESC);

-- ---------------------------------------------------------------------------
-- What is true right now. Upserted every run; cheap to query.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_current (
    brand_id            text NOT NULL REFERENCES brand(brand_id),
    product_id          text NOT NULL,
    snapshot_id         bigint NOT NULL REFERENCES product_snapshot(snapshot_id),
    product_category    text,
    name                text,
    description         text,
    -- denormalised headline figures, for querying without unpacking jsonb
    headline_rate       numeric(9,6),                  -- best deposit or lowest lending rate
    headline_rate_kind  text,                          -- deposit | lending | null
    is_available        boolean NOT NULL DEFAULT true, -- false once withdrawn
    last_changed_at     timestamptz NOT NULL,
    last_seen_at        timestamptz NOT NULL,
    PRIMARY KEY (brand_id, product_id)
);

CREATE INDEX IF NOT EXISTS current_category_idx ON product_current (product_category, headline_rate);
CREATE INDEX IF NOT EXISTS current_available_idx ON product_current (is_available) WHERE is_available;

-- ---------------------------------------------------------------------------
-- The product: an append-only, typed change feed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_change (
    change_id       bigserial PRIMARY KEY,
    brand_id        text NOT NULL REFERENCES brand(brand_id),
    product_id      text NOT NULL,
    change_type     text NOT NULL,                     -- see docs/data-model.md
    field_path      text,                              -- json pointer into canonical form
    old_value       jsonb,
    new_value       jsonb,
    delta_bps       numeric(10,2),                     -- populated for RATE_CHANGED
    from_snapshot_id bigint REFERENCES product_snapshot(snapshot_id),
    to_snapshot_id   bigint REFERENCES product_snapshot(snapshot_id),
    detected_at     timestamptz NOT NULL DEFAULT now(),
    run_id          bigint REFERENCES collection_run(run_id),
    -- the widest window in which the change could actually have occurred
    observed_after  timestamptz NOT NULL,              -- previous successful observation
    observed_before timestamptz NOT NULL               -- this observation
);

CREATE INDEX IF NOT EXISTS change_detected_idx ON product_change (detected_at DESC);
CREATE INDEX IF NOT EXISTS change_product_idx  ON product_change (brand_id, product_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS change_type_idx     ON product_change (change_type, detected_at DESC);

-- ---------------------------------------------------------------------------
-- Payloads we could not parse. Kept, never dropped.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_id   bigserial PRIMARY KEY,
    brand_id        text NOT NULL,
    product_id      text,
    run_id          bigint REFERENCES collection_run(run_id),
    reason          text NOT NULL,                     -- schema_invalid | decode_error | unexpected_shape
    error_detail    text,
    raw             jsonb,
    raw_text        text,                              -- when it wasn't even valid json
    created_at      timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz
);

CREATE INDEX IF NOT EXISTS quarantine_open_idx ON quarantine (created_at DESC) WHERE resolved_at IS NULL;
