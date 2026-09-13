# RateRadar — Architecture

## 1. What this system is

RateRadar is a **historical change feed for Australian retail banking products**.

Every bank regulated under the Consumer Data Right must publish its product
reference data — rates, fees, features, eligibility — through a standardised,
public, unauthenticated API. Comparison sites read that data and show you
*today's* rates. Nobody publishes *what changed, when, and by how much*.

RateRadar polls those APIs on a schedule, stores every version of every
product, and emits a typed change event whenever something moves.

The output is three things, in increasing order of value:

1. A queryable current view of ~20 banks' deposit and mortgage products.
2. An append-only history of every observed change.
3. An open dataset of that history, published for others to use.

**Non-goals for v1:** consumer-facing comparison UI, personalised
recommendations, anything requiring CDR accreditation (consumer data), and
anything requiring a bank's permission.

What this system accesses, what it never accesses, how it behaves towards the
banks it polls, and what may be republished are set out in `data-use.md`.

## 2. Design constraints

These shape every decision below. They are unusual, so they are stated up front.

| Constraint | Consequence |
| --- | --- |
| The operator has little time, in irregular bursts | The system must run unattended for weeks. No step may require a human on a schedule. |
| The dataset's value is unbroken continuity | A missed day is a permanent hole. Availability of *collection* matters more than availability of *serving*. |
| Data sources are ~117 independently operated APIs of varying quality | Partial failure is the normal case, not an exception. One broken bank must never fail a run. |
| Budget is approximately zero | Free tiers only. Storage growth must be sub-linear in time. |
| It is a portfolio artefact | Decisions must be documented and defensible, not just working. |

The second and third constraints together produce the single most important
design rule in this system:

> **A collection run must always complete and always record what happened,
> even when most of it fails.**

## 3. High-level shape

```
                   ┌───────────────────────────────┐
                   │ CDR Register (public)         │
                   │ data holder brands + base URIs│
                   └───────────────┬───────────────┘
                                   │ weekly
                                   ▼
                        ┌──────────────────┐
                        │  BRAND REGISTRY  │  which banks, which base URI,
                        │  (table)         │  enabled?, health state
                        └────────┬─────────┘
                                 │
   scheduled (GitHub Actions, twice a day)
                                 ▼
        ┌────────────────────────────────────────────────┐
        │ COLLECTOR                                      │
        │                                                │
        │  for each brand (bounded concurrency):         │
        │    1. GET /banking/products?product-category=… │
        │       (paginated, version-negotiated)          │
        │    2. GET /banking/products/{id} per product   │
        │    3. normalise → canonical JSON               │
        │    4. hash → unchanged? stop. changed? store.  │
        │                                                │
        │  every brand's outcome recorded independently  │
        └───────────────────┬────────────────────────────┘
                            ▼
        ┌────────────────────────────────────────────────┐
        │ POSTGRES (managed, free tier)                  │
        │                                                │
        │  brand            — who we poll                │
        │  collection_run   — every run, every outcome   │
        │  product_snapshot — immutable, content-hashed  │
        │  product_current  — fast "what is true now"    │
        │  product_change   — typed, append-only events  │
        │  quarantine       — payloads that failed       │
        │                     validation, kept not lost  │
        └───────────────────┬────────────────────────────┘
                            ▼
          ┌─────────────────────────────────────┐
          │ OUTPUTS (later phases)              │
          │  change feed API  │  alerts  │  HF  │
          │  (Spring Boot)    │  (email) │ data │
          └─────────────────────────────────────┘
```

## 4. The pipeline, stage by stage

### 4.1 Discovery — which banks exist?

Base URIs are not hardcoded. They come from the CDR Register's public
**Data Holder Brands Summary** endpoint, refreshed weekly into the `brand`
table. A second, independent source (a community-maintained endpoint list) is
supported as a fallback, because a single point of discovery failure would
silently shrink the dataset.

Brands are never deleted from the registry. A brand that disappears from the
register is marked inactive, with the date. *When a bank leaves the market is
itself data.*

### 4.2 Fetch — talking to 20 different implementations

The CDR standard is a standard; the implementations are not uniform. The client
therefore handles, as normal operation:

- **Version negotiation, per endpoint.** Versions are per endpoint, not per
  bank: Westpac serves the product list at v5 and product detail at v7. On a
  `406` the bank usually names the versions it supports, and that beats any
  assumption of ours, upward or downward. Blind stepping happens only when it
  says nothing. The negotiated version is cached per endpoint — without that,
  every product detail re-negotiates, which cost ~128 needless requests at one
  bank before it was fixed. Record the version each bank actually served;
  version drift across the estate is worth measuring.
