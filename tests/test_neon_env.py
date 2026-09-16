"""Deriving Grafana's datasource variables from the project's connection string.

This exists because the first version sourced .env with the shell, and a real
Neon DSN contains "&" while a real User-Agent contains "(" -- so the shell either
backgrounded the assignment or died on a syntax error, and Grafana silently fell
back to compose's local defaults and showed an empty dashboard. Every test here
is one of the characters that broke it.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "grafana" / "neon_env.py"
DATASOURCE = REPO_ROOT / "deploy" / "grafana" / "provisioning" / "datasources" / "postgres.yml"
DASHBOARD = REPO_ROOT / "deploy" / "grafana" / "dashboards" / "collector-health.json"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("neon_env", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


neon_env = _load()

NEON_DSN = (
    "postgresql://neondb_owner:pw@ep-royal-night-a72lv12f-pooler."
    "ap-southeast-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)


def test_neon_dsn_with_ampersand_query() -> None:
    values = neon_env.derive(NEON_DSN)
    assert values["RATERADAR_DB_HOST"].endswith(".aws.neon.tech")
    assert values["RATERADAR_DB_NAME"] == "neondb"
    assert values["RATERADAR_DB_USER"] == "neondb_owner"
    assert values["RATERADAR_DB_PORT"] == "5432"
    assert values["RATERADAR_DB_SSLMODE"] == "require"


def test_remote_host_defaults_to_require_without_an_explicit_sslmode() -> None:
    values = neon_env.derive("postgresql://u:p@ep-x.aws.neon.tech/db")
    assert values["RATERADAR_DB_SSLMODE"] == "require"


def test_local_host_defaults_to_disable() -> None:
    values = neon_env.derive("postgresql://rateradar:rateradar@localhost:5432/rateradar")
    assert values["RATERADAR_DB_SSLMODE"] == "disable"


def test_explicit_sslmode_wins_over_the_default() -> None:
    values = neon_env.derive("postgresql://u:p@localhost:5432/db?sslmode=require")
    assert values["RATERADAR_DB_SSLMODE"] == "require"


def test_password_is_percent_decoded() -> None:
    values = neon_env.derive("postgresql://u:p%40ss%24word@ep-x.aws.neon.tech/db")
    assert values["RATERADAR_DB_PASSWORD"] == "p@ss$word"


def test_missing_host_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(ValueError):
        neon_env.derive("postgresql:///db")


def test_env_file_parses_what_the_shell_could_not(tmp_path: Path) -> None:
    # Both lines below are verbatim shapes from the real .env: the "&" in the
    # DSN and the "(" in the User-Agent each break `. ./.env`.
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        f"RATERADAR_DATABASE_URL={NEON_DSN}\n"
        "RATERADAR_USER_AGENT=RateRadar/0.1 (+https://github.com/x/rateradar)\n"
        'export QUOTED="value"\n'
    )

    values = neon_env.read_env_file(env)
    assert values["RATERADAR_DATABASE_URL"] == NEON_DSN
    assert values["RATERADAR_USER_AGENT"] == "RateRadar/0.1 (+https://github.com/x/rateradar)"
    assert values["QUOTED"] == "value"


def test_missing_env_file_is_empty_not_an_error(tmp_path: Path) -> None:
    assert neon_env.read_env_file(tmp_path / "nope") == {}


def test_real_environment_beats_the_env_file() -> None:
    assert neon_env.lookup("PATH", {"PATH": "from-file"}) != "from-file"
    assert neon_env.lookup("RATERADAR_NOT_SET_ANYWHERE", {"RATERADAR_NOT_SET_ANYWHERE": "x"}) == "x"


def test_every_variable_the_datasource_reads_is_supplied_by_compose() -> None:
    """The original bug in one assertion: a variable Grafana reads and nothing sets."""
    body = "\n".join(
        line for line in DATASOURCE.read_text().splitlines() if not line.strip().startswith("#")
    )
    used = set(re.findall(r"\$\{?(RATERADAR_[A-Z_]+)\}?", body))
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    supplied = set(re.findall(r"^\s+(RATERADAR_[A-Z_]+):", compose, re.M))

    assert used, "the datasource should read its connection from the environment"
    assert used <= supplied, f"compose does not supply {sorted(used - supplied)}"
    assert used <= set(neon_env.derive(NEON_DSN)), "neon_env does not produce every variable"


def test_dashboard_and_datasource_agree_on_the_plugin_id() -> None:
    """Grafana resolves a panel's datasource by uid but picks the runner by type.

    The plugin id was renamed from "postgres" to "grafana-postgresql-datasource",
    so a dashboard naming the old id against a datasource registered under the new
    one runs fine through the HTTP API and renders nothing in the browser -- a
    split that costs an afternoon to find.
    """
    declared = re.search(r"^\s+type:\s*(\S+)\s*$", DATASOURCE.read_text(), re.M)
    assert declared, "the datasource must declare a type"
    ds_type = declared.group(1)

    dashboard = json.loads(DASHBOARD.read_text())
    refs = {json.dumps(panel.get("datasource"), sort_keys=True) for panel in dashboard["panels"]}
    for target in (t for panel in dashboard["panels"] for t in panel.get("targets", [])):
        refs.add(json.dumps(target.get("datasource"), sort_keys=True))

    assert len(refs) == 1, f"panels disagree about the datasource: {refs}"
    ref = json.loads(refs.pop())
    assert ref["type"] == ds_type, f"dashboard says {ref['type']!r}, datasource is {ds_type!r}"
    assert ref["uid"] == "rateradar-postgres"
