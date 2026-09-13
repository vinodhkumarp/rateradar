"""Command line interface. Every operation the runbook mentions lives here.

Deployment-agnostic by design (ADR-0002): the scheduler only ever invokes these
commands with a DATABASE_URL, so moving off GitHub Actions is a workflow change,
not a rewrite.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import UTC, datetime

import psycopg
import typer

from . import db, register
from .collector import run_collection
from .config import settings

app = typer.Typer(add_completion=False, help="RateRadar — Australian banking product change feed")
log = logging.getLogger("rateradar")

EXIT_OK, EXIT_WARN, EXIT_FAIL = 0, 0, 1  # warnings must not fail a scheduled run


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    _setup_logging(verbose)
    if "USERNAME" in settings.user_agent:
        log.warning(
            "RATERADAR_USER_AGENT still contains the placeholder contact URL. "
            "Banks should be able to find out who is calling them -- see docs/data-use.md"
        )


@app.command()
def migrate() -> None:
    """Apply pending SQL migrations."""
    with db.connect(settings) as conn:
        applied = db.migrate(conn)
    typer.echo(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none'}")


@app.command()
def discover(
    apply: bool = typer.Option(False, "--apply", help="Write results to the database"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change"),
    grep: str = typer.Option("", "--grep", "-g", help="Filter listed brands by name"),
    limit: int = typer.Option(25, "--limit", "-n", help="Rows to show; 0 for all"),
) -> None:
    """Refresh the brand registry from the CDR Register (weekly).

    Without --apply this only reports. Use --grep to find the exact brand names
    to put in config/brands.allowlist.
    """
    brands, source = register.discover(settings)
    allowlist = register.load_allowlist(settings)
    resolution = register.resolve_allowlist(allowlist, brands)

    if dry_run or not apply:
        shown = [b for b in brands if grep.lower() in str(b["brand_name"]).lower()]
        shown.sort(key=lambda b: str(b["brand_name"]).lower())
        typer.echo(
            f"source={source} brands={len(brands)} matching={len(shown)} "
            f"allowlist={len(allowlist)} resolved={len(resolution.brand_ids)}"
        )
        for brand in shown if limit == 0 else shown[:limit]:
            marker = "*" if brand["brand_id"] in resolution.brand_ids else " "
            typer.echo(
                f" {marker} {str(brand['brand_name'])[:40]:40} {brand['brand_id']}  "
                f"{brand['public_base_uri']}"
            )
        if limit and len(shown) > limit:
            typer.echo(f"   ... and {len(shown) - limit} more (use --limit 0)")
        _report_allowlist(resolution)
        return

    with db.connect(settings) as conn:
        count = db.upsert_brands(conn, brands)
        enabled = db.enable_brands(conn, resolution.brand_ids)
    typer.echo(f"upserted {count} brands from {source}; enabled {enabled} for collection")
    _report_allowlist(resolution)


def _report_allowlist(resolution: register.AllowlistResolution) -> None:
    """Unmatched entries are the important output: quietly collecting 17 banks
    when the allowlist asked for 20 is exactly the silent failure to avoid."""
    for entry, name in sorted(resolution.matched.items()):
        typer.echo(f"  ok        {entry}  ->  {name}")
    for entry, candidates in sorted(resolution.ambiguous.items()):
        typer.echo(f"  AMBIGUOUS {entry}  ->  {len(candidates)} brands, none enabled:")
        for candidate in candidates:
            typer.echo(f"              - {candidate}")
    for entry in resolution.unmatched:
        typer.echo(f"  NO MATCH  {entry}")
    if resolution.ambiguous or resolution.unmatched:
        typer.echo(
            "  fix: run 'rateradar discover --grep <part of name>' to find the exact\n"
            "       name or id, then edit config/brands.allowlist"
        )


@app.command()
def collect(
    trigger: str = typer.Option("manual", help="schedule | manual | backfill"),
    brand: str = typer.Option("", help="Collect a single brand id"),
) -> None:
    """Run one collection pass. Always completes; records what happened."""
    try:
        with db.connect(settings) as conn:
            result = asyncio.run(
                run_collection(
                    conn,
                    settings,
                    trigger=trigger,
                    git_sha=os.environ.get("GITHUB_SHA"),
                    only_brand=brand or None,
                )
            )
    except psycopg.OperationalError as exc:
        # Infrastructure, not a bank. Say so in one line: a wall of traceback in
        # a scheduled job's log is how the real cause gets missed.
        typer.echo(f"database connection lost: {exc}".strip())
        raise typer.Exit(EXIT_FAIL) from exc
    typer.echo(db.dumps(result))


@app.command()
def health(strict: bool = typer.Option(False, help="Exit non-zero on FAIL-level problems")) -> None:
    """Freshness, brand failures, quarantine backlog, change plausibility.

    Problems come in two severities, and the distinction is the same one
    ADR-0005 makes about runs: our pipeline being broken is a failure; a bank
    being broken is a finding. Only the former exits non-zero, because a
    scheduled job that fails every time a bank misbehaves is a job whose alerts
    get ignored, and then switched off.
    """
    with db.connect(settings) as conn:
        snapshot = db.health_snapshot(conn)

    now = datetime.now(UTC)
    fail: list[str] = []
    warn: list[str] = []
    last_run = snapshot.get("last_completed_run")

    # --- ours to fix: the pipeline itself ---------------------------------
    if last_run is None:
        fail.append("no completed run yet")
    else:
        age_hours = (now - last_run).total_seconds() / 3600
        if age_hours > settings.freshness_fail_hours:
            fail.append(
                f"stale: last completed run {age_hours:.1f}h ago — is the scheduler running?"
            )
        elif age_hours > settings.freshness_warn_hours:
            warn.append(f"ageing: last completed run {age_hours:.1f}h ago")

    # A silently broken differ looks exactly like a quiet market. This is the
    # check people forget, and it is ours, not a bank's.
    if last_run and snapshot["changes_7d"] < settings.min_changes_per_week:
        fail.append("no changes detected in 7 days — suspect the differ, not the market")

    # --- theirs to fix: individual banks ----------------------------------
    if snapshot["brands_failing"]:
        warn.append(f"{snapshot['brands_failing']} brand(s) failing repeatedly")
    if snapshot["quarantine_open"]:
        warn.append(f"{snapshot['quarantine_open']} payload(s) quarantined")
    if snapshot["brands_without_products"]:
        warn.append(
            f"{snapshot['brands_without_products']} enabled brand(s) hold no products "
            "— check the category filter or the base URI (rateradar probe --brand ...)"
        )

    typer.echo(db.dumps(snapshot))
    for problem in fail:
        typer.echo(f"  FAIL {problem}")
    for problem in warn:
        typer.echo(f"  warn {problem}")
    if not fail and not warn:
        typer.echo("  ok")

    raise typer.Exit(EXIT_FAIL if (fail and strict) else EXIT_OK)


@app.command()
def brands(
    failing: bool = typer.Option(False, "--failing", help="Only brands with failures"),
) -> None:
    """List collected brands and their health."""
    with db.connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT brand_id, brand_name, public_base_uri, consecutive_failures,
                   circuit_open_until, last_success_at, negotiated_api_version
            FROM brand WHERE is_enabled AND (%s = false OR consecutive_failures > 0)
            ORDER BY consecutive_failures DESC, brand_id
            """,
            (failing,),
        )
        for row in cur.fetchall():
            flag = "FAIL" if row["consecutive_failures"] else " ok "
            typer.echo(
                f"[{flag}] {row['brand_name'][:32]:32} fails={row['consecutive_failures']:<3}"
                f" last_ok={row['last_success_at']} {row['public_base_uri']}"
            )


