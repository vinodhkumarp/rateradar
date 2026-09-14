"""Storage layer. Plain SQL against Postgres -- no ORM.

The schema is the interesting artefact here, so it stays visible. Every write is
idempotent; a re-run must never duplicate a snapshot or emit a phantom change.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Buffer, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import REPO_ROOT, Settings
from .differ import Change

log = logging.getLogger(__name__)

# psycopg's Connection is generic over row type. Every query here uses dict_row,
# so pin it once and let the type checker verify every row["column"] access.
Conn = psycopg.Connection[dict[str, Any]]
Cur = psycopg.Cursor[dict[str, Any]]


def one(cur: Cur) -> dict[str, Any]:
    """Fetch a row that must exist. A missing row here is a bug, not a state."""
    row = cur.fetchone()
    if row is None:  # pragma: no cover - would mean the query lost its RETURNING
        raise RuntimeError("expected exactly one row")
    return row


@dataclass(slots=True)
class BrandRow:
    brand_id: str
    brand_name: str
    public_base_uri: str
    consecutive_failures: int = 0
    circuit_open_until: datetime | None = None


@dataclass(slots=True)
class SnapshotRow:
    snapshot_id: int
    content_hash: str
    canonical: dict[str, Any]
    last_seen_at: datetime


@contextmanager
def connect(settings: Settings) -> Iterator[Conn]:
    with psycopg.connect(str(settings.database_url), row_factory=dict_row) as conn:
        yield conn


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------
def migrate(conn: Conn, migrations_dir: Path | None = None) -> list[str]:
    """Apply any SQL files not yet recorded in schema_migration. Idempotent."""
    directory = migrations_dir or (REPO_ROOT / "migrations")
    applied: list[str] = []

    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            " filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT filename FROM schema_migration")
        done = {row["filename"] for row in cur.fetchall()}

        for path in sorted(directory.glob("*.sql")):
            if path.name in done:
                continue
            log.info("applying migration %s", path.name)
            cur.execute(path.read_text())
            cur.execute("INSERT INTO schema_migration (filename) VALUES (%s)", (path.name,))
            applied.append(path.name)
    conn.commit()
    return applied


# --------------------------------------------------------------------------
# brands
# --------------------------------------------------------------------------
def upsert_brands(conn: Conn, brands: list[dict[str, Any]]) -> int:
    """Insert or update discovered brands. Never deletes: departure is data."""
    with conn.cursor() as cur:
        for brand in brands:
            cur.execute(
                """
                INSERT INTO brand (brand_id, brand_name, legal_entity_name, abn,
                                   public_base_uri, industries, is_active_in_cdr, updated_at)
                VALUES (%(brand_id)s, %(brand_name)s, %(legal_entity_name)s, %(abn)s,
                        %(public_base_uri)s, %(industries)s, true, now())
                ON CONFLICT (brand_id) DO UPDATE SET
                    brand_name        = EXCLUDED.brand_name,
                    legal_entity_name = EXCLUDED.legal_entity_name,
                    public_base_uri   = EXCLUDED.public_base_uri,
                    industries        = EXCLUDED.industries,
                    is_active_in_cdr  = true,
                    left_register_at  = NULL,
                    updated_at        = now()
                """,
                brand,
            )
        seen = [b["brand_id"] for b in brands]
        if seen:
            cur.execute(
                """
                UPDATE brand SET is_active_in_cdr = false,
                                 left_register_at = COALESCE(left_register_at, now())
                WHERE brand_id <> ALL(%s) AND is_active_in_cdr
                """,
                (seen,),
            )
    conn.commit()
    return len(brands)


def list_brands(conn: Conn) -> list[dict[str, Any]]:
    """Every known brand, for allowlist resolution and reporting."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT brand_id, brand_name, public_base_uri, is_enabled, is_active_in_cdr"
            " FROM brand ORDER BY brand_name"
        )
        return [dict(row) for row in cur.fetchall()]


def enable_brands(conn: Conn, brand_ids: list[str]) -> int:
    with conn.cursor() as cur:
        cur.execute("UPDATE brand SET is_enabled = (brand_id = ANY(%s))", (brand_ids,))
        count = cur.rowcount
    conn.commit()
    return count


