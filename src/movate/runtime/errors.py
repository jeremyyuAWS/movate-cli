"""Centralized error response shape + exception → HTTP mapping.

One JSON shape for every error so consumers don't have to special-case
auth vs validation vs not-found:

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

Codes are stable enums (``AUTH_REQUIRED``, ``NOT_FOUND``, etc.) — the
``message`` is human-readable and may change between releases, but
the ``code`` is contract.

Auth failures intentionally return a single ``AUTH_REQUIRED`` regardless
of why (missing header, malformed token, revoked, wrong tenant). Leaking
the discriminator to the caller would create a timing-attack oracle.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "auth_required"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    INTERNAL = "internal"


class ErrorBody(BaseModel):
    """Inner payload of every error response."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str


class ErrorResponse(BaseModel):
    """Outer envelope. The single field makes future expansion (e.g.
    ``request_id`` for tracing) non-breaking on the wire."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


def http_error(
    code: ErrorCode,
    *,
    status_code: int,
    message: str | None = None,
) -> HTTPException:
    """Build an ``HTTPException`` whose ``detail`` matches our envelope.

    Default ``message`` is the code's human form; pass ``message`` to
    override (e.g. ``message="job 'xyz' not found"``). Auth-related
    callers should NEVER pass a discriminating message.
    """
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message or code.value.replace("_", " "),
        )
    )
    return HTTPException(
        status_code=status_code,
        detail=body.model_dump(mode="json"),
    )


def auth_required() -> HTTPException:
    """Single-shape 401 for every auth failure mode."""
    return http_error(
        ErrorCode.AUTH_REQUIRED,
        status_code=status.HTTP_401_UNAUTHORIZED,
        message="authentication required",
    )


def not_found(resource: str, identifier: str) -> HTTPException:
    """404 with a narrowly-scoped message — safe to include the id since
    the caller already knew it."""
    return http_error(
        ErrorCode.NOT_FOUND,
        status_code=status.HTTP_404_NOT_FOUND,
        message=f"{resource} {identifier!r} not found",
    )


__all__ = [
    "ErrorBody",
    "ErrorCode",
    "ErrorResponse",
    "auth_required",
    "http_error",
    "not_found",
]
