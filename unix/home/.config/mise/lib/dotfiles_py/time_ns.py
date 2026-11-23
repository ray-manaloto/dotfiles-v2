"""Zero-padded 9-decimal-place nanosecond timestamp helpers backed by `time.time_ns()`."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime

NANOSECOND_TIMESTAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}\.[0-9]{9}(?:Z|[+-][0-9]{4})$")


def format_ns_timestamp(ns: int | None = None, *, utc: bool = False) -> str:
    """Format epoch nanoseconds as `YYYYmmddTHHMMSS.NNNNNNNNN±HHMM` (9 zero-padded fractional digits)."""
    epoch_ns = time.time_ns() if ns is None else int(ns)
    if epoch_ns < 0:
        msg = f"Epoch nanoseconds must be non-negative, got {epoch_ns}"
        raise ValueError(msg)
    sec, rem_ns = divmod(epoch_ns, 1_000_000_000)
    dt_utc = datetime.fromtimestamp(sec, tz=UTC)
    dt_target = dt_utc if utc else dt_utc.astimezone()
    return f"{dt_target:%Y%m%dT%H%M%S}.{rem_ns:09d}{dt_target:%z}"


def now_ns_pair(*, utc: bool = False) -> tuple[str, int]:
    """Return `(formatted_ns_timestamp, epoch_ns)` sampled from a single `time.time_ns()` call."""
    epoch_ns = time.time_ns()
    return format_ns_timestamp(epoch_ns, utc=utc), epoch_ns
