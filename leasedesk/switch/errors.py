"""Typed errors for Switch HTTP calls. ``detail`` is the server's FastAPI ``detail`` text."""

from __future__ import annotations


class SwitchError(Exception):
    def __init__(self, status: int, detail: str, op: str = "") -> None:
        self.status = status
        self.detail = detail
        self.op = op
        super().__init__(f"{op or 'switch'}: HTTP {status}: {detail}")


class BadRequest(SwitchError):
    """400. Note: Switch also answers 400 "not found" when an agent edits a doc it doesn't own."""


class Forbidden(SwitchError):
    """403."""


class NotFound(SwitchError):
    """404."""


class Conflict(SwitchError):
    """409 (name taken, role held)."""


class Unavailable(SwitchError):
    """5xx or a transport failure. The only error a read is retried on."""


class AuthFailed(SwitchError):
    """401."""


def error_for(status: int, detail: str, op: str = "") -> SwitchError:
    cls: type[SwitchError] = {
        400: BadRequest,
        401: AuthFailed,
        403: Forbidden,
        404: NotFound,
        409: Conflict,
    }.get(status, Unavailable if status >= 500 else SwitchError)
    return cls(status, detail, op)
