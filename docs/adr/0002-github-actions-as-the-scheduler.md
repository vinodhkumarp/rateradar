# ADR-0002: GitHub Actions as the scheduler, managed Postgres as the store

**Status:** Accepted · 2026-09-12 · Revisit: after 3 months of operation

## Context

The dataset's value comes from unbroken continuity over months. The operator
has limited time and near-zero budget. Any host that requires patching,
monitoring or payment is a host that eventually stops running.

Options considered: a VPS with cron and Docker Compose; AWS ECS/Lambda with RDS
and Terraform; GitHub Actions with a managed free-tier Postgres; a local
machine.

## Decision

Scheduled GitHub Actions workflow for collection; managed Postgres at **Neon**
(free tier) for storage. Public repository, so Actions minutes are unmetered.

Neon over Supabase specifically: both give ~0.5 GB free, but Neon scales to zero
when idle and wakes in about a second, whereas Supabase pauses a project after a
week of inactivity and needs a manual unpause. A collector that breaks
unnoticed for a week should not also leave the database paused — one failure
should not compound into two. Topology and setup are documented in
`docs/architecture.md` §8.

## Rationale

- Zero cost, zero patching, no machine to run out of disk.
- Built-in run history, logs, retries and failure notifications — telemetry we
  would otherwise have to build.
- The code stays deployment-agnostic: the collector is a CLI invoked with a
  `DATABASE_URL`. Moving it to ECS, Lambda or a VPS later is a workflow change,
  not a rewrite. This is what makes the decision cheap to revisit.

## Consequences

- (+) Highest probability of still running in six months, which is the property
  that actually matters.
- (−) Weaker infrastructure story for a platform-engineering portfolio. Accepted
  for now: the *pipeline* is what this project demonstrates, and Phase 4
  optionally migrates to AWS with Terraform once the dataset has value worth
  protecting.
- (−) GitHub's cron is best-effort and can be delayed under load. Handled by
  making runs idempotent and by detecting gaps in the run ledger rather than
  assuming the schedule held.
- (−) Free-tier Postgres may idle-suspend and can be dropped by the provider.
  Mitigated by weekly logical backups to a second free provider and by the
  restore drill in the runbook.
