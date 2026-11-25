"""Report host quota using the same prompt-free meters as env:smoke."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import cyclopts
import orjson

from dotfiles_py.generated.common.envelope_schema import CommonReturnCode, ReturnEntry
from dotfiles_py.generated.env_quota_schema import (
    EnvQuotaHarnessReading,
    EnvQuotaResponse,
)
from dotfiles_py.generated.env_smoke_schema import (
    EnvSmokeErrorCode,
    EnvSmokeErrorEntry,
    EnvSmokeHarness,
)
from dotfiles_py.logging import configure_logging, get_logger
from dotfiles_py.time_ns import now_ns_pair
from env_smoke import quota
from env_smoke.cli import resolve_env_smoke_state_dir
from env_smoke.runner import default_probe, parse_harnesses, resolve_binary


def collect(harnesses: Sequence[EnvSmokeHarness]) -> EnvQuotaResponse:
    readings = []
    errors = []
    for harness in harnesses:
        binary = resolve_binary(harness.value) or harness.value
        reading = default_probe(harness, binary, quota.now_utc())
        if reading.reason:
            reading = reading.model_copy(
                update={"reason": quota.sanitize_diagnostic(reading.reason)}
            )
        readings.append(EnvQuotaHarnessReading(harness=harness, quota=reading))
        if not reading.probe_ok:
            errors.append(
                EnvSmokeErrorEntry(
                    code=EnvSmokeErrorCode.ERR_QUOTA_PROBE_FAILED,
                    message=reading.reason,
                    detail={"harness": harness.value},
                )
            )
    timestamp, ns = now_ns_pair()
    code = CommonReturnCode.RET_PARTIAL if errors else CommonReturnCode.RET_OK
    return EnvQuotaResponse(
        readings=readings,
        errors=errors,
        returns=[ReturnEntry(code=code)],
        timestamp=timestamp,
        timestamp_unix_ns=ns,
    )


app = cyclopts.App(name="env-quota", version_flags=())


@app.default
def report(
    *,
    harness: list[str] | None = None,
    json_out: Annotated[bool, cyclopts.Parameter(name="--json", negative="")] = False,
    write_log: bool = False,
    state_dir: Path | None = None,
) -> int:
    """Read quota without submitting prompts."""
    try:
        selected = parse_harnesses(harness)
    except ValueError as exc:
        sys.stderr.write(f"env:quota: invalid --harness: {exc}\n")
        return 2
    path = (
        resolve_env_smoke_state_dir(state_dir) / "env-quota.log.jsonl"
        if write_log
        else None
    )
    configure_logging(jsonl_path=path, level=logging.WARNING)
    response = collect(selected)
    get_logger("env_quota").info("quota_checked", response=response)
    if json_out:
        sys.stdout.write(orjson.dumps(response.model_dump(mode="json")).decode() + "\n")
    else:
        for row in response.readings:
            reading = row.quota
            if not reading.probe_ok:
                status = "UNKNOWN"
            elif reading.available:
                status = "AVAILABLE"
            else:
                status = f"SKIP_QUOTA(resets {reading.reset or 'unknown'})"
            sys.stdout.write(f"{row.harness.value}: {status} {reading.reason or ''}\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return app(argv, result_action="return_value")