def enabled_brands(conn: Conn, *, include_open_circuits: bool = False) -> list[BrandRow]:
    clause = (
        ""
        if include_open_circuits
        else "AND (circuit_open_until IS NULL OR circuit_open_until < now())"
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT brand_id, brand_name, public_base_uri, consecutive_failures, circuit_open_until
            FROM brand
            WHERE is_enabled AND is_active_in_cdr {clause}
            ORDER BY brand_id
            """
        )
        return [BrandRow(**row) for row in cur.fetchall()]


def record_brand_health(
    conn: Conn,
    brand_id: str,
    *,
    success: bool,
    settings: Settings,
    api_version: int | None = None,
) -> None:
    with conn.cursor() as cur:
        if success:
            cur.execute(
                "UPDATE brand SET consecutive_failures = 0, circuit_open_until = NULL,"
                " last_success_at = now(),"
                " negotiated_api_version = COALESCE(%s, negotiated_api_version)"
                " WHERE brand_id = %s",
                (api_version, brand_id),
            )
        else:
            cur.execute(
                """
                UPDATE brand
                SET consecutive_failures = consecutive_failures + 1,
                    circuit_open_until = CASE
                        WHEN consecutive_failures + 1 >= %s
                        THEN now() + make_interval(hours => %s)
                        ELSE circuit_open_until END
                WHERE brand_id = %s
                """,
                (settings.circuit_failure_threshold, settings.circuit_cooldown_hours, brand_id),
            )
    conn.commit()


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------
def start_run(conn: Conn, *, trigger: str, git_sha: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_run (trigger, git_sha) VALUES (%s, %s) RETURNING run_id",
            (trigger, git_sha),
        )
        run_id = one(cur)["run_id"]
    conn.commit()
    return int(run_id)


def finish_run(conn: Conn, run_id: int, *, status: str, **counts: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE collection_run SET finished_at = now(), status = %s,
                brands_total = %s, brands_ok = %s, brands_failed = %s,
                products_seen = %s, snapshots_new = %s, changes_emitted = %s, quarantined = %s
            WHERE run_id = %s
            """,
            (
                status,
                counts.get("brands_total", 0),
                counts.get("brands_ok", 0),
                counts.get("brands_failed", 0),
                counts.get("products_seen", 0),
                counts.get("snapshots_new", 0),
                counts.get("changes_emitted", 0),
                counts.get("quarantined", 0),
                run_id,
            ),
        )
    conn.commit()


def record_run_brand(conn: Conn, run_id: int, brand_id: str, **fields: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO collection_run_brand (run_id, brand_id, status, api_version,
                products_seen, products_failed, products_quarantined, snapshots_new,
                http_requests, duration_ms, error_kind, error_detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, brand_id) DO UPDATE SET
                status = EXCLUDED.status, api_version = EXCLUDED.api_version,
                products_seen = EXCLUDED.products_seen,
                products_failed = EXCLUDED.products_failed,
                products_quarantined = EXCLUDED.products_quarantined,
                snapshots_new = EXCLUDED.snapshots_new,
                http_requests = EXCLUDED.http_requests, duration_ms = EXCLUDED.duration_ms,
                error_kind = EXCLUDED.error_kind, error_detail = EXCLUDED.error_detail
            """,
            (
                run_id,
                brand_id,
                fields.get("status", "failed"),
                fields.get("api_version"),
                fields.get("products_seen", 0),
                fields.get("products_failed", 0),
                fields.get("products_quarantined", 0),
                fields.get("snapshots_new", 0),
                fields.get("http_requests", 0),
                fields.get("duration_ms"),
                fields.get("error_kind"),
                (fields.get("error_detail") or "")[:2000] or None,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------
# snapshots, current state, changes
# --------------------------------------------------------------------------
def latest_snapshot(conn: Conn, brand_id: str, product_id: str) -> SnapshotRow | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.snapshot_id, s.content_hash, s.canonical, s.last_seen_at
            FROM product_current c JOIN product_snapshot s ON s.snapshot_id = c.snapshot_id
            WHERE c.brand_id = %s AND c.product_id = %s
            """,
            (brand_id, product_id),
        )
        row = cur.fetchone()
    return SnapshotRow(**row) if row else None


def insert_snapshot(
    conn: Conn,
    *,
    brand_id: str,
    product_id: str,
    content_hash: str,
    canonical: dict[str, Any],
    raw: dict[str, Any] | None,
    api_version: int | None,
    run_id: int,
) -> int:
    """Insert if this exact content is new; otherwise advance last_seen_at."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO product_snapshot (brand_id, product_id, content_hash, canonical,
                                          raw, api_version, first_run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (brand_id, product_id, content_hash)
            DO UPDATE SET last_seen_at = now()
            RETURNING snapshot_id
            """,
            (
                brand_id,
                product_id,
                content_hash,
                Jsonb(canonical),
                Jsonb(raw) if raw is not None else None,
                api_version,
                run_id,
            ),
        )
        return int(one(cur)["snapshot_id"])


