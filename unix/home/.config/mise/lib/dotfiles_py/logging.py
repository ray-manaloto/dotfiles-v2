"""Dual-sink `structlog` + `orjson` logging bridge with 9-digit nanosecond timestamps."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

import orjson
import structlog
from pydantic import BaseModel

from dotfiles_py.time_ns import now_ns_pair


def _normalize_value(value: Any) -> Any:
    """Recursively normalize Pydantic models, StrEnums, Paths, and containers for serialization."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _normalize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    return value


def normalize_event_values(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Normalize Pydantic v2 models and StrEnums in `event_dict` across both sinks."""
    for key in list(event_dict.keys()):
        event_dict[key] = _normalize_value(event_dict[key])
    return event_dict


def add_nanosecond_timestamps(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Attach zero-padded 9-digit nanosecond `timestamp` and `timestamp_unix_ns` from `time.time_ns()`."""
    ts, ns = now_ns_pair()
    event_dict.setdefault("timestamp", ts)
    event_dict.setdefault("timestamp_unix_ns", ns)
    return event_dict


def orjson_dumps_str(event_dict: MutableMapping[str, Any], **_kwargs: Any) -> str:
    """Serialize `event_dict` cleanly via Rust-backed `orjson`."""
    return orjson.dumps(event_dict, option=orjson.OPT_NON_STR_KEYS).decode("utf-8")


SHARED_PROCESSORS: list[structlog.types.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    add_nanosecond_timestamps,
    normalize_event_values,
    structlog.stdlib.PositionalArgumentsFormatter(),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def configure_logging(
    *,
    jsonl_path: Path | None = None,
    console_stream: TextIO | None = None,
    level: int = logging.INFO,
    colors: bool | None = None,
    logger_name: str = "",
) -> logging.Logger:
    """Configure dual-sink `structlog` (`ConsoleRenderer` -> stderr + `orjson` JSONL -> file)."""
    stream = sys.stderr if console_stream is None else console_stream
    use_colors = bool(stream.isatty()) if colors is None else colors

    structlog.configure(
        processors=[
            *SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    target_logger = logging.getLogger(logger_name)
    for existing in list(target_logger.handlers):
        target_logger.removeHandler(existing)
        existing.close()

    target_logger.setLevel(level)
    if logger_name:
        target_logger.propagate = False

    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=use_colors),
        ],
    )
    console_handler = logging.StreamHandler(stream)
    console_handler.setLevel(level)
    console_handler.setFormatter(console_formatter)
    target_logger.addHandler(console_handler)

    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        jsonl_formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=SHARED_PROCESSORS,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(serializer=orjson_dumps_str),
            ],
        )
        file_handler = logging.FileHandler(jsonl_path, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(jsonl_formatter)
        target_logger.addHandler(file_handler)

    return target_logger


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a `structlog` bound logger routed through the dual-sink `ProcessorFormatter`."""
    return structlog.get_logger(name)
