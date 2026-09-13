-- A brand run that fetched 32 products and stored none was recorded as
-- "partial" with no reason attached. ADR-0005 says every outcome is recorded;
-- per-product failures were the gap. These columns close it.

ALTER TABLE collection_run_brand
    ADD COLUMN IF NOT EXISTS products_failed      integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS products_quarantined integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN collection_run_brand.products_failed IS
    'Product detail fetches that errored. status=partial means some; status=failed means all.';
COMMENT ON COLUMN collection_run_brand.error_detail IS
    'Brand-level error, or a representative product-level error when status=partial.';
