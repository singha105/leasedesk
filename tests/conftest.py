"""Test-wide fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(autouse=True)
def _fresh_structlog() -> Iterator[None]:
    """A test that runs main() configures logging; the next test starts from the defaults."""
    yield
    structlog.reset_defaults()
