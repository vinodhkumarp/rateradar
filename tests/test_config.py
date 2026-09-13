"""Settings guards. Small, but each one exists because of a real failure mode."""

from __future__ import annotations

from rateradar.config import Settings


def test_blank_user_agent_falls_back_to_the_default():
    """An unset GitHub Actions variable arrives as an empty string, not as
    absence, so a naive override would send banks a blank User-Agent."""
    assert Settings(user_agent="").user_agent.startswith("RateRadar/")
    assert Settings(user_agent="   ").user_agent.startswith("RateRadar/")


def test_a_real_user_agent_is_kept():
    agent = "RateRadar/0.1 (+https://github.com/vinodh/rateradar)"
    assert Settings(user_agent=agent).user_agent == agent


def test_politeness_defaults_are_conservative():
    """These are not performance tuning; docs/data-use.md commits to them."""
    settings = Settings()
    assert settings.per_host_concurrency == 1
    assert settings.max_retries <= 3
    assert settings.circuit_failure_threshold <= 5
