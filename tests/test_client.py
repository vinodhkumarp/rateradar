"""Client behaviour against banks that misbehave — which is all of them, sometimes.

Uses httpx's MockTransport, so these run offline and never touch a real bank.
"""

from __future__ import annotations

import httpx
import pytest

from rateradar.client import BrandStats, CDRClient, ErrorKind, FetchError
from rateradar.config import Settings

BASE = "https://api.examplebank.com.au"


def make_client(handler, **overrides) -> CDRClient:
    settings = Settings(
        max_retries=2, backoff_base_s=0.001, backoff_max_s=0.002, page_size=2, **overrides
    )
    transport = httpx.MockTransport(handler)
    injected = httpx.AsyncClient(
        transport=transport,
        headers={"Accept": "application/json", "User-Agent": settings.user_agent},
    )
    return CDRClient(settings, client=injected)


def products_page(ids: list[str], total_pages: int) -> dict:
    return {
        "data": {"products": [{"productId": i, "productCategory": "TERM_DEPOSITS"} for i in ids]},
        "meta": {"totalPages": total_pages, "totalRecords": len(ids)},
    }


async def test_pagination_is_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        ids = ["A", "B"] if page == 1 else ["C"]
        return httpx.Response(200, json=products_page(ids, total_pages=2))

    client = make_client(handler)
    stats = BrandStats()
    seen = [p["productId"] async for p in client.list_products(BASE, "TERM_DEPOSITS", stats=stats)]
    await client.aclose()
    assert seen == ["A", "B", "C"]


async def test_version_is_negotiated_down_on_406():
    seen_versions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        version = request.headers["x-v"]
        seen_versions.append(version)
        if version == "4":
            return httpx.Response(406)
        return httpx.Response(200, json=products_page(["A"], 1), headers={"x-v": version})

    client = make_client(handler, product_api_version=4, product_api_max_version=4)
    stats = BrandStats()
    [p async for p in client.list_products(BASE, "TERM_DEPOSITS", stats=stats)]
    await client.aclose()
    assert seen_versions == ["4", "3"]
    assert stats.api_version == 3


async def test_server_errors_are_retried_then_surface():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    client = make_client(handler)
    stats = BrandStats()
    with pytest.raises(FetchError) as exc:
        await client.get_product_detail(BASE, "A", stats=stats)
    await client.aclose()
    assert exc.value.kind == ErrorKind.HTTP_5XX
    assert calls["n"] == 3  # initial attempt plus two retries
    assert stats.retries == 2


async def test_transient_error_then_success_recovers():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"data": {"productId": "A"}})

    client = make_client(handler)
    payload = await client.get_product_detail(BASE, "A", stats=BrandStats())
    await client.aclose()
    assert payload["data"]["productId"] == "A"


async def test_client_errors_are_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="no such product")

    client = make_client(handler)
    with pytest.raises(FetchError) as exc:
        await client.get_product_detail(BASE, "GONE", stats=BrandStats())
    await client.aclose()
    assert exc.value.kind == ErrorKind.HTTP_4XX
    assert calls["n"] == 1


async def test_rate_limiting_is_counted_and_backed_off():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"data": {"productId": "A"}})

    client = make_client(handler)
    stats = BrandStats()
    await client.get_product_detail(BASE, "A", stats=stats)
    await client.aclose()
    assert stats.rate_limited == 1


async def test_invalid_json_is_a_schema_error_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client = make_client(handler)
    with pytest.raises(FetchError) as exc:
        await client.get_product_detail(BASE, "A", stats=BrandStats())
    await client.aclose()
    assert exc.value.kind == ErrorKind.SCHEMA


async def test_identifying_user_agent_is_sent():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"data": {"productId": "A"}})

    client = make_client(handler)
    await client.get_product_detail(BASE, "A", stats=BrandStats())
    await client.aclose()
    assert "RateRadar" in captured["user-agent"]


# ---------------------------------------------------------------------------
# Version negotiation, driven by what banks actually send back.
# ---------------------------------------------------------------------------

WESTPAC_406 = (
    '{ "errors":[ { "code": 406, "title": "Unsupported Version", "detail": '
    '"Value 3 is invalid for the x-v header. Versions available: 5", '
    '"meta": { "urn": "urn:au-cds:error:cds-all:Header/UnsupportedVersion" } } ] }'
)


async def test_negotiates_up_when_the_bank_only_serves_a_newer_version():
    """The real Westpac case: we ask for 5, a bank on 5 answers. If we ask lower,
    the 406 body tells us 5 is available and we must go UP, not down."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        version = request.headers["x-v"]
        seen.append(version)
        if version != "5":
            return httpx.Response(406, text=WESTPAC_406)
        return httpx.Response(200, json={"data": {"productId": "A"}}, headers={"x-v": "5"})

    client = make_client(handler, product_api_version=3, product_api_max_version=6)
    stats = BrandStats()
    await client.get_product_detail(BASE, "A", stats=stats)
    await client.aclose()
    assert seen == ["3", "5"]
    assert stats.api_version == 5


async def test_negotiated_version_is_reused_for_later_calls_on_the_same_host():
    """Without this, every product detail re-negotiates: two requests per product."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        version = request.headers["x-v"]
        seen.append(version)
        if version != "5":
            return httpx.Response(406, text=WESTPAC_406)
        return httpx.Response(200, json={"data": {"productId": "A"}}, headers={"x-v": "5"})

    client = make_client(handler, product_api_version=3, product_api_max_version=6)
    stats = BrandStats()
    for _ in range(3):
        await client.get_product_detail(BASE, "A", stats=stats)
    await client.aclose()
    assert seen == ["3", "5", "5", "5"]  # negotiated once, not once per call


