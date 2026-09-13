"""Turn a bank's product payload into a canonical, diffable, hashable document.

Two products with the same economic meaning must produce the same bytes, or the
change feed fills with noise. Everything in this module exists to make that
true. It is pure: no I/O, no clock, no database -- which is why it is the most
heavily tested part of the codebase.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

# Bumped whenever canonicalisation changes meaning. Stored with each snapshot so
# that hashes produced by different normaliser versions are never compared.
NORMALISER_VERSION = 1

# Non-semantic fields. Their movement is not a product change.
VOLATILE_KEYS = frozenset(
    {
        "lastUpdated",
        "links",
        "meta",
        "self",
        "first",
        "prev",
        "next",
        "last",
        "totalRecords",
        "totalPages",
    }
)

# Fields carrying a rate as a decimal fraction (CDR RateString: 5.25% -> "0.0525")
RATE_KEYS = frozenset({"rate", "comparisonRate", "interestPaymentDue"})
# Fields carrying a money amount as a decimal string
AMOUNT_KEYS = frozenset(
    {
        "amount",
        "balanceRate",
        "transactionRate",
        "accruedRate",
        "minimumAmount",
        "maximumAmount",
        "minimumValue",
        "maximumValue",
        "additionalValue",
    }
)

RATE_SCALE = Decimal("0.00000001")  # 8dp: 1e-8 == 0.000001 bp, well below noise
AMOUNT_SCALE = Decimal("0.01")

# Natural keys used to give arrays a stable order. Banks reorder arrays freely;
# without this, every reorder would look like a change.
ARRAY_SORT_KEYS: dict[str, tuple[str, ...]] = {
    "depositRates": ("depositRateType", "additionalValue", "rate"),
    "lendingRates": ("lendingRateType", "loanPurpose", "repaymentType", "additionalValue"),
    "fees": ("feeType", "name", "additionalValue"),
    "features": ("featureType", "additionalValue"),
    "constraints": ("constraintType", "additionalValue"),
    "eligibility": ("eligibilityType", "additionalValue"),
    "bundles": ("name",),
    "tiers": ("unitOfMeasure", "minimumValue", "maximumValue", "name"),
    "additionalInfoUris": ("description", "additionalInfoUri"),
}


class NormalisationError(ValueError):
    """Raised when a payload cannot be canonicalised. Caller quarantines it."""


def _decimal_str(value: Any, scale: Decimal) -> Any:
    """Render a numeric value at fixed scale so '5.5' == '5.50'."""
    if value is None or isinstance(value, bool):
        return value
    try:
        quantised = Decimal(str(value)).quantize(scale)
    except (InvalidOperation, ArithmeticError, ValueError):
        return value  # not numeric after all; leave it alone, diff will show it
    return format(quantised.normalize(), "f")


def _sort_key(item: Any, keys: tuple[str, ...]) -> str:
    if not isinstance(item, dict):
        return json.dumps(item, sort_keys=True, default=str)
    return "\x00".join(str(item.get(k, "")) for k in keys)


def _canonicalise(node: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key in sorted(node):
            if key in VOLATILE_KEYS:
                continue
            value = node[key]
            if value is None or value == [] or value == {}:
                continue  # absent and empty are the same thing to a reader
            if key in RATE_KEYS:
                out[key] = _decimal_str(value, RATE_SCALE)
            elif key in AMOUNT_KEYS and not isinstance(value, (dict, list)):
                out[key] = _decimal_str(value, AMOUNT_SCALE)
            else:
                out[key] = _canonicalise(value, parent_key=key)
        return out

    if isinstance(node, list):
        items = [_canonicalise(i, parent_key=parent_key) for i in node]
        keys = ARRAY_SORT_KEYS.get(parent_key or "")
        if keys:
            items.sort(key=lambda i: _sort_key(i, keys))
        else:
            items.sort(key=lambda i: json.dumps(i, sort_keys=True, default=str))
        return items

    if isinstance(node, str):
        return node.strip()

    return node


def canonicalise(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical form of a product-detail payload.

    Accepts either the full envelope ({"data": {...}}) or the inner object.
    """
    if not isinstance(payload, dict):
        raise NormalisationError(f"expected object, got {type(payload).__name__}")

    product = payload.get("data", payload)
    if not isinstance(product, dict):
        raise NormalisationError("'data' is not an object")
    if not product.get("productId"):
        raise NormalisationError("productId missing")

    canonical = _canonicalise(product)
    if not isinstance(canonical, dict):  # pragma: no cover - defensive
        raise NormalisationError("canonical form is not an object")
    return canonical


def content_hash(canonical: dict[str, Any]) -> str:
    """Stable SHA-256 over the canonical form."""
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def headline_rate(canonical: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    """Best deposit rate, or lowest lending rate, for cheap querying.

    Deliberately simple: a headline figure for sorting and dashboards, never the
    basis of a comparison claim. The full rate structure stays in the canonical
    document.
    """

    def _rates(entries: Any) -> list[Decimal]:
        found: list[Decimal] = []
        if not isinstance(entries, list):
            return found
        for entry in entries:
            if isinstance(entry, dict) and entry.get("rate") is not None:
                try:
                    found.append(Decimal(str(entry["rate"])))
                except (InvalidOperation, ValueError):
                    continue
        return found

    deposits = _rates(canonical.get("depositRates"))
    lendings = _rates(canonical.get("lendingRates"))

    if deposits:
        return max(deposits), "deposit"
    if lendings:
        return min(lendings), "lending"
    return None, None
