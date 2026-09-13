"""Tests run offline: no database, no bank APIs, no network."""

import os

os.environ.setdefault("RATERADAR_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
