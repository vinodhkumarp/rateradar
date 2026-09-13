# ADR-0004: Discover endpoints from the CDR Register, with a fallback source

**Status:** Accepted · 2026-09-12

## Context

Each bank publishes product data at its own base URI. Hardcoding a list means
the dataset silently rots as banks change endpoints, merge, or enter and leave
the regime. The CDR Register exposes a public Data Holder Brands Summary
endpoint listing brands and their public base URIs. A community-maintained list
of the same endpoints also exists and is independently updated.

## Decision

Refresh the `brand` table weekly from the CDR Register. Support a second,
configurable source as a fallback. Collect only from brands explicitly enabled
in the allowlist for v1.

## Rationale

- The register is authoritative and machine-readable; manual lists rot.
- A single discovery dependency is a single point of silent failure, and silent
  is worse than loud here — the failure mode is a dataset that quietly stops
  covering a bank.
- The allowlist keeps v1 to ~20 well-behaved brands while the pipeline
  stabilises, without any code change needed to widen coverage later.
- The allowlist holds bank NAMES, not the register's UUIDs, because it is edited
  by a human. Names are matched case- and punctuation-insensitively, then by
  unique whole-word substring; ids are still accepted and win. Ambiguous or
  unmatched entries are reported and skipped, never guessed at -- silently
  collecting 17 banks when the file asked for 20 is the failure that only
  becomes visible months later, in the data.

## Consequences

- (+) Coverage tracks the real market; expanding to all ~117 holders is a config
  change.
- (+) Brands that leave the register are marked inactive with a date, which is
  itself data worth having.
- (−) Two discovery sources means reconciling disagreements between them. Treated
  as a finding: a diff between sources is logged, not silently resolved.
- The fallback source ships empty by default. A fallback URL that 404s is worse
  than none, because the failure it produces hides the real error behind it --
  as it did the first time discovery failed here. Configure one only after
  verifying it yourself.
