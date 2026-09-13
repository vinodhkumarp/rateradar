# ADR-0001: Python for the collector

**Status:** Accepted · 2026-09-12

## Context

The operator's professional depth is Java/Spring Boot. The collector is an
I/O-bound scraper of ~20 HTTP APIs, run on a schedule, whose output feeds later
analytical and ML work. Build time is the scarcest resource.

## Decision

Build the collector in Python (3.12+), with `httpx` for async HTTP, `pydantic`
for validation, and `psycopg` for Postgres.

## Rationale

- The pipeline and any later ML work live in Python's ecosystem; a Java
  collector would force a language boundary exactly where the data flows.
- The whole collector is roughly 500 lines of Python versus a Spring Boot
  application with the same behaviour. In a project constrained by irregular
  evenings, that difference decides whether it ships.
- Java depth is demonstrated later, and more convincingly, by the change-feed
  API and by BlastRadius.

## Consequences

- (+) Fastest path to a running collector; strongest fit with later phases.
- (−) This repo alone under-represents the operator's Java experience. Mitigated
  by Phase 3's Spring Boot API, which is explicitly part of the roadmap.
- (−) Requires discipline that Python projects often lack: typing, linting and
  tests are enforced in CI from the first commit, not added later.
