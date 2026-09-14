"""Backup coverage.

The failure this guards against is specific and quiet: someone adds a table in
a migration, backups keep succeeding, and the new table is simply not in them.
Nobody finds out until a restore, which is the worst possible moment.
"""

from __future__ import annotations

import re
from pathlib import Path

from rateradar.config import REPO_ROOT
from rateradar.db import BACKUP_TABLES

CREATE_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)", re.IGNORECASE)


def tables_in_migrations() -> set[str]:
    found: set[str] = set()
    for path in sorted((REPO_ROOT / "migrations").glob("*.sql")):
        found.update(name.lower() for name in CREATE_TABLE.findall(path.read_text()))
    return found


def test_every_table_in_the_schema_is_backed_up():
    missing = tables_in_migrations() - set(BACKUP_TABLES)
    assert not missing, (
        f"tables exist in migrations but are not in BACKUP_TABLES: {sorted(missing)}. "
        "A backup that silently skips a table is worse than no backup."
    )


def test_backup_list_has_no_phantom_tables():
    """The reverse: a renamed or dropped table would fail every backup run."""
    phantom = set(BACKUP_TABLES) - tables_in_migrations()
    assert not phantom, f"BACKUP_TABLES names tables that no longer exist: {sorted(phantom)}"


def test_migrations_are_actually_found():
    """Guards the guard: a wrong path would make both tests vacuously pass."""
    assert len(tables_in_migrations()) >= 7
    assert Path(REPO_ROOT / "migrations").is_dir()
