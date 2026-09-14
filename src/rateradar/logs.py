"""Logging setup.

Two audiences, two formats. A person at a terminal wants a readable line; a log
platform wants fields it can filter on without regexes. The same call sites
serve both: pass structured values via `extra=` and let the formatter decide.

    log.warning("brand failed", extra={"brand_id": brand_id, "kind": exc.kind})

CloudWatch Logs Insights parses JSON automatically, so those become queryable
fields rather than text to pattern-match:

    fields @timestamp, brand_id, kind | filter kind = "version"
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

# Attributes every LogRecord carries. Anything else was passed as extra= and is
# therefore something the caller wanted recorded.
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with extras promoted to top-level fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure(*, verbose: bool = False, json_output: bool | None = None) -> None:
    """Configure root logging once.

    json_output defaults to on when running in Lambda, off at a terminal, and is
    overridable with RATERADAR_LOG_FORMAT=json|text either way.
    """
    if json_output is None:
        configured = os.environ.get("RATERADAR_LOG_FORMAT", "").lower()
        if configured in {"json", "text"}:
            json_output = configured == "json"
        else:
            json_output = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # httpx logs every request at INFO. Useful when chasing a bank, noise the
    # rest of the time, and the run ledger records the request counts anyway.
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)
