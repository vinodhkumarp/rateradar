#!/usr/bin/env python3
"""Print shell exports for Grafana's datasource, derived from the project's DSN.

The connection string already lives in .env, which is gitignored. Re-typing the
password into a second file would double the number of places it can leak from,
so this prints `export` lines for `make dash-neon` to eval: the credential exists
only in the environment of the compose command that consumes it, and nothing new
is written to disk.

Reads RATERADAR_GRAFANA_DATABASE_URL if set, falling back to
RATERADAR_DATABASE_URL. Prefer the former, pointing at a read-only role -- see
docs/operations.md, "Pointing the dashboard at Neon".

Values come from the real environment first, then from .env. .env is parsed here
rather than sourced by the shell: a Neon connection string contains "&" and a
User-Agent contains "(", both of which the shell interprets, so `. ./.env` either
backgrounds the assignment or dies on a syntax error -- silently, which is how
the dashboard ended up pointed at an empty local database.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv file. No interpolation, no shell -- KEY=VALUE and nothing else."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        # Strip one layer of matching quotes; leave everything else verbatim.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def lookup(name: str, file_values: dict[str, str]) -> str | None:
    """Real environment wins over .env, matching pydantic-settings' precedence."""
    return os.environ.get(name) or file_values.get(name)


def derive(dsn: str) -> dict[str, str]:
    parts = urlsplit(dsn)
    if not parts.hostname:
        raise ValueError("no host in connection string")

    # Neon terminates TLS at the endpoint and refuses plaintext, so anything that
    # is not the local compose Postgres defaults to require rather than disable.
    # An explicit sslmode in the DSN always wins.
    default_ssl = "disable" if parts.hostname in LOCAL_HOSTS else "require"
    sslmode = parse_qs(parts.query).get("sslmode", [default_ssl])[0]

    return {
        "RATERADAR_DB_HOST": parts.hostname,
        "RATERADAR_DB_PORT": str(parts.port or 5432),
        "RATERADAR_DB_NAME": unquote(parts.path.lstrip("/")) or "rateradar",
        "RATERADAR_DB_USER": unquote(parts.username or ""),
        "RATERADAR_DB_PASSWORD": unquote(parts.password or ""),
        "RATERADAR_DB_SSLMODE": sslmode,
    }


def main() -> int:
    file_values = read_env_file(ENV_FILE)
    dsn = lookup("RATERADAR_GRAFANA_DATABASE_URL", file_values) or lookup(
        "RATERADAR_DATABASE_URL", file_values
    )
    if not dsn:
        print(
            "neither RATERADAR_GRAFANA_DATABASE_URL nor RATERADAR_DATABASE_URL is set "
            f"in the environment or {ENV_FILE}; see .env.example",
            file=sys.stderr,
        )
        return 1

    try:
        values = derive(dsn)
    except ValueError as exc:
        # Never echo the string itself: it contains the password.
        print(f"could not parse the connection string: {exc}", file=sys.stderr)
        return 1

    if not values["RATERADAR_DB_USER"]:
        print("connection string has no username", file=sys.stderr)
        return 1

    if values["RATERADAR_DB_HOST"] in LOCAL_HOSTS:
        # Inside the Grafana container, "localhost" is the container itself --
        # not the machine. Pointing Grafana at a host-local database this way
        # produces a connection refused that looks like a credential problem.
        print(
            f"warning: {values['RATERADAR_DB_HOST']} resolves to the Grafana "
            "container, not this machine -- use `make dash` for the local stack",
            file=sys.stderr,
        )

    for key, value in values.items():
        print(f"export {key}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
