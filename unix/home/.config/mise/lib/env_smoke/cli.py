"""Cyclopts-backed CLI entrypoint for `mise run env:smoke`."""

from __future__ import annotations

import logging
import math
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import cyclopts
import orjson

from dotfiles_py.generated.env_smoke_schema import (
    EnvSmokeMode,
    EnvSmokeResponse,
    EnvSmokeStatus,
)
from dotfiles_py.logging import configure_logging
from env_smoke.runner import DEFAULT_TIMEOUT_S, parse_harnesses, run_smoke


def resolve_env_smoke_state_dir(override: Path | None = None) -> Path:
    """Resolve `$XDG_STATE_HOME/dotfiles-v2` cleanly."""
    if override is not None:
        return override
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(os.path.expanduser(xdg_state))
        if xdg_state
        else Path.home() / ".local" / "state"
    )
    return base / "dotfiles-v2"


def _configure_smoke_logging(
    state_dir: Path | None, *, json_mode: bool, write_log: bool
) -> None:
    jsonl_path = (
        resolve_env_smoke_state_dir(state_dir) / "env-smoke.log.jsonl"
        if write_log
        else None
    )
    logger = configure_logging(
        jsonl_path=jsonl_path,
        level=logging.WARNING if json_mode else logging.INFO,
        file_level=logging.INFO,
        logger_name="env_smoke",
    )
    if json_mode:
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.WARNING)


def _emit_json(resp: EnvSmokeResponse) -> None:
    sys.stdout.write(orjson.dumps(resp.model_dump(mode="json")).decode("utf-8") + "\n")


def _emit_text(resp: EnvSmokeResponse) -> None:
    s = resp.summary
    sys.stdout.write(
        f"env:smoke mode={resp.mode.value} total={s.total} ok={s.ok} "
        f"skip_quota={s.skip_quota} skip_rate_limit={s.skip_rate_limit} "
        f"skip_missing={s.skip_missing} fail={s.fail}\n"
    )
    for outcome in resp.outcomes:
        model = f" model={outcome.model}" if outcome.model else ""
        dur = f" {outcome.duration_ms}ms" if outcome.duration_ms is not None else ""
        reason = f" ({outcome.reason})" if outcome.reason else ""
        sys.stdout.write(
            f"  [{outcome.status.value}] {outcome.harness.value}{model}{dur}{reason}\n"
        )
        if outcome.status == EnvSmokeStatus.FAIL and outcome.output_excerpt:
            sys.stdout.write(f"    output: {outcome.output_excerpt[-300:]}\n")


def _build_app() -> cyclopts.App:
    app = cyclopts.App(
        name="env-smoke",
        help="Quota-gated smoke tests for claude, codex, agy, opencode, zcode, and hermes.",
        version_flags=(),
    )

    @app.default
    def smoke_cmd(
        *,
        harness: Annotated[
            list[str] | None, cyclopts.Parameter(name="--harness")
        ] = None,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = False,
        dry_run: Annotated[
            bool, cyclopts.Parameter(name="--dry-run", negative="")
        ] = False,
        skill_check: Annotated[
            bool, cyclopts.Parameter(name="--skill-check", negative="")
        ] = False,
        timeout: Annotated[
            float, cyclopts.Parameter(name="--timeout")
        ] = DEFAULT_TIMEOUT_S,
        state_dir: Annotated[
            Path | None, cyclopts.Parameter(name="--state-dir")
        ] = None,
        write_log: Annotated[
            bool, cyclopts.Parameter(name="--write-log", negative="")
        ] = False,
    ) -> int:
        """Pre-flight zero-token quota checks, then one tiny prompt per eligible harness.

        Parameters
        ----------
        harness
            Harness(es) to test (repeatable or comma list); default all 6.
        json_out
            Emit schema-validated EnvSmokeResponse JSON.
        dry_run
            Quota pre-flight only; never sends a prompt.
        skill_check
            Replace the OK prompt with an agentsview-finding-history CLI-vs-MCP check.
        timeout
            Per-harness prompt timeout in seconds.
        state_dir
            Override state directory for JSONL logs.
        write_log
            Append structured JSONL events to the state directory.
        """
        _configure_smoke_logging(state_dir, json_mode=json_out, write_log=write_log)
        try:
            selected = parse_harnesses(harness)
        except ValueError as exc:
            sys.stderr.write(f"env:smoke: invalid --harness: {exc}\n")
            return 2
        if not math.isfinite(timeout) or timeout <= 0:
            sys.stderr.write("env:smoke: --timeout must be a finite positive number\n")
            return 2
        if dry_run:
            mode = EnvSmokeMode.DRY_RUN
        elif skill_check:
            mode = EnvSmokeMode.SKILL_CHECK
        else:
            mode = EnvSmokeMode.SMOKE
        resp = run_smoke(selected, mode=mode, timeout=timeout)
        if json_out:
            _emit_json(resp)
        else:
            _emit_text(resp)
        return resp.rc

    return app


def main(argv: Sequence[str] | None = None) -> int:
    app = _build_app()
    result = app(argv, result_action="return_value")
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
