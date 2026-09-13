"""Derive typed change events from two canonical product documents.

Pure, deterministic, no I/O. This is the piece that turns bytes into meaning,
and the piece most likely to need refinement as real payloads arrive -- so it is
isolated, cheap to test, and replayable over stored history.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class ChangeType(StrEnum):
    PRODUCT_ADDED = "PRODUCT_ADDED"
    PRODUCT_WITHDRAWN = "PRODUCT_WITHDRAWN"
    PRODUCT_REAPPEARED = "PRODUCT_REAPPEARED"
    RATE_CHANGED = "RATE_CHANGED"
    FEE_CHANGED = "FEE_CHANGED"
    ELIGIBILITY_CHANGED = "ELIGIBILITY_CHANGED"
    FEATURE_CHANGED = "FEATURE_CHANGED"
    TERMS_CHANGED = "TERMS_CHANGED"
    OTHER_CHANGED = "OTHER_CHANGED"


@dataclass(frozen=True, slots=True)
class Change:
    change_type: ChangeType
    field_path: str | None = None
    old_value: Any = None
    new_value: Any = None
    delta_bps: Decimal | None = None


# Path prefix -> change type. First match wins, longest prefixes first.
_PREFIX_RULES: tuple[tuple[str, ChangeType], ...] = (
    ("/depositRates", ChangeType.RATE_CHANGED),
    ("/lendingRates", ChangeType.RATE_CHANGED),
    ("/fees", ChangeType.FEE_CHANGED),
    ("/eligibility", ChangeType.ELIGIBILITY_CHANGED),
    ("/features", ChangeType.FEATURE_CHANGED),
    ("/bundles", ChangeType.FEATURE_CHANGED),
    ("/constraints", ChangeType.TERMS_CHANGED),
    ("/additionalInformation", ChangeType.TERMS_CHANGED),
    ("/effectiveFrom", ChangeType.TERMS_CHANGED),
    ("/effectiveTo", ChangeType.TERMS_CHANGED),
    ("/applicationUri", ChangeType.TERMS_CHANGED),
)


def _classify(path: str) -> ChangeType:
    for prefix, change_type in _PREFIX_RULES:
        if path == prefix or path.startswith(prefix + "/"):
            return change_type
    # Unrecognised is reported, never swallowed. A growing pile of these is the
    # signal to extend the rules above.
    return ChangeType.OTHER_CHANGED


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _delta_bps(path: str, old: Any, new: Any) -> Decimal | None:
    """Basis-point movement, for rate fields only.

    CDR expresses rates as decimal fractions (5.25% -> "0.0525"), so one basis
    point is 0.0001.
    """
    if not path.endswith(("/rate", "/comparisonRate")):
        return None
    old_d, new_d = _to_decimal(old), _to_decimal(new)
    if old_d is None or new_d is None:
        return None
    return ((new_d - old_d) * Decimal(10_000)).quantize(Decimal("0.01"))


def _walk(old: Any, new: Any, path: str, out: list[Change], limit: int) -> None:
    if len(out) >= limit:
        return

    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            _walk(old.get(key), new.get(key), f"{path}/{key}", out, limit)
        return

    if isinstance(old, list) and isinstance(new, list):
        # Canonical arrays are sorted by natural key, so positional comparison is
        # meaningful. Length changes are reported at the element position.
        for index in range(max(len(old), len(new))):
            left = old[index] if index < len(old) else None
            right = new[index] if index < len(new) else None
            _walk(left, right, f"{path}/{index}", out, limit)
        return

    if old != new:
        out.append(
            Change(
                change_type=_classify(path),
                field_path=path or "/",
                old_value=old,
                new_value=new,
                delta_bps=_delta_bps(path, old, new),
            )
        )


def diff_products(
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
    *,
    previously_withdrawn: bool = False,
    max_changes: int = 200,
) -> list[Change]:
    """Compare two canonical product documents.

    old=None  -> the product is new (or has come back)
    new=None  -> the product has disappeared from the bank's catalogue
    """
    if old is None and new is None:
        return []

    if old is None:
        change_type = (
            ChangeType.PRODUCT_REAPPEARED if previously_withdrawn else ChangeType.PRODUCT_ADDED
        )
        return [Change(change_type=change_type, new_value=new)]

    if new is None:
        return [Change(change_type=ChangeType.PRODUCT_WITHDRAWN, old_value=old)]

    changes: list[Change] = []
    _walk(old, new, "", changes, max_changes)

    if len(changes) >= max_changes:
        # A diff this large is almost always a bank restructuring its payload,
        # not 200 independent decisions. Report it as one event and keep the
        # detail in the snapshots.
        return [
            Change(
                change_type=ChangeType.OTHER_CHANGED,
                field_path="/",
                old_value={"truncated": True, "changes": max_changes},
                new_value={"truncated": True, "changes": max_changes},
            )
        ]
    return changes


def summarise(changes: list[Change]) -> dict[str, int]:
    """Counts by type, for run summaries and health checks."""
    counts: dict[str, int] = {}
    for change in changes:
        counts[change.change_type] = counts.get(change.change_type, 0) + 1
    return counts
