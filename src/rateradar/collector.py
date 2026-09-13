"""The collection run.

The rule that shapes this module (ADR-0005): a run always completes and always
records what happened, even when most brands fail. Only infrastructure faults --
a dead database, broken config -- fail the run itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import psycopg

from . import db
from .client import BrandStats, CDRClient, ErrorKind, FetchError
from .config import Settings
from .differ import diff_products
from .normalise import NormalisationError, canonicalise, content_hash, headline_rate

log = logging.getLogger(__name__)


class RunTotals(dict[str, int]):
    def bump(self, key: str, amount: int = 1) -> None:
        self[key] = self.get(key, 0) + amount


async def _collect_brand(
    client: CDRClient,
    conn: db.Conn,
    brand: db.BrandRow,
    *,
    run_id: int,
    settings: Settings,
    totals: RunTotals,
) -> None:
    started = time.monotonic()
    stats = BrandStats()
    seen_ids: set[str] = set()
    snapshots_new = 0
    products_failed = 0
    quarantined_before = totals.get("quarantined", 0)
    status = "ok"
    error_kind: str | None = None
    error_detail: str | None = None
    known_before = db.current_product_ids(conn, brand.brand_id)

    try:
        # 1. List. Paging is sequential by nature (page N+1 depends on N).
        summaries: dict[str, dict[str, Any]] = {}
        for category in settings.categories:
            async for summary in client.list_products(brand.public_base_uri, category, stats=stats):
                summaries[str(summary["productId"])] = summary
        seen_ids = set(summaries)

        # 2. Fetch details concurrently. The client's semaphores bound how hard we
        #    lean on any one bank; this is where the run's wall-clock time goes.
        async def _detail(pid: str) -> tuple[str, dict[str, Any] | FetchError]:
            try:
                return pid, await client.get_product_detail(brand.public_base_uri, pid, stats=stats)
            except FetchError as exc:
                return pid, exc

        results = await asyncio.gather(*(_detail(pid) for pid in sorted(summaries)))

        # 3. Store sequentially. All database work for a brand happens on one
        #    connection inside one transaction, so writes stay consistent and a
        #    mid-brand failure rolls the whole brand back rather than half of it.
        for product_id, outcome in results:
            if isinstance(outcome, FetchError):
                # One bad product must not cost us the brand -- but the reason is
                # recorded, because "partial" with no explanation is not an outcome.
                products_failed += 1
                if error_detail is None:
                    error_kind, error_detail = outcome.kind, outcome.detail
                log.warning("%s/%s detail failed: %s", brand.brand_id, product_id, outcome)
                continue
            if _store_product(
                conn,
                brand=brand,
                product_id=product_id,
                payload=outcome,
                summary=summaries[product_id],
                run_id=run_id,
                settings=settings,
                api_version=stats.api_version,
                totals=totals,
            ):
                snapshots_new += 1

        if products_failed:
            # Nothing collected at all is a failure, however politely it failed:
            # it must trip the circuit breaker and show up in the health check.
            status = "failed" if products_failed == len(summaries) else "partial"

        # Products previously available and absent from every category this run.
        withdrawn = sorted(known_before - seen_ids)
        if withdrawn:
            _record_withdrawals(conn, brand, withdrawn, run_id=run_id, totals=totals)

        conn.commit()

    except FetchError as exc:
        conn.rollback()
        status, error_kind, error_detail = "failed", exc.kind, exc.detail
        log.warning("brand %s failed: %s", brand.brand_id, exc)
    except psycopg.Error:
        conn.rollback()
        raise  # database problems are infrastructure: fail the run
    except Exception as exc:
        conn.rollback()
        status, error_kind, error_detail = "failed", ErrorKind.TRANSPORT, repr(exc)
        log.exception("brand %s raised", brand.brand_id)

    db.record_run_brand(
        conn,
        run_id,
        brand.brand_id,
        status=status,
        api_version=stats.api_version,
        products_seen=len(seen_ids),
        products_failed=products_failed,
        products_quarantined=totals.get("quarantined", 0) - quarantined_before,
        snapshots_new=snapshots_new,
        http_requests=stats.http_requests,
        duration_ms=int((time.monotonic() - started) * 1000),
        error_kind=error_kind,
        error_detail=error_detail,
    )
    db.record_brand_health(
        conn,
        brand.brand_id,
        success=status != "failed",
        settings=settings,
        api_version=stats.api_version,
    )

    totals.bump("brands_ok" if status != "failed" else "brands_failed")
    totals.bump("products_seen", len(seen_ids))
    totals.bump("snapshots_new", snapshots_new)


def _store_product(
    conn: db.Conn,
    *,
    brand: db.BrandRow,
    product_id: str,
    payload: dict[str, Any],
    summary: dict[str, Any],
    run_id: int,
    settings: Settings,
    api_version: int | None,
    totals: RunTotals,
) -> bool:
    """Normalise, hash, store if changed, emit change events. True if new snapshot."""
    try:
        canonical = canonicalise(payload)
    except NormalisationError as exc:
        db.quarantine(
            conn,
            brand_id=brand.brand_id,
            product_id=product_id,
            run_id=run_id,
            reason="schema_invalid",
            error_detail=str(exc),
            raw=payload,
        )
        totals.bump("quarantined")
        return False

    new_hash = content_hash(canonical)
    previous = db.latest_snapshot(conn, brand.brand_id, product_id)
    unchanged = previous is not None and previous.content_hash == new_hash
    observed_after = previous.last_seen_at if previous else datetime.now(UTC)

    snapshot_id = db.insert_snapshot(
        conn,
        brand_id=brand.brand_id,
        product_id=product_id,
        content_hash=new_hash,
        canonical=canonical,
        raw=payload if settings.store_raw_payloads else None,
        api_version=api_version,
        run_id=run_id,
    )

    changes = []
    if not unchanged:
        changes = diff_products(
            previous.canonical if previous else None,
            canonical,
            max_changes=settings.max_changes_per_product,
        )
        emitted = db.insert_changes(
            conn,
            brand_id=brand.brand_id,
            product_id=product_id,
            changes=changes,
            from_snapshot_id=previous.snapshot_id if previous else None,
            to_snapshot_id=snapshot_id,
            run_id=run_id,
            observed_after=observed_after,
            observed_before=datetime.now(UTC),
        )
        totals.bump("changes_emitted", emitted)

    rate, kind = headline_rate(canonical)
    db.upsert_current(
        conn,
        brand_id=brand.brand_id,
        product_id=product_id,
        snapshot_id=snapshot_id,
        category=canonical.get("productCategory") or summary.get("productCategory"),
        name=canonical.get("name"),
        description=canonical.get("description"),
        headline_rate=rate,
        headline_rate_kind=kind,
        changed=bool(changes),
    )
    return not unchanged


def _record_withdrawals(
    conn: db.Conn,
    brand: db.BrandRow,
    product_ids: list[str],
    *,
    run_id: int,
    totals: RunTotals,
) -> None:
    now = datetime.now(UTC)
    for product_id in product_ids:
        previous = db.latest_snapshot(conn, brand.brand_id, product_id)
        db.insert_changes(
            conn,
            brand_id=brand.brand_id,
            product_id=product_id,
            changes=diff_products(previous.canonical if previous else {}, None),
            from_snapshot_id=previous.snapshot_id if previous else None,
            to_snapshot_id=None,
            run_id=run_id,
            observed_after=previous.last_seen_at if previous else now,
            observed_before=now,
        )
        totals.bump("changes_emitted")
    db.mark_withdrawn(conn, brand.brand_id, product_ids)


async def run_collection(
    conn: db.Conn,
    settings: Settings,
    *,
    trigger: str = "manual",
    git_sha: str | None = None,
    only_brand: str | None = None,
) -> dict[str, Any]:
    brands = db.enabled_brands(conn)
    if only_brand:
        brands = [b for b in brands if b.brand_id == only_brand]
    if not brands:
        log.warning("no enabled brands: run 'rateradar discover --apply' first")

    run_id = db.start_run(conn, trigger=trigger, git_sha=git_sha)
    totals = RunTotals(brands_total=len(brands))
    log.info("run %s starting: %d brands", run_id, len(brands))

    try:
        async with CDRClient(settings) as client:
            # Brands are processed one at a time: a single database connection is a
            # single transaction scope, and interleaving brands across it would let
            # one brand's commit publish another's half-written state. Concurrency
            # lives inside each brand, where the network latency actually is.
            for brand in brands:
                await _collect_brand(
                    client, conn, brand, run_id=run_id, settings=settings, totals=totals
                )
    except psycopg.Error:
        db.finish_run(conn, run_id, status="failed", **totals)
        raise

    db.finish_run(conn, run_id, status="completed", **totals)
    log.info("run %s finished: %s", run_id, dict(totals))
    return {"run_id": run_id, **totals}