- **Retries** with exponential backoff and jitter, on connection errors, `429`
  and `5xx`. Never retry `4xx` other than `429`.
- **Timeouts** on connect and read separately. A hung bank is more dangerous
  than a broken one.
- **Bounded concurrency**, globally and per host. We are an uninvited guest on
  someone else's API; we behave accordingly.
- **A circuit breaker per brand.** Consecutive failures escalate to a cooldown,
  so a dead endpoint doesn't consume the whole run's budget on every run.

Every response, good or bad, produces a row in `collection_run_brand`. If a
bank was unreachable, the dataset must be able to say *"unknown"* rather than
imply *"unchanged"* — this distinction is the difference between a dataset that
can be trusted and one that cannot.

### 4.3 Normalise — making comparison possible

Raw payloads cannot be diffed directly. Banks reorder arrays, vary whitespace,
include timestamps, and use inconsistent numeric formats (`"5.5"`, `"5.50"`,
`"0.055"`). The normaliser produces a **canonical form**:

- keys sorted; arrays sorted by a stable natural key
- rates parsed to `Decimal` and stored at fixed scale
- volatile fields (`lastUpdated`, request echoes) stripped and kept separately
- output validated against a Pydantic model

Anything that fails validation goes to `quarantine` with the raw payload and
the error. **Nothing is ever silently dropped** — a bank publishing malformed
data is a finding, not a bug in our pipeline.

### 4.4 Store — three tiers

The storage model separates three questions that get conflated in naive designs:

| Question | Table | Shape |
| --- | --- | --- |
| What did this bank publish at this moment? | `product_snapshot` | Immutable, content-addressed by SHA-256 of the canonical form. Written only when the hash changes. |
| What is true right now? | `product_current` | One row per product, upserted. Fast to query, cheap to serve. |
| What changed, when? | `product_change` | Append-only, typed events derived by diffing consecutive snapshots. |

Writing snapshots only on hash change is what keeps storage sub-linear: most
products don't change most days, so after the first full collection the daily
write volume is small. `first_seen_at` / `last_seen_at` on each snapshot record
the interval over which that version was observed.

### 4.5 Diff — from bytes to meaning

A structural diff between the previous and current canonical form is
classified into typed events:

`PRODUCT_ADDED`, `PRODUCT_WITHDRAWN`, `RATE_CHANGED`, `FEE_CHANGED`,
`ELIGIBILITY_CHANGED`, `FEATURE_CHANGED`, `TERMS_CHANGED`, `OTHER_CHANGED`

`RATE_CHANGED` carries `old_value`, `new_value` and `delta_bps`, because "ANZ
cut its 12-month term deposit by 15 basis points on 3 March" is the atomic unit
of everything this project is ultimately for.

Classification lives in one pure function over two canonical documents. It has
no I/O, so it is trivially testable — and it is the piece most likely to need
refinement as real data arrives.

## 5. Why this shape

**Why content hashing instead of storing every fetch?**
Free-tier Postgres is ~0.5 GB. Storing ~2,000 products at ~10 KB each, four
times a day, unconditionally, fills that in weeks. Hashing turns storage growth
from "proportional to time" into "proportional to actual change", which is
roughly what the dataset is worth anyway.

**Why derive change events instead of computing diffs on read?**
Because the change feed is the product. Computing it at write time means it is
cheap to query, cheap to publish, and — importantly — *stable*: a change event
records what we concluded at the time, using the classifier we had then.

**Why a run ledger?**
Two reasons. First, honesty: a dataset that cannot distinguish "no change" from
"we didn't look" is not scientifically usable. Second, the ledger is a real
operational telemetry stream, which makes this pipeline a genuine demo target
for Sentinel later.

**Why GitHub Actions rather than a server?**
It is free, requires no patching, has built-in retries, alerting and audit
history, and cannot silently die because a VPS ran out of disk. The trade-off —
a weaker infrastructure story — is deliberate and revisited in ADR-0002.

## 6. Failure modes, and what happens

| Failure | Behaviour | Detection |
| --- | --- | --- |
| One bank's API is down | That brand is marked `failed` for the run; everything else proceeds | Run summary; consecutive-failure counter |
| A bank returns malformed JSON | Quarantined with raw payload; product's last known state preserved | Quarantine count > 0 |
| The register endpoint is unavailable | Existing brand list is reused; refresh retried next week | Registry staleness metric |
| Database is unreachable | Run fails loudly and exits non-zero; no partial state | Workflow failure notification |
| Scheduled run doesn't fire | Detected as a gap in `collection_run` | Freshness check on next run |
| Rate limit / bank blocks us | Backoff, then circuit break; user agent identifies the project with a contact URL | 429 counter per brand |

