# RateRadar — Roadmap

Phases are sequential and each one ends in something demonstrable. The rule
throughout: **the collector must never stop running while later phases are
built.**

## Phase 1 — Collector (now)

Goal: unbroken collection of ~20 brands, deposits and mortgages.

- [ ] Postgres provisioned, migrations applied
- [ ] Discovery populates the brand table from the CDR Register
- [ ] Collector runs end-to-end for one brand, locally
- [ ] Allowlist widened to ~20 brands
- [ ] Scheduled workflow green for seven consecutive days
- [ ] First verified real rate change in `product_change`, checked by hand
      against the bank's website

**Done when:** two weeks of unattended running with no manual intervention.
That criterion, not feature count, is what makes the rest possible.

## Phase 2 — Make the data legible

- Weekly digest: what moved, biggest movers, who moved first after an RBA
  decision
- A single static page published from the repo (no server, no cost)
- Data quality report: coverage, gaps, quarantine rate per bank

## Phase 3 — Serve it

- Spring Boot change-feed API over the same database (this is where the Java
  depth shows)
- Email or webhook alerts on a watched product
- Published dataset on the Hugging Face Hub, as Parquet, refreshed weekly —
  derived figures only, per the publication policy in `data-use.md`. Nothing
  is published by default; this is a separate, deliberate decision

## Phase 4 — Use it

- LLM summaries of what a terms-and-conditions change actually means for a
  customer
- "Who moves first?" analysis after each RBA decision — genuinely novel output
  that only a longitudinal dataset can produce
- Optional AWS migration with Terraform, once the dataset is worth protecting

## Phase 5 — Feed the next project

Point Sentinel at this pipeline. The run ledger, freshness checks and
quarantine table were designed to be its input, so RateRadar becomes the live
demo environment for the monitoring project rather than a synthetic one.

## Explicitly not doing

- Consumer data (requires CDR accreditation — a regulatory process, not a
  technical one)
- A comparison site (Canstar, Mozo and AustralianRates already do this well)
- Financial advice or recommendations of any kind
