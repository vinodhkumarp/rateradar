"""The Lambda adapter. Thin by design, but the dispatch must not guess."""

from __future__ import annotations

import pytest

from rateradar import handler as handler_module


@pytest.fixture(autouse=True)
def _no_ssm(monkeypatch):
    """Never reach for Parameter Store in tests."""
    monkeypatch.delenv("RATERADAR_DATABASE_URL_PARAM", raising=False)
    monkeypatch.setenv("RATERADAR_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    handler_module._load_settings.cache_clear()
    yield
    handler_module._load_settings.cache_clear()


def test_defaults_to_collect(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(handler_module, "_collect", lambda s: called.append("collect") or {})
    result = handler_module.handler({})
    assert called == ["collect"]
    assert result["command"] == "collect" and result["ok"] is True


def test_empty_event_is_treated_as_collect(monkeypatch):
    monkeypatch.setattr(handler_module, "_collect", lambda s: {})
    assert handler_module.handler(None)["command"] == "collect"


def test_discover_is_dispatched(monkeypatch):
    monkeypatch.setattr(handler_module, "_discover", lambda s: {"brands_enabled": 20})
    result = handler_module.handler({"command": "discover"})
    assert result["brands_enabled"] == 20


def test_unknown_command_is_rejected_not_assumed():
    """A malformed schedule should fail loudly, not quietly do nothing."""
    with pytest.raises(ValueError, match="unknown command"):
        handler_module.handler({"command": "collectt"})


def test_settings_are_cached_across_invocations(monkeypatch):
    """Warm invocations must not re-read the parameter on every event."""
    monkeypatch.setattr(handler_module, "_collect", lambda s: {})
    first = handler_module._load_settings()
    handler_module.handler({})
    assert handler_module._load_settings() is first
