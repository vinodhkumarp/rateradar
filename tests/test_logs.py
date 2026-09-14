"""Structured logging.

The point is queryability: fields a log platform can filter on rather than
strings to regex. These tests check the shape a query would depend on.
"""

from __future__ import annotations

import json
import logging

import pytest

from rateradar.logs import JsonFormatter, configure


def record(**extra) -> logging.LogRecord:
    rec = logging.LogRecord("rateradar", logging.WARNING, __file__, 10, "brand failed", None, None)
    rec.__dict__.update(extra)
    return rec


def test_emits_one_json_object_per_line():
    line = JsonFormatter().format(record())
    payload = json.loads(line)
    assert "\n" not in line
    assert payload["level"] == "WARNING"
    assert payload["msg"] == "brand failed"
    assert payload["logger"] == "rateradar"
    assert payload["ts"]


def test_extras_become_top_level_fields():
    """`extra={"brand_id": x}` must be queryable as brand_id, not buried in text."""
    payload = json.loads(JsonFormatter().format(record(brand_id="b1", kind="version")))
    assert payload["brand_id"] == "b1"
    assert payload["kind"] == "version"


def test_standard_record_attributes_are_not_dumped():
    """Otherwise every line carries pathname, lineno, thread and the rest."""
    payload = json.loads(JsonFormatter().format(record()))
    assert "pathname" not in payload
    assert "args" not in payload
    assert set(payload) >= {"ts", "level", "logger", "msg"}


def test_unserialisable_values_do_not_break_logging():
    """A log call must never be the thing that fails a run."""
    payload = json.loads(JsonFormatter().format(record(obj=object())))
    assert isinstance(payload["obj"], str)


def test_exceptions_are_included():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = record()
        rec.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(rec))
    assert "ValueError: boom" in payload["exception"]


@pytest.mark.parametrize(
    ("env", "expect_json"),
    [({"RATERADAR_LOG_FORMAT": "json"}, True), ({"RATERADAR_LOG_FORMAT": "text"}, False)],
)
def test_format_is_overridable_by_environment(monkeypatch, env, expect_json):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    configure()
    formatter = logging.getLogger().handlers[0].formatter
    assert isinstance(formatter, JsonFormatter) is expect_json


def test_lambda_defaults_to_json(monkeypatch):
    monkeypatch.delenv("RATERADAR_LOG_FORMAT", raising=False)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "rateradar")
    configure()
    assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)


def test_terminal_defaults_to_text(monkeypatch):
    monkeypatch.delenv("RATERADAR_LOG_FORMAT", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    configure()
    assert not isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)
