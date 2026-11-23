"""Shared schema-first Python core (`time_ns`, dual-sink `structlog`, typed `envelope`, and generated DTOs)."""

from dotfiles_py.envelope import (
    build_common_error,
    build_common_return,
    build_envelope_metadata,
)
from dotfiles_py.logging import (
    add_nanosecond_timestamps,
    configure_logging,
    get_logger,
    normalize_event_values,
    orjson_dumps_str,
)
from dotfiles_py.time_ns import (
    NANOSECOND_TIMESTAMP_RE,
    format_ns_timestamp,
    now_ns_pair,
)

__all__ = [
    "NANOSECOND_TIMESTAMP_RE",
    "add_nanosecond_timestamps",
    "build_common_error",
    "build_common_return",
    "build_envelope_metadata",
    "configure_logging",
    "format_ns_timestamp",
    "get_logger",
    "normalize_event_values",
    "now_ns_pair",
    "orjson_dumps_str",
]
