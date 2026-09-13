# RateRadar — Data Model

The schema lives in `migrations/001_initial_schema.sql`. This document explains
*why* it looks like that, which the SQL cannot.

## The three questions

Naive versions of this project use one table: "products, as last seen". That
answers one question and destroys the other two. RateRadar keeps them apart:

| Question | Table | Mutability |
| --- | --- | --- |
| What did the bank publish at that moment? | `product_snapshot` | Immutable |
| What is true now? | `product_current` | Overwritten each run |
| What changed, and when? | `product_change` | Append-only |

`product_current` is a **cache**, in the strict sense: it can be rebuilt
entirely from `product_snapshot`. If a bug corrupts it, we truncate and replay.
`product_snapshot` and `product_change` are the sources of truth and are never
updated in place.

## Identity

A product is identified by `(brand_id, product_id)`.

`product_id` is assigned by the bank. Two consequences that will eventually
bite, and are accepted for v1:

- Some banks mint a **new** `product_id` when a product materially changes,
  rather than updating the existing one. This looks like a withdrawal plus an
  addition. Later phases can add fuzzy identity resolution on name and
  category; v1 records what the bank said and leaves the interpretation to the
  reader.
- `product_id` is not globally unique, hence the composite key.

## Content addressing

`product_snapshot.content_hash` is the SHA-256 of the canonical JSON form. The
uniqueness constraint is `(brand_id, product_id, content_hash)`, so re-observing
an unchanged product writes no new row — it only advances `last_seen_at`.

This gives three properties at once:

1. Storage grows with *change*, not with *time*.
2. Idempotency is free. Re-running a collection, or backfilling, cannot create
   duplicates or spurious change events.
3. "Did this product revert to a previous state?" becomes a hash comparison.

Note the deliberate consequence: **a flip-flop is not a distinct row.** If a
product changes A → B → A, the third state matches the first hash. The change
events still record both transitions, so nothing is lost, but the snapshot
table stays compact. `first_seen_at`/`last_seen_at` on a snapshot therefore
describe an *observation window*, not a continuous period of validity.

## Canonical form

Raw bank payloads are not directly comparable. The normaliser produces a
canonical document with:

- object keys sorted
- arrays sorted by a stable natural key (rate type + tier + term), so that a
  bank reordering its rate array is not reported as a change
- rates as `Decimal` strings at fixed scale, so `"5.5"` and `"5.50"` are equal
- volatile and non-semantic fields removed (`lastUpdated`, echoed request
  parameters, self links)

The raw payload is retained alongside in `product_snapshot.raw`, so a
normalisation bug is always recoverable — we can rebuild canonical forms and
replay the differ over history. This is the single most important reversibility
property in the system, and the reason `raw` is stored even though it roughly
doubles snapshot size.

## Change types

| `change_type` | Meaning |
| --- | --- |
| `PRODUCT_ADDED` | First time we have ever seen this product |
| `PRODUCT_WITHDRAWN` | Previously present, now absent from the bank's list |
| `PRODUCT_REAPPEARED` | Absent, then present again |
| `RATE_CHANGED` | A deposit or lending rate moved; carries `delta_bps` |
| `FEE_CHANGED` | A fee amount or structure changed |
| `ELIGIBILITY_CHANGED` | Who can hold the product changed |
| `FEATURE_CHANGED` | Features added or removed |
| `TERMS_CHANGED` | Constraints, conditions or T&C document reference changed |
| `OTHER_CHANGED` | A real change the classifier could not categorise |

`OTHER_CHANGED` is intentional. A classifier that silently swallows what it
doesn't recognise produces a dataset that lies by omission. Anything unmatched
is emitted as `OTHER_CHANGED` with its JSON pointer, and a healthy backlog of
those is the signal to extend the classifier.

## Time, and being honest about it

Four distinct timestamps matter, and conflating them is the classic error in
this kind of dataset:

| Time | Meaning | Source |
| --- | --- | --- |
| `detected_at` | When our pipeline noticed | Us |
| `observed_after` | The previous time we successfully saw this product | Us |
| `observed_before` | The observation that revealed the change | Us |
| effective dates inside `canonical` | When the bank says the rate applies | The bank |

We can never claim to know exactly when a rate changed — only that it changed
somewhere in the `(observed_after, observed_before]` window. Storing that window
explicitly is what makes the dataset defensible. A twice-daily cadence means the
window is usually under twelve hours; after an outage it may be days wide, and
the data says so rather than pretending. Collecting more often would narrow it,
at a cost paid by someone else's infrastructure -- see `data-use.md`.

## The run ledger

`collection_run` and `collection_run_brand` record every run and every brand's
outcome inside it. This exists so that the dataset can distinguish:

- "the rate did not change" (we looked, hash matched), from
- "we don't know" (we couldn't reach the bank).

Any analysis built on this data must join against the ledger to be correct.
It also doubles as the operational telemetry stream — freshness, error rates,
per-brand reliability — which is what a later monitoring project consumes.

## Quarantine

Invalid payloads are written to `quarantine` with the raw body and the
validation error, and the product's last known good state is left untouched.

The rule: **the pipeline never decides that bad data means no data.** A bank
publishing something unparseable is a fact about that bank, and after a few
weeks the quarantine table becomes its own small finding — "these three
institutions do not conform to the standard they are obliged to implement."

## Storage budget

Free-tier Postgres is around 0.5 GB. Rough v1 figures:

| Item | Estimate |
| --- | --- |
| Products tracked | ~2,000 |
| Canonical + raw per snapshot | ~15 KB |
| Initial full load | ~30 MB |
| New snapshots per day (steady state) | tens, not thousands |
| Growth | ~0.5–2 MB/day, spiking around RBA decisions |

That is comfortably inside a free tier for well over a year. When it stops
being comfortable, the first lever is dropping `raw` for snapshots older than
90 days (the `raw` column is nullable precisely for this), and the second is
moving cold snapshots to object storage as Parquet — which is also the format
the public dataset will be published in.
