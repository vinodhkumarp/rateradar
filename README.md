# RateRadar

**A historical change feed for Australian retail banking products.**

Every bank regulated under the Consumer Data Right must publish its product
reference data — rates, fees, features, eligibility — through a standardised,
public, unauthenticated API. Comparison sites read that data and show you
*today's* rates. Nobody publishes **what changed, when, and by how much**.

RateRadar polls those APIs on a schedule, stores every version of every product,
and emits a typed change event whenever something moves. The output is an open,
longitudinal dataset of Australian banking behaviour that does not currently
exist anywhere.

```
"ANZ cut its 12-month term deposit by 15 basis points, and we know it happened
 between 03:17 and 09:17 on 3 March."
```

## Status

Phase 1 — collector. See [`docs/roadmap.md`](docs/roadmap.md).

## Quickstart

```bash
make setup                 # venv + dev dependencies
make db-up                 # local Postgres via docker compose
cp .env.example .env       # point RATERADAR_DATABASE_URL at it

rateradar migrate          # create the schema
rateradar discover                 # list banks from the CDR register (no writes)
rateradar discover --grep macquarie  # find one bank's exact name
#   → config/brands.allowlist already names the 20 to start with; edit if needed
rateradar discover --apply         # write brands, enable the allowlist

rateradar collect --brand <one-brand-id>   # prove it end to end on one bank
rateradar changes --days 1                 # see what it found
rateradar health                           # freshness, failures, quarantine

make dash                                  # Grafana over the run ledger, localhost:3000
```

`make check` runs everything CI runs: ruff, mypy, pytest.

To exercise the deployed shape — the Lambda handler, Parameter Store, the S3
backup — rather than just the collector, `make local-up` stands the whole thing
up locally on [Floci](https://floci.io/). See [`deploy/README.md`](deploy/README.md).

## How it works

```
CDR Register ──► brand registry ──► collector ──► Postgres ──► change feed
 (weekly)          (~20 banks)      (twice a day)               (the product)
```

Three storage tiers, kept deliberately separate:

| Question | Table | Mutability |
| --- | --- | --- |
| What did the bank publish at that moment? | `product_snapshot` | Immutable, content-addressed |
| What is true now? | `product_current` | Rebuildable cache |
| What changed, and when? | `product_change` | Append-only |

Products are normalised to a canonical form and hashed, so a snapshot is written
only when something actually changed. That keeps storage proportional to change
rather than to time, and makes every run idempotent.

The design principle underneath all of it: **the dataset must always be able to
distinguish "it didn't change" from "we didn't look."** Every run and every
brand's outcome is recorded, and every change event carries the observation
window in which the change must have occurred.

## Where it runs

| | Runs where | Persists? |
| --- | --- | --- |
| Collector (Python) | A GitHub Actions runner, created twice a day and destroyed after ~5 minutes | No — stateless by design |
| Database | Managed Postgres at Neon, free tier | **Yes — the only durable state** |
| Local Postgres (`docker compose`) | Your machine, development only | Throwaway |

The three are connected by exactly one thing: `RATERADAR_DATABASE_URL`. No code
knows where it runs, which is what makes moving to ECS, Lambda or a VPS a
workflow change rather than a rewrite. Full topology and setup steps in
[`docs/architecture.md` §8](docs/architecture.md).

Running cost: zero.

## Layout

```
src/rateradar/
  config.py      settings; everything tunable lives here
  register.py    discovery — which banks exist, and where
  client.py      HTTP: version negotiation, retries, politeness, circuit breaker
  normalise.py   canonical form + content hash          (pure, heavily tested)
  differ.py      canonical docs → typed change events   (pure, heavily tested)
  db.py          storage; plain SQL, no ORM
  collector.py   the run: fetch → normalise → store → diff
  cli.py         every operation the runbook mentions

deploy/          Lambda packaging, Terraform, local Floci stack
migrations/      SQL, applied by `rateradar migrate`
docs/            architecture, data model, ADRs, runbook, roadmap
tests/           offline: no database, no bank APIs, no network
.github/workflows/  ci · collect (twice daily) · backup (weekly)
```

## Documentation

| Document | Read it when |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | You want the whole picture |
| [`docs/data-model.md`](docs/data-model.md) | You are writing a query, or wondering why the schema looks like that |
| [`docs/operations.md`](docs/operations.md) | Something is broken, or you have been away for three weeks |
| [`docs/data-use.md`](docs/data-use.md) | You want to know what this accesses, what it never accesses, and how it behaves |
| [`deploy/grafana/`](deploy/grafana/) | You want the dashboard, or to point it at a different database |
| [`docs/roadmap.md`](docs/roadmap.md) | You are deciding what to build next |
| [`docs/adr/`](docs/adr/) | You disagree with a decision and want to know what it traded away |

## Before you point this at real banks

1. Read [`docs/data-use.md`](docs/data-use.md). It is short.
2. Put a real contact URL in `RATERADAR_USER_AGENT`. These are other people's
   APIs and they should be able to reach you.
3. Leave the concurrency and cadence settings alone. They are not performance
   tuning; raising them is a decision to be less polite.
4. Start with one or two brands until a full run is clean.

## Licence

MIT.
