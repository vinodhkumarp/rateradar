"""The differ turns bytes into meaning. If it is wrong, the dataset is wrong."""

from __future__ import annotations

from decimal import Decimal

from rateradar.differ import ChangeType, diff_products, summarise


def base():
    return {
        "productId": "TD-001",
        "name": "Term Deposit",
        "depositRates": [{"depositRateType": "FIXED", "rate": "0.045000"}],
        "fees": [{"feeType": "PERIODIC", "name": "Monthly fee", "amount": "5.00"}],
        "eligibility": [{"eligibilityType": "MIN_AGE", "additionalValue": "18"}],
    }


def test_identical_documents_produce_no_changes():
    assert diff_products(base(), base()) == []


def test_new_product():
    changes = diff_products(None, base())
    assert [c.change_type for c in changes] == [ChangeType.PRODUCT_ADDED]


def test_reappearance_is_distinguished_from_a_new_product():
    changes = diff_products(None, base(), previously_withdrawn=True)
    assert changes[0].change_type == ChangeType.PRODUCT_REAPPEARED


def test_withdrawal():
    changes = diff_products(base(), None)
    assert [c.change_type for c in changes] == [ChangeType.PRODUCT_WITHDRAWN]


def test_rate_cut_is_classified_and_measured_in_basis_points():
    new = base()
    new["depositRates"][0]["rate"] = "0.043000"
    (change,) = diff_products(base(), new)
    assert change.change_type == ChangeType.RATE_CHANGED
    assert change.field_path == "/depositRates/0/rate"
    assert change.delta_bps == Decimal("-20.00")


def test_rate_rise_has_positive_delta():
    new = base()
    new["depositRates"][0]["rate"] = "0.050000"
    (change,) = diff_products(base(), new)
    assert change.delta_bps == Decimal("50.00")


def test_fee_change_is_not_a_rate_change():
    new = base()
    new["fees"][0]["amount"] = "7.50"
    (change,) = diff_products(base(), new)
    assert change.change_type == ChangeType.FEE_CHANGED
    assert change.delta_bps is None


def test_eligibility_change():
    new = base()
    new["eligibility"][0]["additionalValue"] = "21"
    (change,) = diff_products(base(), new)
    assert change.change_type == ChangeType.ELIGIBILITY_CHANGED


def test_unknown_field_is_reported_not_swallowed():
    new = base()
    new["someNewFieldTheStandardAdded"] = "surprise"
    (change,) = diff_products(base(), new)
    assert change.change_type == ChangeType.OTHER_CHANGED
    assert change.field_path == "/someNewFieldTheStandardAdded"


def test_added_array_element_is_detected():
    new = base()
    new["depositRates"].append({"depositRateType": "BONUS", "rate": "0.010000"})
    changes = diff_products(base(), new)
    assert any(c.change_type == ChangeType.RATE_CHANGED for c in changes)


def test_pathological_diff_is_collapsed_to_one_event():
    new = {"productId": "TD-001", **{f"field{i}": i for i in range(300)}}
    changes = diff_products(base(), new, max_changes=50)
    assert len(changes) == 1
    assert changes[0].change_type == ChangeType.OTHER_CHANGED
    assert changes[0].old_value["truncated"] is True


def test_summarise_counts_by_type():
    new = base()
    new["depositRates"][0]["rate"] = "0.040000"
    new["fees"][0]["amount"] = "9.00"
    assert summarise(diff_products(base(), new)) == {"RATE_CHANGED": 1, "FEE_CHANGED": 1}


def test_both_none_is_a_no_op():
    assert diff_products(None, None) == []