def upsert_current(
    conn: Conn,
    *,
    brand_id: str,
    product_id: str,
    snapshot_id: int,
    category: str | None,
    name: str | None,
    description: str | None,
    headline_rate: Decimal | None,
    headline_rate_kind: str | None,
    changed: bool,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO product_current (brand_id, product_id, snapshot_id, product_category,
                name, description, headline_rate, headline_rate_kind, is_available,
                last_changed_at, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true, now(), now())
            ON CONFLICT (brand_id, product_id) DO UPDATE SET
                snapshot_id = EXCLUDED.snapshot_id,
                product_category = EXCLUDED.product_category,
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                headline_rate = EXCLUDED.headline_rate,
                headline_rate_kind = EXCLUDED.headline_rate_kind,
                is_available = true,
                last_changed_at = CASE WHEN %s THEN now() ELSE product_current.last_changed_at END,
                last_seen_at = now()
            """,
            (
                brand_id,
                product_id,
                snapshot_id,
                category,
                (name or "")[:500] or None,
                (description or "")[:2000] or None,
                headline_rate,
                headline_rate_kind,
                changed,
            ),
        )


def mark_withdrawn(conn: Conn, brand_id: str, product_ids: list[str]) -> None:
    if not product_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE product_current SET is_available = false, last_changed_at = now()"
            " WHERE brand_id = %s AND product_id = ANY(%s) AND is_available",
            (brand_id, product_ids),
        )


def current_product_ids(conn: Conn, brand_id: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT product_id FROM product_current WHERE brand_id = %s AND is_available",
            (brand_id,),
        )
        return {row["product_id"] for row in cur.fetchall()}


def insert_changes(
    conn: Conn,
    *,
    brand_id: str,
    product_id: str,
    changes: list[Change],
    from_snapshot_id: int | None,
    to_snapshot_id: int | None,
    run_id: int,
    observed_after: datetime,
    observed_before: datetime,
) -> int:
    if not changes:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO product_change (brand_id, product_id, change_type, field_path,
                old_value, new_value, delta_bps, from_snapshot_id, to_snapshot_id,
                run_id, observed_after, observed_before)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    brand_id,
                    product_id,
                    str(c.change_type),
                    c.field_path,
                    Jsonb(c.old_value) if c.old_value is not None else None,
                    Jsonb(c.new_value) if c.new_value is not None else None,
                    c.delta_bps,
                    from_snapshot_id,
                    to_snapshot_id,
                    run_id,
                    observed_after,
                    observed_before,
                )
                for c in changes
            ],
        )
    return len(changes)


def quarantine(
    conn: Conn,
    *,
    brand_id: str,
    product_id: str | None,
    run_id: int,
    reason: str,
    error_detail: str,
    raw: Any,
) -> None:
    """Never drop a payload we could not parse. It is evidence, not noise."""
    is_json = isinstance(raw, (dict, list))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO quarantine (brand_id, product_id, run_id, reason, error_detail, raw, raw_text)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                brand_id,
                product_id,
                run_id,
                reason,
                error_detail[:2000],
                Jsonb(raw) if is_json else None,
                None if is_json else str(raw)[:100_000],
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------
# backup
#
# Lambda has no pg_dump, so backups are taken with SQL COPY instead. The output
# is plain CSV per table: restorable with \copy, readable without this codebase,
# and not dependent on a Postgres version matching.
# --------------------------------------------------------------------------
BACKUP_TABLES: tuple[str, ...] = (
    "schema_migration",
    "brand",
    "collection_run",
    "collection_run_brand",
    "product_snapshot",
    "product_current",
    "product_change",
    "quarantine",
)


class ByteSink(Protocol):
    """Anything bytes can be written to -- a file, a gzip stream, a buffer.

    Takes a buffer rather than bytes specifically: psycopg's COPY yields
    memoryview chunks, and copying each one into bytes purely to satisfy a type
    annotation would be real work done for no reason.
    """

    def write(self, data: Buffer, /) -> int: ...


def copy_table_csv(conn: Conn, table: str, sink: ByteSink) -> int:
    """Stream one table out as CSV. Returns bytes written.

    The table name is interpolated into SQL, so it is checked against the fixed
    list above rather than trusted -- a habit worth keeping even where the only
    caller passes a constant.
    """
    if table not in BACKUP_TABLES:
        raise ValueError(f"{table!r} is not a backup table")

    written = 0
    with (
        conn.cursor() as cur,
        cur.copy(f"COPY {table} TO STDOUT WITH (FORMAT csv, HEADER)") as copy,
    ):
        for chunk in copy:
            sink.write(chunk)
            written += len(chunk)
    return written


def row_counts(conn: Conn) -> dict[str, int]:
    """Row count per table, recorded alongside a backup.

    A backup nobody can verify is a backup nobody should trust: these counts are
    what a restore is checked against.
    """
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in BACKUP_TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            counts[table] = int(one(cur)["n"])
    return counts


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------
def health_snapshot(conn: Conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT max(finished_at) FROM collection_run WHERE status = 'completed') AS last_completed_run,
              (SELECT count(*) FROM brand WHERE is_enabled) AS brands_enabled,
              (SELECT count(*) FROM brand WHERE is_enabled AND consecutive_failures >= 5) AS brands_failing,
              (SELECT count(*) FROM brand WHERE circuit_open_until > now()) AS circuits_open,
              (SELECT count(*) FROM product_current WHERE is_available) AS products_tracked,
              (SELECT count(*) FROM product_change WHERE detected_at > now() - interval '7 days') AS changes_7d,
              (SELECT count(*) FROM quarantine WHERE resolved_at IS NULL) AS quarantine_open,
              (SELECT count(*) FROM brand b WHERE b.is_enabled AND NOT EXISTS (
                   SELECT 1 FROM product_current c
                   WHERE c.brand_id = b.brand_id AND c.is_available
               )) AS brands_without_products
            """
        )
        return one(cur)


def json_default(value: Any) -> str:
    if isinstance(value, (Decimal, datetime)):
        return str(value)
    raise TypeError(f"not serialisable: {type(value)!r}")


def dumps(value: Any) -> str:
    return json.dumps(value, indent=2, default=json_default)