## 7. Scale expectations (v1)

- ~20 brands × 2 categories × ~30–150 products ≈ **1,000–3,000 products**
- Detail fetches per run ≈ same, so ~3,000 HTTP requests per run
- 2 runs/day ≈ 6,000 requests/day, spread across 20 hosts — roughly 150 requests
  per bank per day, at under 2 requests per second. Deliberately conservative;
  see `data-use.md` for the reasoning and the limits it stays under
- Steady-state new snapshots per day: expected in the tens, spiking around RBA
  decision dates. That spike is the interesting signal.

## 8. Where this actually runs

Nothing in the source tree names a host, and that is deliberate — the collector
is a CLI that takes a database URL. This section says where it runs today, and
what would have to change to move it.

There are three environments, and only one of them keeps anything.

```
  DEVELOPMENT                    PRODUCTION                    STORAGE
  your laptop                    GitHub Actions                Neon (managed Postgres)
┌────────────────────────┐    ┌────────────────────────┐    ┌────────────────────────┐
│ python in .venv        │    │ ubuntu VM, created on  │    │ Postgres instance      │
│ rateradar collect      │    │ schedule, destroyed    │    │                        │
│                        │    │ ~5 minutes later       │───►│ scales to zero when    │
│ Postgres in docker     │    │                        │    │ idle, wakes in ~1s     │
│ compose (throwaway)    │    │ stateless: keeps       │    │                        │
│                        │    │ nothing between runs   │    │ THE ONLY DURABLE STATE │
└────────────────────────┘    └────────────────────────┘    └────────────────────────┘
         │                               │                             ▲
         └───────────────────────────────┴─────────────────────────────┘
                    connected only by RATERADAR_DATABASE_URL
```

**The Python runs on an ephemeral runner.** `.github/workflows/collect.yml`
schedules it twice a day. Each run checks out the repository, installs
dependencies fresh, applies migrations, collects, runs the health check, and
exits. There is no server to rent, patch or log into, and nothing on the runner
survives the run.

**Postgres is a managed instance at Neon**, reached over TLS with a normal
connection string held as a GitHub Actions secret. `config.py` reads it from
`RATERADAR_DATABASE_URL`; nothing else in the code knows where the database is.

**docker-compose is development only.** It provides a local Postgres so runs can
be broken and wiped without touching real data. The same `rateradar collect`
command runs in both environments; only the connection string differs.

### Why Neon rather than Supabase

Both offer roughly 0.5 GB free, which covers the storage budget in §7 for well
over a year. The deciding difference is idle behaviour. Neon scales to zero
after a few minutes and resumes in about a second, which suits a job that runs
twice a day and sleeps in between. Supabase pauses a project after a week
of inactivity and requires a manual unpause.

That matters only in the failure case, which is exactly the case this project is
designed around: the collector breaks while nobody is watching, and a week later
the database has paused too. One failure should not compound into two.

### First-time setup

1. Create a Neon project (Australian region if offered) and copy the connection
   string.
2. Add it as the repository secret `RATERADAR_DATABASE_URL`.
3. Keep the repository public, so Actions minutes are unmetered.
4. Push. CI runs immediately; collection begins on its schedule, or on demand
   from the Actions tab.

Running cost: zero.

### What this implies elsewhere in the design

Three earlier decisions exist because of this topology, and would be
over-engineering without it:

- **Content-addressed writes** — a scheduler that occasionally double-fires, and
  runs that can die mid-way, make idempotency a requirement rather than a nicety.
- **The run ledger** — the runner keeps no memory between runs, so the database
  is the only place run history can live.
- **Weekly backups** — Neon is now a single point of failure for a dataset that
  cannot be re-collected retrospectively.

### Moving off it later

Migrating to ECS, Lambda, a VPS or Kubernetes means changing what invokes
`rateradar collect` and where `RATERADAR_DATABASE_URL` points. No application
code changes. That portability is the reason the free-tier choice in ADR-0002 is
cheap to revisit, and it is the honest answer to "but this isn't real
infrastructure": the infrastructure is swappable by design.

## 9. Roadmap boundary

Phase 1 (this scaffold) ends when the collector has run unattended for two
weeks and the change feed contains real, verified rate movements.

Everything after that — the Spring Boot API, alerting, Hugging Face dataset
publication, LLM summarisation of terms changes, and Sentinel monitoring this
pipeline — is deliberately out of scope until then. See `roadmap.md`.
