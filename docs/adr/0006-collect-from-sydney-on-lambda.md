# ADR-0006: Collect from Sydney, on Lambda

**Status:** Accepted · 2026-09-14 · Supersedes the compute half of ADR-0002

## Context

ADR-0002 chose GitHub Actions as the scheduler and said to revisit after three
months. Two things arrived sooner than that.

The first CI run died on Neon's idle-in-transaction timeout. The direct cause
was ours — a transaction held open across HTTP calls, since fixed — but the
reason it surfaced in CI and never locally is that GitHub-hosted runners are in
US Azure regions, and every request to an Australian bank carried an extra
couple of hundred milliseconds. Runs that take five minutes from Sydney take
considerably longer from Virginia, and long runs are fragile runs. GitHub
offers no region control on hosted runners, so this cannot be fixed in place.

The operator also raised how the traffic looks: an Australian open-banking
collector calling from a US address. On its own this argument is weak — these
endpoints are public, no consumer data is involved, no residency rule applies,
and plenty of legitimate CDR consumers run in US regions. It is not why this
decision was made, but it points the same way.

## Decision

Run the collector on AWS Lambda in **ap-southeast-2 (Sydney)**, invoked by
EventBridge Scheduler. Keep GitHub Actions for CI and for deploying the code.
Manage the infrastructure with Terraform.

- Lambda's free tier is perpetual (1M requests, 400k GB-seconds/month); two
  runs a day at 512MB is roughly 9,000 GB-seconds/month, a couple of percent.
- The connection string lives in SSM Parameter Store as a SecureString, read at
  cold start. Standard parameters are free; Secrets Manager is not, and rotation
  is not a requirement here.
- Deployment authenticates by GitHub OIDC. No AWS access keys exist anywhere.
  The trust policy matches `sub` against both subject formats, plus the
  `repository` and `ref` claims. GitHub changed its default for repositories
  created after 15 July 2026 to embed immutable owner and repository IDs, so
  the `repo:owner/name:ref:...` form every tutorial shows no longer matches a
  new repo -- and IAM separately refuses any GitHub trust policy that does not
  constrain `sub` or `job_workflow_ref`, so dropping `sub` is not an option.
  Both failure modes are opaque: a bare "Not authorized" from STS, and a
  MalformedPolicyDocument from IAM. Decoding a real token's claims settled it
  in one round trip; reasoning from the error messages would not have.
- Schedules are expressed in `Australia/Sydney`, so they hold their local time
  across daylight saving instead of drifting twice a year.
- Terraform ignores the function's code, and the deploy workflow ignores the
  infrastructure. Each owns one thing; neither reverts the other.

## Rationale

- Latency is the operational argument: shorter runs, fewer timeouts, less time
  holding anything open.
- It is the stronger portfolio artefact, and closer to the roles this project
  is meant to support. ADR-0002 accepted "weaker infrastructure story" as a
  cost; this repays it with something real rather than a toy.
- It cost no collector code. The CLI takes a database URL and the Lambda handler
  is a thin adapter over the same operations. That portability was ADR-0002's
  claimed benefit, and this is the first time it has actually been tested.

## Consequences

- (+) Same region as the data sources; runs are faster and less fragile.
- (+) Real infrastructure as code: IAM, scheduling, secrets, alarms, OIDC.
- (+) Two CloudWatch alarms now cover the failure mode a cron job cannot see —
  not "the run failed" but "nothing ran at all", which is the one that quietly
  ends the dataset.
- (−) More moving parts than a cron file, and an AWS account that can now incur
  cost if something is misconfigured. Bounded by the free tier and by a 900s
  timeout, but no longer structurally impossible.
- (−) Terraform state is local for now. Fine for one operator on one machine;
  the S3 backend is stubbed in `versions.tf` for when that stops being true.
- (−) The database should live in ap-southeast-2 too. Compute in Sydney talking
  to Postgres in Virginia would move the latency rather than remove it.
