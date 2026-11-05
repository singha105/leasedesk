"""Structured JSON logging with a redaction processor that runs before anything is rendered."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

REDACTED = "[REDACTED]"

# Keys whose values are always secret, whatever they contain.
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|token|secret|password|passwd|registration[_-]?key|"
    r"aws_access_key_id|aws_secret_access_key|aws_session_token|credential)",
    re.IGNORECASE,
)

# Secret-looking substrings inside free text.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),  # AWS access key id
    re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)[A-Za-z0-9/+=]{40}"),
)


def redact_text(text: str) -> str:
    for pattern in _VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any, key: str | None = None) -> Any:
    """Return a copy of value with secret keys and secret-looking strings replaced."""
    if key is not None and _SECRET_KEY_RE.search(key) and value not in (None, "", False):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact(v) for v in value)
    return value


def redaction_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return {k: redact(v, k) for k, v in event_dict.items()}


QUIET_LIBRARIES = ("httpx", "httpcore", "websockets")


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=level.upper())
    # These log through plain stdlib logging, which skips the redaction processor.
    for noisy in QUIET_LIBRARIES:
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLevelName(level.upper())))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redaction_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