@app.command()
def changes(
    days: int = typer.Option(7, help="Look-back window"),
    change_type: str = typer.Option("", help="Filter, e.g. RATE_CHANGED"),
    limit: int = typer.Option(50),
) -> None:
    """Recent change events — the thing this project exists to produce."""
    with db.connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.detected_at, b.brand_name, c.product_id, c.change_type,
                   c.field_path, c.old_value, c.new_value, c.delta_bps
            FROM product_change c JOIN brand b USING (brand_id)
            WHERE c.detected_at > now() - make_interval(days => %s)
              AND (%s = '' OR c.change_type = %s)
            ORDER BY c.detected_at DESC LIMIT %s
            """,
            (days, change_type, change_type, limit),
        )
        for row in cur.fetchall():
            delta = f" {row['delta_bps']:+.0f}bp" if row["delta_bps"] is not None else ""
            typer.echo(
                f"{row['detected_at']:%Y-%m-%d %H:%M} {row['brand_name'][:24]:24}"
                f" {row['change_type']:<20}{delta} {row['field_path'] or ''}"
            )


@app.command()
def runs(
    limit: int = typer.Option(5, help="How many recent runs to show"),
) -> None:
    """What happened in recent runs, per brand. Read this before guessing."""
    with db.connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.run_id, r.started_at, r.status, r.trigger,
                   r.brands_total, r.brands_ok, r.brands_failed,
                   r.products_seen, r.snapshots_new, r.changes_emitted, r.quarantined
            FROM collection_run r ORDER BY r.run_id DESC LIMIT %s
            """,
            (limit,),
        )
        for run in cur.fetchall():
            typer.echo(
                f"\nrun {run['run_id']}  {run['started_at']:%Y-%m-%d %H:%M}  {run['status']}"
                f"  ({run['trigger']})  brands ok={run['brands_ok']} failed={run['brands_failed']}"
                f"  products={run['products_seen']} snapshots={run['snapshots_new']}"
                f" changes={run['changes_emitted']} quarantined={run['quarantined']}"
            )
            cur.execute(
                """
                SELECT b.brand_name, rb.status, rb.api_version, rb.products_seen,
                       rb.products_failed, rb.products_quarantined, rb.snapshots_new,
                       rb.http_requests, rb.duration_ms, rb.error_kind, rb.error_detail
                FROM collection_run_brand rb JOIN brand b USING (brand_id)
                WHERE rb.run_id = %s ORDER BY rb.status, b.brand_name
                """,
                (run["run_id"],),
            )
            for row in cur.fetchall():
                typer.echo(
                    f"   {row['status']:<8} {row['brand_name'][:26]:26}"
                    f" x-v={row['api_version'] or '?':<2}"
                    f" seen={row['products_seen']:<4} failed={row['products_failed']:<4}"
                    f" quarantined={row['products_quarantined']:<3}"
                    f" new={row['snapshots_new']:<4} reqs={row['http_requests']:<4}"
                    f" {row['duration_ms'] or 0}ms"
                )
                if row["error_detail"]:
                    typer.echo(f"            {row['error_kind']}: {row['error_detail'][:300]}")


