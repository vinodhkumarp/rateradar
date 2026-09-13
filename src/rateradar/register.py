"""Discovery: which banks exist, and where are their product APIs.

Two independent sources (ADR-0004). Disagreement between them is logged as a
finding rather than silently resolved -- a shrinking dataset should never be
something you notice months later.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class DiscoveryError(RuntimeError):
    pass


def _clean_base_uri(uri: str) -> str | None:
    uri = (uri or "").strip().rstrip("/")
    if not uri:
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    # Some holders register the full products path; we want the base.
    for suffix in ("/cds-au/v1/banking/products", "/cds-au/v1", "/cds-au"):
        if uri.endswith(suffix):
            uri = uri[: -len(suffix)]
    return uri.rstrip("/")


def fetch_register_brands(
    settings: Settings, client: httpx.Client | None = None
) -> list[dict[str, Any]]:
    """Public, unauthenticated: the CDR Register's data holder brands summary."""
    url = f"{settings.register_base_url.rstrip('/')}{settings.register_brands_path}"
    owns = client is None
    client = client or httpx.Client(
        timeout=30.0, headers={"User-Agent": settings.user_agent, "Accept": "application/json"}
    )
    try:
        response = client.get(url, headers={"x-v": str(settings.register_api_version)})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DiscoveryError(f"register unavailable: {exc}") from exc
    finally:
        if owns:
            client.close()

    entries = payload.get("data")
    if not isinstance(entries, list):
        raise DiscoveryError("unexpected register response shape")

    brands: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        industries = entry.get("industries") or []
        if industries and "banking" not in [str(i).lower() for i in industries]:
            continue
        base_uri = _clean_base_uri(str(entry.get("publicBaseUri") or ""))
        brand_id = entry.get("dataHolderBrandId") or entry.get("brandId")
        if not base_uri or not brand_id:
            continue
        legal = entry.get("legalEntity") or {}
        brands.append(
            {
                "brand_id": str(brand_id),
                "brand_name": str(
                    entry.get("brandName") or legal.get("legalEntityName") or brand_id
                ),
                "legal_entity_name": legal.get("legalEntityName"),
                "abn": legal.get("abn"),
                "public_base_uri": base_uri,
                "industries": [str(i) for i in industries] or ["banking"],
            }
        )

    if not brands:
        raise DiscoveryError("register returned no usable banking brands")
    return brands


def fetch_fallback_endpoints(
    settings: Settings, client: httpx.Client | None = None
) -> list[dict[str, Any]]:
    """Community-maintained endpoint list, used when the register is unreachable."""
    owns = client is None
    client = client or httpx.Client(timeout=30.0, headers={"User-Agent": settings.user_agent})
    try:
        response = client.get(settings.fallback_endpoint_list_url)
        response.raise_for_status()
        lines = response.text.splitlines()
    except httpx.HTTPError as exc:
        raise DiscoveryError(f"fallback list unavailable: {exc}") from exc
    finally:
        if owns:
            client.close()

    brands: list[dict[str, Any]] = []
    for line in lines:
        base_uri = _clean_base_uri(line.split("#")[0])
        if not base_uri:
            continue
        host = urlparse(base_uri).netloc
        brands.append(
            {
                "brand_id": f"fallback:{host}",
                "brand_name": host,
                "legal_entity_name": None,
                "abn": None,
                "public_base_uri": base_uri,
                "industries": ["banking"],
            }
        )
    return brands


def discover(settings: Settings) -> tuple[list[dict[str, Any]], str]:
    """Return (brands, source). Falls back rather than failing the run."""
    try:
        brands = fetch_register_brands(settings)
        log.info("discovered %d banking brands from the CDR register", len(brands))
        return brands, "register"
    except DiscoveryError as exc:
        if not settings.fallback_endpoint_list_url:
            raise
        log.warning("register discovery failed (%s); trying fallback list", exc)
        brands = fetch_fallback_endpoints(settings)
        log.info("discovered %d endpoints from the fallback list", len(brands))
        return brands, "fallback"


def load_allowlist(settings: Settings) -> list[str]:
    """Entries naming the brands collected in v1 (names or ids).

    Widening coverage is a config change, not a code change.
    """
    path = settings.brand_allowlist_file
    if not path.exists():
        return []
    entries: list[str] = []
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            entries.append(line)
    return entries


# ---------------------------------------------------------------------------
# Allowlist resolution
#
# The allowlist is edited by a human, so it holds bank NAMES, not the register's
# opaque UUIDs. Ids are still accepted for the cases where a name is ambiguous.
# ---------------------------------------------------------------------------

_PUNCTUATION = str.maketrans({c: " " for c in ".,&'()-/"})

# Entries that would match half the register are refused rather than guessed at.
_TOO_GENERIC = frozenset({"bank", "banking", "mutual", "group", "credit union", "australia"})


def _normalise_name(name: str) -> str:
    """Fold the cosmetic differences between 'St.George' and 'St George Bank'."""
    text = name.lower().translate(_PUNCTUATION)
    words = [w for w in text.split() if w not in {"the", "limited", "ltd"}]
    return " ".join(words)


@dataclass(frozen=True)
class AllowlistResolution:
    """What the allowlist actually matched. Unmatched entries are the point:
    silently collecting 17 banks when you asked for 20 is the failure mode."""

    brand_ids: list[str]
    matched: dict[str, str]  # entry -> brand_name
    unmatched: list[str]
    ambiguous: dict[str, list[str]]  # entry -> candidate brand names


def resolve_allowlist(entries: list[str], brands: list[dict[str, Any]]) -> AllowlistResolution:
    """Match human-written allowlist entries to discovered brands.

    Precedence, most specific first:
      1. exact brand id
      2. exact name, ignoring case and punctuation
      3. a unique whole-word substring match ("Bankwest" -> "Bankwest Ltd")

    Anything matching several brands is reported rather than guessed at.
    """
    by_id = {b["brand_id"]: b for b in brands}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for brand in brands:
        by_name.setdefault(_normalise_name(str(brand["brand_name"])), []).append(brand)

    resolution = AllowlistResolution(brand_ids=[], matched={}, unmatched=[], ambiguous={})

    for entry in entries:
        raw = entry.strip()
        if not raw:
            continue

        if raw in by_id:
            brand = by_id[raw]
            resolution.brand_ids.append(brand["brand_id"])
            resolution.matched[raw] = str(brand["brand_name"])
            continue

        needle = _normalise_name(raw)
        if not needle or needle in _TOO_GENERIC:
            resolution.ambiguous[raw] = ["entry is too generic to match safely"]
            continue

        exact = by_name.get(needle, [])
        if len(exact) == 1:
            resolution.brand_ids.append(exact[0]["brand_id"])
            resolution.matched[raw] = str(exact[0]["brand_name"])
            continue
        if len(exact) > 1:
            resolution.ambiguous[raw] = [str(b["brand_name"]) for b in exact]
            continue

        # whole-word substring, so "anz" does not match "finanz"
        pattern = re.compile(rf"(?:^| ){re.escape(needle)}(?: |$)")
        partial = [b for n, bs in by_name.items() if pattern.search(n) for b in bs]
        if len(partial) == 1:
            resolution.brand_ids.append(partial[0]["brand_id"])
            resolution.matched[raw] = str(partial[0]["brand_name"])
        elif partial:
            resolution.ambiguous[raw] = sorted(str(b["brand_name"]) for b in partial)
        else:
            resolution.unmatched.append(raw)

    return resolution
