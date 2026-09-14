"""AWS Lambda entry point.

The collector knows nothing about Lambda: this module is a thin adapter that
turns an event into the same operations the CLI exposes. That the adapter is
this small is the point ADR-0002 claimed and ADR-0006 actually tested — moving
from a GitHub cron to Sydney changed no collector code at all.

Events:
    {"command": "collect"}    default; one collection pass
    {"command": "discover"}   weekly refresh of the brand registry
    {"command": "migrate"}    apply pending migrations only

Anything else is rejected rather than assumed, so a malformed schedule fails
loudly instead of quietly doing nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from functools import lru_cache
from typing import Any

import psycopg

from . import db, register
from .collector import run_collection
from .config import Settings

log = logging.getLogger("rateradar")
logging.getLogger().setLevel(logging.INFO)
log.setLevel(logging.INFO)

VALID_COMMANDS = frozenset({"collect", "discover", "migrate"})


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


def _migrate(settings: Settings) -> dict[str, Any]:
    with db.connect(settings) as conn:
        return {"migrations_applied": db.migrate(conn, settings.migrations_dir)}


def handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    command = (event or {}).get("command", "collect")
    if command not in VALID_COMMANDS:
        raise ValueError(f"unknown command {command!r}; expected one of {sorted(VALID_COMMANDS)}")

    settings = _load_settings()
    log.info("rateradar %s starting", command)

    try:
        if command == "collect":
            result = _collect(settings)
        elif command == "discover":
            result = _discover(settings)
        else:
            result = _migrate(settings)
    except psycopg.OperationalError as exc:
        # Infrastructure, not a bank. Raise so the CloudWatch alarm fires.
        log.error("database unavailable: %s", exc)
        raise

    log.info("rateradar %s finished: %s", command, json.dumps(result, default=str))
    return {"command": command, "ok": True, **result}