@app.command()
def probe(
    brand: str = typer.Option(..., help="Brand id or name fragment to probe"),
    category: str = typer.Option("TERM_DEPOSITS", help="Product category to list"),
) -> None:
    """Show the raw HTTP exchange with one bank: list, then one product detail.

    For when a collection run reports failures and you need to see what the bank
    actually said, rather than what the collector made of it.
    """
    import httpx

    with db.connect(settings) as conn:
        candidates = [
            b
            for b in db.list_brands(conn)
            if brand.lower() in b["brand_id"].lower() or brand.lower() in b["brand_name"].lower()
        ]
    if len(candidates) != 1:
        typer.echo(f"need exactly one brand, matched {len(candidates)}")
        for candidate in candidates[:10]:
            typer.echo(f"  {candidate['brand_id']}  {candidate['brand_name']}")
        raise typer.Exit(EXIT_FAIL)

    target = candidates[0]
    base = str(target["public_base_uri"]).rstrip("/")
    typer.echo(f"{target['brand_name']}  {base}")
    headers = {"Accept": "application/json", "User-Agent": settings.user_agent}

    with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
        list_url = f"{base}/cds-au/v1/banking/products"
        for version in range(
            settings.product_api_max_version, settings.product_api_min_version - 1, -1
        ):
            response = client.get(
                list_url,
                params={"product-category": category, "page": 1, "page-size": 5},
                headers={"x-v": str(version), "x-min-v": str(settings.product_api_min_version)},
            )
            typer.echo(
                f"  LIST   x-v={version} -> {response.status_code} served={response.headers.get('x-v')}"
            )
            if response.status_code != 200:
                typer.echo(f"         {response.text[:200]}")
                continue
            products = (response.json().get("data") or {}).get("products") or []
            typer.echo(f"         {len(products)} product(s)")
            if not products:
                return
            product_id = products[0]["productId"]
            typer.echo(f"         first productId={product_id}")

            detail_url = f"{base}/cds-au/v1/banking/products/{product_id}"
            for detail_version in range(
                settings.product_api_max_version, settings.product_api_min_version - 1, -1
            ):
                detail = client.get(
                    detail_url,
                    headers={
                        "x-v": str(detail_version),
                        "x-min-v": str(settings.product_api_min_version),
                    },
                )
                typer.echo(
                    f"  DETAIL x-v={detail_version} -> {detail.status_code}"
                    f" served={detail.headers.get('x-v')}"
                )
                if detail.status_code == 200:
                    keys = sorted((detail.json().get("data") or {}).keys())
                    typer.echo(f"         ok, fields: {', '.join(keys)}")
                    return
                typer.echo(f"         {detail.text[:300]}")
            return


@app.command()
def quarantine(summary: bool = typer.Option(False, "--summary")) -> None:
    """Inspect payloads that failed validation."""
    with db.connect(settings) as conn, conn.cursor() as cur:
        if summary:
            cur.execute(
                "SELECT brand_id, reason, count(*) AS n, max(created_at) AS latest"
                " FROM quarantine WHERE resolved_at IS NULL"
                " GROUP BY brand_id, reason ORDER BY n DESC"
            )
        else:
            cur.execute(
                "SELECT brand_id, product_id, reason, error_detail, created_at"
                " FROM quarantine WHERE resolved_at IS NULL ORDER BY created_at DESC LIMIT 50"
            )
        for row in cur.fetchall():
            typer.echo(db.dumps(dict(row)))


if __name__ == "__main__":  # pragma: no cover
    app()
