"""AWS Lambda entry point.

The collector knows nothing about Lambda: this module is a thin adapter that
turns an event into the same operations the CLI exposes. That the adapter is
this small is the point ADR-0002 claimed and ADR-0006 actually tested — moving
from a GitHub cron to Sydney changed no collector code at all.

Events:
    {"command": "collect"}    default; one collection pass
    {"command": "discover"}   weekly refresh of the brand registry
    {"command": "migrate"}    apply pending migrations only
    {"command": "backup"}     dump every table to S3 as gzipped CSV

Anything else is rejected rather than assumed, so a malformed schedule fails
loudly instead of quietly doing nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import psycopg

from . import db, logs, register
from .collector import run_collection
from .config import Settings

logs.configure(json_output=True)
log = logging.getLogger("rateradar")

VALID_COMMANDS = frozenset({"collect", "discover", "migrate", "backup"})


@lru_cache(maxsize=1)
def _load_settings() -> Settings:
    """Build settings, resolving the connection string from Parameter Store.

    The secret is deliberately not a Lambda environment variable: those are
    visible to anyone who can read the function's configuration. Cached for the
    life of the execution environment, so warm invocations cost no API call.
    """
    parameter = os.environ.get("RATERADAR_DATABASE_URL_PARAM")
    if parameter and not os.environ.get("RATERADAR_DATABASE_URL"):
        import boto3  # provided by the Lambda runtime; not a project dependency

        client = boto3.client("ssm")
        value = client.get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]
        os.environ["RATERADAR_DATABASE_URL"] = value
        log.info("loaded database url from %s", parameter)

    return Settings()


def _collect(settings: Settings) -> dict[str, Any]:
    with db.connect(settings) as conn:
        db.migrate(conn, settings.migrations_dir)
        result = asyncio.run(
            run_collection(
                conn,
                settings,
                trigger="schedule",
                git_sha=os.environ.get("RATERADAR_GIT_SHA"),
            )
        )
        health = db.health_snapshot(conn)
    return {"run": result, "health": json.loads(db.dumps(health))}


def _discover(settings: Settings) -> dict[str, Any]:
    brands, source = register.discover(settings)
    resolution = register.resolve_allowlist(register.load_allowlist(settings), brands)
    with db.connect(settings) as conn:
        upserted = db.upsert_brands(conn, brands)
        enabled = db.enable_brands(conn, resolution.brand_ids)

    # Unresolved allowlist entries must reach CloudWatch, not just a local
    # terminal: an entry that silently matches nothing is how coverage shrinks
    # without anyone noticing.
    if resolution.unmatched or resolution.ambiguous:
        log.warning(
            "allowlist unresolved: unmatched=%s ambiguous=%s",
            resolution.unmatched,
            sorted(resolution.ambiguous),
        )
    return {
        "source": source,
        "brands_upserted": upserted,
        "brands_enabled": enabled,
        "unmatched": resolution.unmatched,
        "ambiguous": sorted(resolution.ambiguous),
    }


def _backup(settings: Settings) -> dict[str, Any]:
    """Dump every table to S3 as gzipped CSV, with a manifest.

    Lambda has no pg_dump, so this uses SQL COPY. The result is plain CSV:
    restorable with \\copy, readable without this codebase, and indifferent to
    which Postgres version wrote it.

    The dataset cannot be re-collected retrospectively -- a month of rate
    movements exists nowhere else -- so this is the difference between a free
    database tier being an inconvenience and being a catastrophe.
    """
    bucket = os.environ.get("RATERADAR_BACKUP_BUCKET")
    if not bucket:
        # Fail loudly: a backup that silently does not happen is the worst of
        # both worlds -- no copy, and the belief that there is one.
        raise RuntimeError("RATERADAR_BACKUP_BUCKET is not set")

    import gzip
    import io

    import boto3

    taken_at = datetime.now(UTC)
    prefix = f"backups/{taken_at:%Y/%m/%d}/{taken_at:%H%M%S}"
    s3 = boto3.client("s3")
    files: list[dict[str, Any]] = []

    with db.connect(settings) as conn:
        counts = db.row_counts(conn)
        for table in db.BACKUP_TABLES:
            buffer = io.BytesIO()
            with gzip.GzipFile(fileobj=buffer, mode="wb") as compressed:
                raw_bytes = db.copy_table_csv(conn, table, compressed)

            body = buffer.getvalue()
            key = f"{prefix}/{table}.csv.gz"
            s3.put_object(Bucket=bucket, Key=key, Body=body)
            files.append({"table": table, "key": key, "rows": counts[table], "bytes": len(body)})
            log.info(
                "backed up %s: %s rows, %s bytes raw -> %s", table, counts[table], raw_bytes, key
            )

    # The manifest is what makes a restore verifiable rather than hopeful.
    manifest = {
        "taken_at": taken_at.isoformat(),
        "git_sha": os.environ.get("RATERADAR_GIT_SHA"),
        "files": files,
        "total_rows": sum(f["rows"] for f in files),
        "total_bytes": sum(f["bytes"] for f in files),
    }
    s3.put_object(
        Bucket=bucket,
        Key=f"{prefix}/manifest.json",
        Body=json.dumps(manifest, indent=2).encode(),
        ContentType="application/json",
    )
    return manifest


def _migrate(settings: Settings) -> dict[str, Any]:
    with db.connect(settings) as conn:
        return {"migrations_applied": db.migrate(conn, settings.migrations_dir)}


def handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    command = (event or {}).get("command", "collect")
    if command not in VALID_COMMANDS:
        raise ValueError(f"unknown command {command!r}; expected one of {sorted(VALID_COMMANDS)}")

    settings = _load_settings()
    request_id = getattr(context, "aws_request_id", None)
    log.info("starting", extra={"command": command, "request_id": request_id, "event": "start"})

    try:
        if command == "collect":
            result = _collect(settings)
        elif command == "discover":
            result = _discover(settings)
        elif command == "backup":
            result = _backup(settings)
        else:
            result = _migrate(settings)
    except psycopg.OperationalError as exc:
        # Infrastructure, not a bank. Raise so the CloudWatch alarm fires.
        log.error("database unavailable: %s", exc)
        raise

    log.info(
        "finished",
        extra={
            "command": command,
            "request_id": request_id,
            "event": "finish",
            "result": result,
        },
    )
    return {"command": command, "ok": True, **result}
