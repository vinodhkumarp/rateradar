"""The normaliser decides what counts as a change. These are the tests that
stop the change feed filling with noise."""

from __future__ import annotations

import pytest

from rateradar.normalise import (
    NormalisationError,
    canonicalise,
    content_hash,
    headline_rate,
)


def product(**overrides):
    base = {
        "productId": "SAV-001",
        "productCategory": "TRANS_AND_SAVINGS_ACCOUNTS",
        "name": "Everyday Saver",
        "lastUpdated": "2026-09-01T00:00:00Z",
        "depositRates": [
            {"depositRateType": "VARIABLE", "rate": "0.0450"},
            {"depositRateType": "BONUS", "rate": "0.0100"},
        ],
    }
    base.update(overrides)
    return {"data": base}


def test_accepts_envelope_or_bare_object():
    assert canonicalise(product()) == canonicalise(product()["data"])


def test_volatile_fields_do_not_change_the_hash():
    a = canonicalise(product(lastUpdated="2026-09-01T00:00:00Z"))
    b = canonicalise(product(lastUpdated="2026-09-11T23:59:59Z"))
    assert content_hash(a) == content_hash(b)


def test_array_reordering_does_not_change_the_hash():
    reordered = product(
        depositRates=[
            {"depositRateType": "BONUS", "rate": "0.0100"},
            {"depositRateType": "VARIABLE", "rate": "0.0450"},
        ]
    )
    assert content_hash(canonicalise(product())) == content_hash(canonicalise(reordered))


def test_numeric_formatting_is_normalised():
    a = canonicalise(product(depositRates=[{"depositRateType": "VARIABLE", "rate": "0.045"}]))
    b = canonicalise(product(depositRates=[{"depositRateType": "VARIABLE", "rate": "0.04500"}]))
    assert content_hash(a) == content_hash(b)


def test_a_real_rate_move_does_change_the_hash():
    a = canonicalise(product())
    b = canonicalise(product(depositRates=[{"depositRateType": "VARIABLE", "rate": "0.0425"}]))
    assert content_hash(a) != content_hash(b)


def test_empty_and_absent_are_equivalent():
    a = canonicalise(product(features=[]))
    b = canonicalise(product())
    assert content_hash(a) == content_hash(b)


def test_whitespace_is_stripped():
    a = canonicalise(product(name="  Everyday Saver  "))
    assert a["name"] == "Everyday Saver"


def test_missing_product_id_is_rejected_not_guessed():
    with pytest.raises(NormalisationError):
        canonicalise({"data": {"name": "no id"}})


def test_non_object_payload_is_rejected():
    with pytest.raises(NormalisationError):
        canonicalise(["not", "an", "object"])  # type: ignore[arg-type]


def test_headline_rate_prefers_best_deposit():
    rate, kind = headline_rate(canonicalise(product()))
    assert kind == "deposit"
    assert str(rate) == "0.045"


def test_headline_rate_uses_lowest_lending_rate_for_mortgages():
    mortgage = product(
        productCategory="RESIDENTIAL_MORTGAGES",
        depositRates=None,
        lendingRates=[
            {"lendingRateType": "VARIABLE", "rate": "0.0629"},
            {"lendingRateType": "FIXED", "rate": "0.0589"},
        ],
    )
    rate, kind = headline_rate(canonicalise(mortgage))
    assert kind == "lending"
    assert str(rate) == "0.0589"


def test_unparseable_rate_is_left_alone_rather_than_dropped():
    canonical = canonicalise(product(depositRates=[{"depositRateType": "X", "rate": "n/a"}]))
    assert canonical["depositRates"][0]["rate"] == "n/a"
