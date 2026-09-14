# RateRadar — Operations Runbook

Written for a system whose operator may not look at it for three weeks. Every
procedure here assumes you have forgotten how it works.

## Health in 30 seconds

```bash
make health          # freshness, last run, failing brands, quarantine backlog
```

This runs `rateradar health`, which the scheduled workflow also runs before and
after collecting. Problems come in two severities, and only the first exits
non-zero under `--strict`:

- **FAIL — ours.** The pipeline is broken: no completed run, staleness beyond
  the threshold, or no changes detected in a week (which points at the differ,
  not the market).
- **warn — theirs.** A bank is broken: repeated failures, quarantined payloads,
  a brand holding no products.

The split matters operationally. A scheduled job that fails every time some
bank misbehaves is a job whose alerts get ignored, and then switched off — and
then the collection stops without anyone noticing, which is the one outcome
this project cannot survive.

The checks:

| Check | Healthy | Act when |
| --- | --- | --- |
| Freshness | last completed run < 14h ago | > 30h — the scheduler has stopped |
| Brand failures | 0 brands with ≥ 5 consecutive failures | any — an endpoint has moved or died |
| Quarantine backlog | no new reasons this week | a new `reason` appears — a bank changed its payload shape |
| Change volume | non-zero over a week | zero for 7 days — the differ is probably broken, not the market frozen |

The last one is the check people forget. A pipeline that has silently stopped
detecting change looks identical to a quiet market, and only a plausibility
floor catches it.

## Scheduled behaviour

- Collection runs twice a day (`.github/workflows/collect.yml`).
- Brand discovery refreshes weekly (`discover` job in the same workflow).
- A backup dump runs weekly (`.github/workflows/backup.yml`).

Runs are idempotent. Re-running any of them is always safe.

## Common situations

### The workflow has stopped running

GitHub disables scheduled workflows on repositories with no activity for 60
days. This is the most likely cause of a silent stop, and the failure is
invisible unless you look.

*Fix:* push any commit, then re-enable the workflow in the Actions tab.
*Prevent:* the weekly backup job commits a checksum file, which counts as
activity.

### One bank fails constantly

```bash
rateradar brands --failing
```

The circuit breaker will already have backed it off. Check whether the base URI
changed by re-running discovery:

```bash
rateradar discover --dry-run
```

If the register shows a new `publicBaseUri`, `rateradar discover --apply` fixes
it. If the bank has genuinely withdrawn, it will be marked inactive
automatically; leave the history in place.

### Quarantine is filling up

```bash
rateradar quarantine --summary
```

Group by `reason`. A single bank with a new shape is a normaliser gap: add a
fixture from the quarantined payload, extend the model, replay. Many banks at
once means the CDR standard version moved — check `negotiated_api_version`
across brands and bump the preferred version.

### Restoring from backup

Weekly dumps land in S3 as gzipped CSV, one file per table plus a manifest:

    s3://rateradar-backups-<account>/backups/2026/09/21/034100/
        manifest.json          rows and bytes per table, and when it was taken
        brand.csv.gz
        product_snapshot.csv.gz
        ...

To restore into an empty database:

```bash
aws s3 sync s3://rateradar-backups-<account>/backups/2026/09/21/034100/ ./restore/
cd restore && gunzip *.gz

rateradar migrate                     # create the schema first

# Order matters: foreign keys point at brand and collection_run.
for t in schema_migration brand collection_run collection_run_brand \
         product_snapshot product_current product_change quarantine; do
  psql "$RATERADAR_DATABASE_URL" -c "\copy $t FROM '$t.csv' WITH (FORMAT csv, HEADER)"
done

psql "$RATERADAR_DATABASE_URL" -c "SELECT setval(pg_get_serial_sequence('collection_run','run_id'), max(run_id)) FROM collection_run;"
psql "$RATERADAR_DATABASE_URL" -c "SELECT setval(pg_get_serial_sequence('product_snapshot','snapshot_id'), max(snapshot_id)) FROM product_snapshot;"
psql "$RATERADAR_DATABASE_URL" -c "SELECT setval(pg_get_serial_sequence('product_change','change_id'), max(change_id)) FROM product_change;"
```

Then check the row counts against `manifest.json`. That is what the manifest is
for: a backup nobody has verified is a backup nobody should rely on.

**Do this once, deliberately, against a scratch database.** An untested restore
is not a backup, and this dataset cannot be re-collected — the rate movements of
a given week exist nowhere else once they are gone.

### The database is full or suspended

Free-tier Postgres suspends when idle and can be reclaimed if untouched for
long periods. The weekly backup is the recovery path:

```bash
make restore FILE=backups/rateradar-YYYY-MM-DD.sql.gz
```

**Practise this once, deliberately, in the first month.** An untested backup is
not a backup, and this dataset cannot be re-collected retrospectively — history
is the one thing that cannot be rebuilt.

To reclaim space before restoring:

```bash
rateradar prune --raw-older-than 90d    # drops raw payloads, keeps canonical
```

### A normaliser bug corrupted the canonical forms

This is why raw payloads are kept.

```bash
rateradar rebuild --from-raw --since 2026-01-01   # recompute canonical + hashes
rateradar replay-diff --since 2026-01-01          # re-derive change events
```

`product_current` is a cache and is rebuilt as a side effect. Snapshots are
never mutated; a rebuild writes new rows with the new normaliser version.

## Deliberate operating principles

1. **A run never fails because a bank failed.** Only infrastructure faults fail
   a run. See ADR-0005.
2. **Never delete history to save space.** Drop `raw`, move data to cold
   storage, but never delete snapshots or change events.
3. **Do not fix data by hand.** If a value is wrong, fix the normaliser and
   replay. Manual edits make the dataset unciteable.
4. **Politeness is not optional.** The banks did not ask to be polled. Keep the
   concurrency caps and the identifying user agent with a contact URL; if a bank
   asks you to stop, stop and record it.

## Monthly, in ten minutes

- [ ] `make health`
- [ ] Skim the last four weekly change counts for a step change
- [ ] Check quarantine for new reasons
- [ ] Confirm the latest backup restores (quarterly at minimum)
- [ ] Check that no enabled brand has been silently inactive for a month
