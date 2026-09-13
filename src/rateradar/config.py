"""Configuration. Everything tunable lives here; nothing tunable lives in code."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

# Product categories collected in v1. Widening this is a config change, not a
# code change -- see ADR-0004.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "TRANS_AND_SAVINGS_ACCOUNTS",
    "TERM_DEPOSITS",
    "RESIDENTIAL_MORTGAGES",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RATERADAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql://rateradar:rateradar@localhost:5432/rateradar"),
        description="Postgres connection string. Provided by CI as a secret.",
    )

    # --- discovery -----------------------------------------------------------
    register_base_url: str = "https://api.cdr.gov.au/cdr-register/v1"
    register_brands_path: str = "/banking/data-holders/brands/summary"
    register_api_version: int = 1
    #  Independent second source, used when the register is unreachable
    #  (ADR-0004: one discovery source is one silent point of failure).
    #  Empty by default: a fallback URL that 404s is worse than none, because it
    #  masks the real error. Set it once you have verified a source yourself.
    fallback_endpoint_list_url: str = ""
    brand_allowlist_file: Path = REPO_ROOT / "config" / "brands.allowlist"

    # --- CDR product API -----------------------------------------------------
    # Versions are per ENDPOINT, not per bank: Westpac serves the product list at
    # v5 and product detail at v7. These bounds govern blind stepping only, for
    # banks that 406 without saying what they support. A version the bank
    # explicitly advertises is always honoured (up to the sanity cap), because the
    # bank knows what it serves and we do not -- and an unexpected payload shape
    # is caught by the normaliser and quarantined, which is a recorded finding
    # rather than a silent loss.
    product_api_version: int = 5  # where blind negotiation starts
    product_api_min_version: int = 3  # x-min-v floor for blind stepping
    product_api_max_version: int = 7  # blind-stepping ceiling
    product_api_sanity_cap: int = 20  # refuse absurd advertised versions
    categories: tuple[str, ...] = DEFAULT_CATEGORIES
    page_size: int = 100

    # --- politeness ----------------------------------------------------------
    # We are an uninvited guest on someone else's API.
    global_concurrency: int = 8
    per_host_concurrency: int = 1  # one request at a time to any one bank
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 30.0
    max_retries: int = 3
    backoff_base_s: float = 1.0
    backoff_max_s: float = 30.0
    user_agent: str = (
        "RateRadar/0.1 (open banking product change tracker; "
        "+https://github.com/USERNAME/rateradar)"
    )

    # --- circuit breaker -----------------------------------------------------
    circuit_failure_threshold: int = 5
    circuit_cooldown_hours: int = 6

    # --- health thresholds ---------------------------------------------------
    freshness_warn_hours: int = 14
    freshness_fail_hours: int = 30
    min_changes_per_week: int = 1  # zero for a week means the differ is broken

    # --- behaviour flags -----------------------------------------------------
    store_raw_payloads: bool = True
    max_changes_per_product: int = 200  # guard against pathological diffs
    dry_run: bool = False


settings = Settings()
