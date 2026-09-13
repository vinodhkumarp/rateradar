"""Discovery must degrade rather than silently shrink the dataset."""

from __future__ import annotations

import httpx
import pytest

from rateradar import register
from rateradar.config import Settings
from rateradar.register import DiscoveryError, _clean_base_uri, fetch_register_brands

SETTINGS = Settings()


def register_payload():
    return {
        "data": [
            {
                "dataHolderBrandId": "brand-1",
                "brandName": "Example Bank",
                "industries": ["banking"],
                "publicBaseUri": "https://api.examplebank.com.au/",
                "legalEntity": {"legalEntityName": "Example Bank Ltd", "abn": "123"},
            },
            {
                "dataHolderBrandId": "energy-1",
                "brandName": "Example Energy",
                "industries": ["energy"],
                "publicBaseUri": "https://api.exampleenergy.com.au",
                "legalEntity": {},
            },
        ]
    }


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_only_banking_brands_are_kept_and_uris_normalised():
    client = client_for(lambda r: httpx.Response(200, json=register_payload()))
    brands = fetch_register_brands(SETTINGS, client=client)
    assert len(brands) == 1
    assert brands[0]["brand_id"] == "brand-1"
    assert brands[0]["public_base_uri"] == "https://api.examplebank.com.au"


def test_register_failure_raises_so_the_caller_can_fall_back():
    client = client_for(lambda r: httpx.Response(503))
    with pytest.raises(DiscoveryError):
        fetch_register_brands(SETTINGS, client=client)


def test_empty_register_is_treated_as_failure_not_as_zero_banks():
    client = client_for(lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(DiscoveryError):
        fetch_register_brands(SETTINGS, client=client)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.bank.com.au/cds-au/v1/banking/products", "https://api.bank.com.au"),
        ("https://api.bank.com.au/cds-au/v1", "https://api.bank.com.au"),
        ("https://api.bank.com.au//", "https://api.bank.com.au"),
        ("http://insecure.bank.com.au", None),
        ("", None),
    ],
)
def test_base_uri_cleaning(raw, expected):
    assert _clean_base_uri(raw) == expected


# ---------------------------------------------------------------------------
# Allowlist resolution: the allowlist is human-edited, so matching must be
# forgiving about form and strict about ambiguity.
# ---------------------------------------------------------------------------

BRANDS = [
    {"brand_id": "id-cba", "brand_name": "Commonwealth Bank of Australia"},
    {"brand_id": "id-stg", "brand_name": "St.George Bank"},
    {"brand_id": "id-bwa", "brand_name": "Bankwest"},
    {"brand_id": "id-boq", "brand_name": "Bank of Queensland Limited"},
    {"brand_id": "id-boqs", "brand_name": "BOQ Specialist"},
    {"brand_id": "id-mac", "brand_name": "Macquarie Bank Limited"},
    {"brand_id": "id-hsbc", "brand_name": "HSBC Bank Australia Limited"},
    {"brand_id": "id-banka", "brand_name": "Bank Australia"},
    {"brand_id": "id-sunb", "brand_name": "Suncorp Bank"},
    {"brand_id": "id-suni", "brand_name": "Suncorp Insurance"},
]


def resolve(*entries):
    return register.resolve_allowlist(list(entries), BRANDS)


def test_exact_name_match_ignores_case_and_punctuation():
    result = resolve("st george bank")
    assert result.brand_ids == ["id-stg"]
    assert result.matched["st george bank"] == "St.George Bank"


def test_legal_suffixes_are_ignored():
    assert resolve("Macquarie Bank").brand_ids == ["id-mac"]


def test_unique_substring_match():
    assert resolve("Commonwealth Bank").brand_ids == ["id-cba"]
    assert resolve("HSBC").brand_ids == ["id-hsbc"]


def test_brand_id_still_works_and_wins():
    assert resolve("id-bwa").brand_ids == ["id-bwa"]


def test_ambiguous_entry_is_reported_and_not_guessed():
    result = resolve("Suncorp")
    assert result.brand_ids == []
    assert result.ambiguous["Suncorp"] == ["Suncorp Bank", "Suncorp Insurance"]


def test_an_abbreviation_can_match_the_wrong_bank_but_says_so():
    """ "BOQ" matches only BOQ Specialist, because "Bank of Queensland Limited"
    contains no such word. The resolver cannot know that is not what you meant,
    so the report always names what it resolved to -- which is how you catch it."""
    result = resolve("BOQ")
    assert result.matched["BOQ"] == "BOQ Specialist"
    assert result.brand_ids == ["id-boqs"]


def test_unmatched_entry_is_reported_rather_than_dropped():
    result = resolve("Nonexistent Bank")
    assert result.brand_ids == []
    assert result.unmatched == ["Nonexistent Bank"]


def test_too_generic_entry_is_refused():
    result = resolve("Bank")
    assert result.brand_ids == []
    assert "Bank" in result.ambiguous


def test_bank_australia_is_not_swallowed_by_the_word_australia():
    assert resolve("Bank Australia").brand_ids == ["id-banka"]


def test_blank_lines_are_skipped():
    assert resolve("", "   ", "Bankwest").brand_ids == ["id-bwa"]


def test_mixed_list_reports_each_entry():
    result = resolve("Bankwest", "Macquarie Bank", "Nope Bank", "Bank")
    assert result.brand_ids == ["id-bwa", "id-mac"]
    assert result.unmatched == ["Nope Bank"]
    assert "Bank" in result.ambiguous
