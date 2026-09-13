# Data Use and Conduct

What this project accesses, what it deliberately does not, and how it behaves.
Written down because "we were being careful" is worth more when it is a policy
that predates the question, not a claim made afterwards.

This is not legal advice, and the author is not a lawyer. It is a statement of
what the project does and the reasoning behind it.

## What is accessed

Exactly two endpoints, per bank:

```
GET {publicBaseUri}/cds-au/v1/banking/products
GET {publicBaseUri}/cds-au/v1/banking/products/{productId}
```

These serve **Product Reference Data** under Australia's Consumer Data Right.
The Consumer Data Standards state that access to PRD does not require
authentication, and data holders are obliged to publish it. It is the same
product information the banks advertise publicly — rates, fees, features,
eligibility — in structured form. The CDR's own Product Comparator Demo calls
these same endpoints.

Endpoints are discovered from the CDR Register's public data holder brands
summary. No credentials, API keys, accreditation or registration are involved
anywhere in this system.

## What is never accessed

**Consumer data. Any of it.**

Account balances, transactions, direct debits, scheduled payments, payees,
customer details — all of it lives behind different endpoints
(`/banking/accounts`, `/banking/transactions`, and similar), and all of it
requires CDR accreditation, an individual customer's consent, and an OAuth
token. This project has none of those and requests none of them.

This is structural, not a matter of restraint: the collector constructs only the
two URLs above. There is no code path that could reach a consumer endpoint.

**No personal information of any kind is collected, stored, or processed.**

## How this system behaves

The Consumer Data Standards set a guidance threshold of roughly 300 transactions
per second for public APIs, and note that data holders may rate limit clients
they consider excessive. This project operates about four orders of magnitude
below that, deliberately.

| Setting | Value | Why |
| --- | --- | --- |
| Collection frequency | Twice a day | Rates rarely move more than once a day; more often buys precision nobody needs |
| Concurrent requests per bank | 1 | One request at a time to any one institution |
| Concurrent requests overall | 8 | Spread across different hosts |
| On HTTP 429 | Back off, honour `Retry-After` | The bank's own rate limiting is respected |
| On repeated failure | Circuit breaker, hours of cooldown | A struggling endpoint is left alone |
| Unchanged data | Not re-stored | Content hashing; no redundant work |
| Identification | User agent with a contact URL | Anyone at a bank can find out who is calling and ask |

Typical load: roughly 2,000–3,000 requests per day, spread across 20 different
institutions — around 150 requests per bank per day, at under 2 requests per
second at peak.

**Raising the concurrency settings is a decision to be less polite.** They are
not performance tuning.

## Publication policy

Collecting for private analysis and publishing a dataset are different acts with
different considerations. This project separates them deliberately.

**Rates, fees, dates and identifiers are facts**, and facts are not subject to
copyright. **Product descriptions and terms-and-conditions text are the banks'
own written expression**, and republishing thousands of them verbatim is a
different proposition.

So, if and when data from this project is published:

- **Published:** brand, product id, product category, product name, rates, fees
  as amounts, effective dates, observation timestamps, and computed changes
  (including basis-point movements).
- **Not published in bulk:** product descriptions, terms and conditions text,
  eligibility prose, or other extended free-text fields written by the bank.
  These remain in the local database, where they are needed for change
  detection.
- Any published dataset states its source, its method, and its limitations,
  including the observation windows within which a change was detected.

Nothing is published by default. Phase 1 is a private repository and a private
database. Publication is a separate, later, deliberate decision.

## If a bank objects

1. Stop collecting from that bank immediately: remove it from
   `config/brands.allowlist` and run `rateradar discover --apply`.
2. Record the request, the date and who made it, in this file.
3. Do not resume without their agreement.

The cost of losing one bank from the dataset is small. The cost of ignoring a
request like that is not.

No such requests have been received.

## Why the run ledger matters here too

`collection_run` and `collection_run_brand` record every request pattern this
system has ever made: when, how many, to whom, and with what outcome. That
exists for data quality reasons, but it also means the operating behaviour of
this project is fully auditable after the fact rather than reconstructed from
memory.
