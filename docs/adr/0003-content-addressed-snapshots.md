# ADR-0003: Content-addressed snapshots rather than append-every-fetch

**Status:** Accepted · 2026-09-12

## Context

Four runs a day across ~2,000 products means ~8,000 product documents fetched
daily. Storing each unconditionally is roughly 100 MB/day against a 0.5 GB free
tier, and most of it is byte-identical to the row before it.

## Decision

Normalise each product to a canonical JSON form, hash it (SHA-256), and insert
a snapshot only when the hash differs from the product's latest snapshot.
Unchanged observations advance `last_seen_at` only.

## Rationale

- Storage grows with change, not with time.
- Idempotency comes free: re-running or backfilling cannot duplicate rows or
  emit phantom change events. This matters because a scheduler that occasionally
  double-fires is now harmless.
- Detecting "reverted to a previous state" becomes a hash lookup.

## Consequences

- (+) Sub-linear storage; safe retries; cheap change detection.
- (−) Correctness now depends on the normaliser. A change in canonicalisation
  logic alters every hash, so a normaliser version is recorded and a rebuild
  path (replay from `raw`) is required — hence the decision to retain raw
  payloads despite the size cost.
- (−) A snapshot's `first_seen_at`..`last_seen_at` is an observation window, not
  a validity period. Documented in the data model so analysis does not misread
  it.
