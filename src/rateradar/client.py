"""HTTP client for CDR product reference APIs.

The standard is a standard; the implementations are not uniform. Version
negotiation, retries, timeouts, concurrency caps and a per-brand circuit breaker
are normal operation here, not edge cases.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# CDR 406 bodies name the versions the holder supports, e.g.
#   "Value 3 is invalid for the x-v header. Versions available: 5"
# Wording varies between implementations, so match the numbers, not the sentence.
_VERSIONS_AVAILABLE = re.compile(r"versions?\s+(?:available|supported)\s*:?\s*([\d,\s and]+)")


def _advertised_versions(response: httpx.Response) -> set[int]:
    """Versions the bank says it supports, from the x-v header or the error body."""
    found: set[int] = set()

    header = response.headers.get("x-v")
    if header and header.isdigit():
        found.add(int(header))

    body = response.text[:2000].lower()
    for match in _VERSIONS_AVAILABLE.finditer(body):
        for token in re.split(r"[,\s]+|and", match.group(1)):
            if token.strip().isdigit():
                found.add(int(token.strip()))
    return found


class ErrorKind(str):
    TIMEOUT = "timeout"
    VERSION = "version"
    TRANSPORT = "transport"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    SCHEMA = "schema"


class FetchError(Exception):
    def __init__(self, kind: str, detail: str, status: int | None = None) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.status = status


@dataclass(slots=True)
class BrandStats:
    """Per-brand counters for one run. Written to collection_run_brand."""

    http_requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    api_version: int | None = None
    errors: list[str] = field(default_factory=list)


class CDRClient:
    """Async client for one collection run across many brands."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._global_sem = asyncio.Semaphore(settings.global_concurrency)
        self._host_sems: dict[str, asyncio.Semaphore] = {}
        # Negotiated version per (host, endpoint). Per HOST is wrong: Westpac
        # serves the product list at v5 and product detail at v7, so a host-wide
        # cache sends every detail call down the wrong path.
        self._endpoint_version: dict[str, int] = {}
        # Endpoints where negotiation is already known to be hopeless. Without
        # this, one unsupported endpoint re-runs the whole version walk for every
        # single product -- 128 pointless requests at one bank, every run.
        self._endpoint_dead: dict[str, str] = {}
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_s,
                read=settings.read_timeout_s,
                write=settings.read_timeout_s,
                pool=settings.read_timeout_s,
            ),
            headers={"Accept": "application/json", "User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        self._owns_client = client is None

    async def __aenter__(self) -> CDRClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _endpoint_key(url: str) -> str:
        """Identify the endpoint, not just the host: .../products vs .../products/{id}."""
        parsed = httpx.URL(url)
        path = parsed.path.rstrip("/")
        marker = "/banking/products"
        kind = "detail" if path.endswith(marker) is False and marker in path else "list"
        return f"{parsed.host}:{kind}"

    def _start_version(self, url: str) -> int:
        """Start from whatever this endpoint has already agreed to, if anything."""
        return self._endpoint_version.get(
            self._endpoint_key(url), self.settings.product_api_version
        )

    def _host_sem(self, url: str) -> asyncio.Semaphore:
        host = httpx.URL(url).host
        if host not in self._host_sems:
            self._host_sems[host] = asyncio.Semaphore(self.settings.per_host_concurrency)
        return self._host_sems[host]

    async def _sleep_backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                await asyncio.sleep(min(float(retry_after), self.settings.backoff_max_s))
                return
            except ValueError:
                pass
        delay = min(self.settings.backoff_base_s * (2**attempt), self.settings.backoff_max_s)
        await asyncio.sleep(delay * (0.5 + random.random() / 2))  # jitter

    def _choose_version(self, advertised: set[int], tried: set[int]) -> int | None:
        """Pick the next x-v to try: the best version the bank actually offers.

        Falls back to stepping down one version at a time when the bank says
        nothing useful. Returns None once there is nothing left to try, which is
        a real finding -- that bank has moved beyond what this collector parses.
        """
        floor = self.settings.product_api_min_version
        ceiling = self.settings.product_api_max_version

        if advertised:
            # The bank told us what it serves; that beats anything we assumed,
            # even above our blind-stepping ceiling (Westpac detail is v7). If
            # none of what it offers is usable, stop -- walking the range anyway
            # is just noise on someone else's API.
            usable = {
                v for v in advertised if 1 <= v <= self.settings.product_api_sanity_cap
            } - tried
            return max(usable) if usable else None

        remaining = set(range(floor, ceiling + 1)) - tried
        return max(remaining) if remaining else None

    async def get_json(
        self, url: str, *, params: dict[str, Any] | None, stats: BrandStats, version: int
    ) -> tuple[dict[str, Any], int]:
        """GET with version negotiation and retries. Returns (payload, version served)."""
        attempt = 0
        current_version = version
        tried_versions: set[int] = set()

        dead = self._endpoint_dead.get(self._endpoint_key(url))
        if dead:
            # Established on an earlier product; do not re-litigate it 31 more times.
            raise FetchError(ErrorKind.VERSION, dead, 406)

        while True:
            retry_after: str | None = None
            async with self._global_sem, self._host_sem(url):
                stats.http_requests += 1
                headers = {
                    "x-v": str(current_version),
                    "x-min-v": str(self.settings.product_api_min_version),
                }
                try:
                    response = await self._client.get(url, params=params, headers=headers)
                except httpx.TimeoutException as exc:
                    kind, detail = ErrorKind.TIMEOUT, str(exc) or "timeout"
                except httpx.HTTPError as exc:
                    kind, detail = ErrorKind.TRANSPORT, str(exc) or exc.__class__.__name__
                else:
                    # 406: this bank does not speak the version we asked for. It
                    # usually says which ones it does speak, so read that rather
                    # than guessing -- and be willing to negotiate UP, since banks
                    # move to newer versions of the standard on their own schedule.
                    if response.status_code == 406:
                        tried_versions.add(current_version)
                        advertised = _advertised_versions(response)
                        next_version = self._choose_version(advertised, tried_versions)
                        if next_version is None:
                            detail = (
                                f"no usable API version: tried {sorted(tried_versions)}, "
                                f"bank offers {sorted(advertised) or 'unknown'}"
                            )
                            self._endpoint_dead[self._endpoint_key(url)] = detail
                            raise FetchError(ErrorKind.VERSION, detail, 406)
                        log.info(
                            "version negotiation: x-v=%s -> %s for %s (bank offers %s)",
                            current_version,
                            next_version,
                            url,
                            sorted(advertised) or "unknown",
                        )
                        current_version = next_version
                        continue

                    if response.status_code == 429:
                        stats.rate_limited += 1
                        kind, detail = ErrorKind.HTTP_5XX, "429 rate limited"
                    elif 500 <= response.status_code < 600:
                        kind, detail = ErrorKind.HTTP_5XX, f"{response.status_code}"
                    elif 400 <= response.status_code < 500:
                        # Not retryable. Fail fast and record it.
                        raise FetchError(
                            ErrorKind.HTTP_4XX,
                            f"{response.status_code} {response.text[:200]}",
                            response.status_code,
                        )
                    else:
                        try:
                            payload = response.json()
                        except ValueError as exc:
                            raise FetchError(ErrorKind.SCHEMA, f"invalid json: {exc}") from exc
                        served = response.headers.get("x-v")
                        served_version = (
                            int(served) if served and served.isdigit() else current_version
                        )
                        stats.api_version = served_version
                        self._endpoint_version[self._endpoint_key(url)] = served_version
                        return payload, served_version

                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")

            attempt += 1
            if attempt > self.settings.max_retries:
                stats.errors.append(detail)
                raise FetchError(kind, f"{detail} after {attempt} attempts")
            stats.retries += 1
            await self._sleep_backoff(attempt, retry_after)

    async def list_products(
        self, base_uri: str, category: str, *, stats: BrandStats
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield product summaries for one category, following pagination."""
        url = f"{base_uri.rstrip('/')}/cds-au/v1/banking/products"
        page = 1
        version = self._start_version(url)

        while True:
            payload, version = await self.get_json(
                url,
                params={
                    "product-category": category,
                    "page": page,
                    "page-size": self.settings.page_size,
                },
                stats=stats,
                version=version,
            )
            data = payload.get("data") or {}
            products = data.get("products") or []
            if not isinstance(products, list):
                raise FetchError(ErrorKind.SCHEMA, "data.products is not a list")

            for product in products:
                if isinstance(product, dict) and product.get("productId"):
                    yield product

            meta = payload.get("meta") or {}
            total_pages = meta.get("totalPages")
            if not isinstance(total_pages, int) or page >= total_pages or not products:
                return
            page += 1

    async def get_product_detail(
        self, base_uri: str, product_id: str, *, stats: BrandStats
    ) -> dict[str, Any]:
        url = f"{base_uri.rstrip('/')}/cds-au/v1/banking/products/{product_id}"
        payload, _ = await self.get_json(
            url, params=None, stats=stats, version=self._start_version(url)
        )
        return payload
