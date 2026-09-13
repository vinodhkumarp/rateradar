"""A database transaction must never be held open across network I/O.

The first CI run died on Neon's idle-in-transaction timeout: each brand opened
a transaction with its first read, then spent minutes fetching from banks
before writing. Locally it never showed, because local Postgres has no such
timeout and a laptop in Sydney talks to Australian banks far faster than a
GitHub runner in the US does.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from rateradar import collector as collector_module
from rateradar.collector import RunTotals, _collect_brand
from rateradar.config import Settings
from rateradar.db import BrandRow


class RecordingConn(MagicMock):
    """Records the order of rollback/commit against a shared event log."""


@pytest.fixture
def timeline() -> list[str]:
    return []


class StubClient:
    """Minimal stand-in for CDRClient that logs when the network is touched."""

    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline

    async def list_products(self, base_uri: str, category: str, *, stats: Any):
        self.timeline.append("http:list")
        if category == "TERM_DEPOSITS":
            yield {"productId": "P1", "productCategory": category}

    async def get_product_detail(self, base_uri: str, product_id: str, *, stats: Any):
        self.timeline.append("http:detail")
        return {"data": {"productId": product_id, "name": "Test"}}


def test_read_transaction_is_closed_before_any_http_call(monkeypatch, timeline):
    conn = MagicMock()
    conn.rollback.side_effect = lambda: timeline.append("rollback")
    conn.commit.side_effect = lambda: timeline.append("commit")

    monkeypatch.setattr(
        collector_module.db,
        "current_product_ids",
        lambda *a, **k: timeline.append("db:read") or set(),
    )
    for name in ("record_run_brand", "record_brand_health", "upsert_current"):
        monkeypatch.setattr(collector_module.db, name, lambda *a, **k: None)
    # returns a count, which the collector adds to the run totals
    monkeypatch.setattr(collector_module.db, "insert_changes", lambda *a, **k: 1)
    monkeypatch.setattr(collector_module.db, "latest_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(collector_module.db, "insert_snapshot", lambda *a, **k: 1)

    brand = BrandRow(brand_id="b1", brand_name="Test Bank", public_base_uri="https://x.example")
    asyncio.run(
        _collect_brand(
            StubClient(timeline),  # type: ignore[arg-type]
            conn,
            brand,
            run_id=1,
            settings=Settings(categories=("TERM_DEPOSITS",)),
            totals=RunTotals(),
        )
    )

    first_http = next(i for i, event in enumerate(timeline) if event.startswith("http:"))
    assert "rollback" in timeline[:first_http], (
        f"a transaction was still open when the network was first touched: {timeline}"
    )
    # and the writes that follow are committed
    assert "commit" in timeline[first_http:]
