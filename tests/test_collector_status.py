"""Run-status rules. These encode ADR-0005: every outcome is recorded, and a
brand that collected nothing is not a success, however politely it failed."""

from __future__ import annotations

from rateradar.client import ErrorKind, FetchError


def classify(products_total: int, products_failed: int) -> str:
    """Mirrors the rule in collector._collect_brand."""
    if not products_failed:
        return "ok"
    return "failed" if products_failed == products_total else "partial"


def test_no_failures_is_ok():
    assert classify(32, 0) == "ok"


def test_some_failures_is_partial():
    assert classify(32, 3) == "partial"


def test_every_product_failing_is_a_failure_not_a_partial_success():
    """The Westpac case: 32 products listed, 32 details failed, nothing stored.
    Recording that as 'partial' hides it from the circuit breaker and the health
    check, which is how a brand silently stops being collected."""
    assert classify(32, 32) == "failed"


def test_fetch_error_carries_the_kind_and_detail_for_the_ledger():
    error = FetchError(ErrorKind.HTTP_5XX, "429 rate limited", 429)
    assert error.kind == "http_5xx"
    assert "429" in error.detail