async def test_steps_down_when_the_bank_says_nothing_useful():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-v"])
        if request.headers["x-v"] != "3":
            return httpx.Response(406, text="not acceptable")
        return httpx.Response(200, json={"data": {"productId": "A"}})

    client = make_client(
        handler, product_api_version=5, product_api_min_version=3, product_api_max_version=5
    )
    await client.get_product_detail(BASE, "A", stats=BrandStats())
    await client.aclose()
    assert seen == ["5", "4", "3"]


async def test_gives_up_with_a_version_error_rather_than_looping():
    """A bank beyond what we can use is a finding, not an infinite retry.

    It named its versions, so there is nothing left to guess at: ask once, then
    stop. Walking our own range anyway is just noise on someone else's API.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-v"])
        return httpx.Response(406, text="Versions available: 99")

    client = make_client(
        handler,
        product_api_version=5,
        product_api_min_version=4,
        product_api_max_version=5,
        product_api_sanity_cap=20,
    )
    with pytest.raises(FetchError) as exc:
        await client.get_product_detail(BASE, "A", stats=BrandStats())
    await client.aclose()
    assert exc.value.kind == ErrorKind.VERSION
    assert seen == ["5"]
    assert "99" in exc.value.detail


# ---------------------------------------------------------------------------
# The Westpac case in full: list on v5, detail on v7, versions per ENDPOINT.
# ---------------------------------------------------------------------------


def westpac_handler(seen: list[tuple[str, str]]):
    """List serves v5 only; detail serves v7 only -- as Westpac actually does."""

    def handler(request: httpx.Request) -> httpx.Response:
        version = request.headers["x-v"]
        is_detail = request.url.path.rstrip("/").split("/banking/products")[-1] not in ("", "/")
        seen.append(("detail" if is_detail else "list", version))
        wanted = "7" if is_detail else "5"
        if version != wanted:
            return httpx.Response(
                406,
                text='{"errors":[{"code":406,"title":"Unsupported Version","detail":'
                f'"Value {version} is invalid for the x-v header. Versions available: {wanted}"}}]}}',
            )
        if is_detail:
            return httpx.Response(
                200, json={"data": {"productId": "HLFixed"}}, headers={"x-v": "7"}
            )
        return httpx.Response(
            200, json=products_page(["HLFixed", "SavLife"], 1), headers={"x-v": "5"}
        )

    return handler


async def test_list_and_detail_negotiate_independently():
    seen: list[tuple[str, str]] = []
    client = make_client(westpac_handler(seen))
    stats = BrandStats()

    products = [p async for p in client.list_products(BASE, "TERM_DEPOSITS", stats=stats)]
    await client.get_product_detail(BASE, "HLFixed", stats=stats)
    await client.aclose()

    assert len(products) == 2
    # list settles on 5; detail must reach 7 even though it is above the blind ceiling
    assert ("detail", "7") in seen
    assert seen[0] == ("list", "5")


async def test_detail_version_is_negotiated_once_not_per_product():
    """The bug that cost ~128 requests at one bank: detail re-negotiated every time."""
    seen: list[tuple[str, str]] = []
    client = make_client(westpac_handler(seen))
    stats = BrandStats()

    [p async for p in client.list_products(BASE, "TERM_DEPOSITS", stats=stats)]
    for product_id in ("A", "B", "C", "D"):
        await client.get_product_detail(BASE, product_id, stats=stats)
    await client.aclose()

    detail_calls = [v for kind, v in seen if kind == "detail"]
    # first detail negotiates 5 -> 7, the rest go straight to 7
    assert detail_calls == ["5", "7", "7", "7", "7"]


async def test_an_unsupported_endpoint_fails_fast_for_every_later_product():
    """One hopeless endpoint must not re-run the whole version walk 31 more times."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(406, text="Versions available: 99")

    client = make_client(handler, product_api_sanity_cap=10)
    for _ in range(5):
        with pytest.raises(FetchError) as exc:
            await client.get_product_detail(BASE, "A", stats=BrandStats())
        assert exc.value.kind == ErrorKind.VERSION
    await client.aclose()
    assert calls["n"] == 1  # tried once; the other four short-circuited


async def test_absurd_advertised_versions_are_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(406, text="Versions available: 4096")

    client = make_client(handler, product_api_sanity_cap=20)
    with pytest.raises(FetchError) as exc:
        await client.get_product_detail(BASE, "A", stats=BrandStats())
    await client.aclose()
    assert exc.value.kind == ErrorKind.VERSION
