# ADR-0007: Emulate AWS locally with Floci

**Status:** Accepted · 2026-09-14

## Context

Moving collection to Lambda (ADR-0006) opened a gap between what can be tested
locally and what actually runs. The CLI against a local Postgres exercises the
collector, which is most of the value — but it does not exercise the handler,
the Parameter Store lookup, the S3 write, or whether the packaged zip imports
at all on Linux. Those are exactly the things that failed in the first two
deployments.

The alternative is testing in production: push, wait for the workflow, invoke,
read CloudWatch. Minutes per iteration, and every failed attempt is real traffic
to real banks.

## Decision

Use [Floci](https://floci.io/) for local AWS emulation, driven by
`deploy/local_floci.sh` and `make local-up`. Postgres stays in Docker Compose.

- The local function reads its connection string from Parameter Store, exactly
  as the deployed one does. Testing a different code path than production runs
  is how a broken one gets shipped.
- The zip is the same artefact the deploy workflow builds, so a dependency that
  does not import on Linux fails here rather than in Sydney.
- The real Terraform is **not** run against Floci. It would share a state file
  with production, and Terraform would conclude the real function had been
  replaced by an emulated one. The local stack needs four resources; a short
  script is easier to trust than a second state file is to keep honest.

## Consequences

- (+) The deployed shape is testable in seconds, without touching AWS.
- (+) Packaging errors surface locally, where they cost nothing.
- (−) An emulator is not AWS. IAM is not meaningfully evaluated here, so a
  policy that is too narrow still fails only in Sydney — as one already did.
  Fidelity is highest for the parts that are plain API calls (S3, SSM, Lambda
  invocation) and lowest for the parts that are policy.
- (−) One more tool to install, and Docker becomes a hard requirement for the
  full loop. The plain CLI path still works without either.
- (−) Floci is young. If it becomes a maintenance burden, the fallback is the
  CLI plus a slower deploy loop — nothing depends on it, which is the point of
  keeping it to two scripts and five Makefile targets.
