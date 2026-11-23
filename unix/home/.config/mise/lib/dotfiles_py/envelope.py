"""Typed envelope construction helpers backed by schema-generated Pydantic v2 models."""

from __future__ import annotations

from typing import Any

from dotfiles_py.generated import (
    CommonErrorCode,
    CommonReturnCode,
    EnvelopeMetadata,
    ErrorEntry,
    ReturnEntry,
)
from dotfiles_py.time_ns import now_ns_pair


def build_common_error(
    code: CommonErrorCode,
    *,
    message: str | None = None,
    detail: dict[str, Any] | None = None,
) -> ErrorEntry:
    """Create an immutable typed `ErrorEntry`."""
    return ErrorEntry(
        code=code,
        message=message if message else None,
        detail=detail,
    )


def build_common_return(
    code: CommonReturnCode,
    *,
    message: str | None = None,
    detail: dict[str, Any] | None = None,
) -> ReturnEntry:
    """Create an immutable typed `ReturnEntry`."""
    return ReturnEntry(
        code=code,
        message=message if message else None,
        detail=detail,
    )


def build_envelope_metadata(
    *,
    errors: list[ErrorEntry] | None = None,
    returns: list[ReturnEntry] | None = None,
    utc: bool = False,
) -> EnvelopeMetadata:
    """Build a schema-validated `EnvelopeMetadata` with 9-digit ns timestamp."""
    ts, ns = now_ns_pair(utc=utc)
    return EnvelopeMetadata(
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=list(errors or ()),
        returns=list(returns or ()),
    )
