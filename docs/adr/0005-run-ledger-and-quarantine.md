# ADR-0005: Record every run's outcome; quarantine rather than drop

**Status:** Accepted · 2026-09-12

## Context

Twenty independently operated APIs will fail constantly: timeouts, 500s,
malformed payloads, schema violations. The naive handling — log a warning and
skip — produces a dataset in which "unchanged" and "unobserved" are
indistinguishable, and in which non-conforming banks quietly vanish.

## Decision

1. Every run writes a `collection_run` row, and every brand writes a
   `collection_run_brand` row with its outcome, error kind and timing — success
   or failure.
2. A collection run exits successfully if the infrastructure worked, even when
   individual brands failed. Only database-level or configuration failures fail
   the run.
3. Payloads that fail validation are written to `quarantine` with the raw body
   and the error, leaving the product's last known good state intact.

## Rationale

- Any statistical claim over this data requires knowing where the observation
  gaps are. Without the ledger, the dataset is not defensible.
- Failing a whole run because one bank is down would, in practice, mean losing
  most runs — and on a free scheduler, a failed run is a lost observation.
- Quarantined payloads are evidence. Banks are obliged to conform to the
  standard; a record of where they don't is one of the more interesting things
  this project can produce.

## Consequences

- (+) The dataset can always answer "did we look?" separately from "did it
  change?".
- (+) Operational metrics come from the same tables, with no extra
  instrumentation.
- (−) Green runs are no longer proof of health, so freshness and per-brand
  failure-streak checks are required. They are part of the runbook and run at
  the start of every collection.
