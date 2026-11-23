"""Cyclopts-backed CLI entrypoint for `herdr_roll` global mise tasks."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import cyclopts
import orjson

from dotfiles_py.time_ns import now_ns_pair
from herdr_roll.engine import (
    EXIT_FAILED,
    EXIT_REFUSED,
    cmd_restart,
    cmd_self,
    cmd_snapshot,
    cmd_start,
    cmd_stop,
    cmd_verify,
)
from herdr_roll.graph import GraphError, RefusedError
from herdr_roll.models import (
    HarnessKind,
    HerdrRollErrorCode,
    HerdrRollErrorEntry,
    HerdrRollResponse,
    HerdrRollReturnCode,
    HerdrRollReturnEntry,
    Timing,
)


def _split_csv(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    out: list[str] = []
    for item in values:
        for part in item.split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return tuple(out)


def _parse_harnesses(values: Sequence[str] | None) -> tuple[HarnessKind, ...]:
    raw = _split_csv(values)
    out: list[HarnessKind] = []
    valid = {k.value: k for k in HarnessKind if k is not HarnessKind.SHELL}
    for name in raw:
        kind = valid.get(name.lower())
        if kind is None:
            raise RefusedError(
                f"Unsupported harness {name!r}; choose from {', '.join(sorted(valid))}"
            )
        if kind not in out:
            out.append(kind)
    return tuple(out)


def _validate_positive_int(name: str, value: int) -> int:
    if value < 1:
        raise RefusedError(f"{name} must be >= 1 (got {value})")
    return value


def _validate_non_negative(name: str, value: float) -> float:
    if value < 0:
        raise RefusedError(f"{name} must be >= 0 (got {value})")
    return float(value)


def _build_app(raw_argv: tuple[str, ...]) -> cyclopts.App:
    app = cyclopts.App(
        name="herdr_roll",
        help="Clean-room wave-ordered Herdr rolling restart CLI.",
        version_flags=(),
    )

    @app.command(name="snapshot")
    def snapshot_cmd(
        *,
        tabs: Annotated[list[str] | None, cyclopts.Parameter(name="--tabs")] = None,
        spaces: Annotated[list[str] | None, cyclopts.Parameter(name="--spaces")] = None,
        harnesses: Annotated[
            list[str] | None, cyclopts.Parameter(name="--harnesses")
        ] = None,
        all_targets: Annotated[
            bool, cyclopts.Parameter(name="--all", negative="")
        ] = False,
        exclude: Annotated[
            list[str] | None, cyclopts.Parameter(name="--exclude")
        ] = None,
        apply: Annotated[bool, cyclopts.Parameter(name="--apply", negative="")] = False,
    ) -> int:
        """Capture Herdr session graph snapshot."""
        return cmd_snapshot(
            tabs=_split_csv(tabs),
            spaces=_split_csv(spaces),
            harnesses=_parse_harnesses(harnesses),
            all_targets=all_targets,
            exclude=_split_csv(exclude),
            apply=apply,
            argv=raw_argv,
        )

    @app.command(name="restart")
    def restart_cmd(
        *,
        tabs: Annotated[list[str] | None, cyclopts.Parameter(name="--tabs")] = None,
        spaces: Annotated[list[str] | None, cyclopts.Parameter(name="--spaces")] = None,
        harnesses: Annotated[
            list[str] | None, cyclopts.Parameter(name="--harnesses")
        ] = None,
        all_targets: Annotated[
            bool, cyclopts.Parameter(name="--all", negative="")
        ] = False,
        exclude: Annotated[
            list[str] | None, cyclopts.Parameter(name="--exclude")
        ] = None,
        apply: Annotated[bool, cyclopts.Parameter(name="--apply", negative="")] = False,
        concurrency: Annotated[int, cyclopts.Parameter(name="--concurrency")] = 4,
        wait: Annotated[float, cyclopts.Parameter(name="--wait")] = 180.0,
        settle: Annotated[float, cyclopts.Parameter(name="--settle")] = 45.0,
        busy_wait: Annotated[float, cyclopts.Parameter(name="--busy-wait")] = 0.0,
        handshake: Annotated[
            bool, cyclopts.Parameter(name="--handshake", negative="")
        ] = False,
        verify_turn: Annotated[
            bool, cyclopts.Parameter(name="--verify-turn", negative="")
        ] = False,
        fresh_tab: Annotated[
            bool, cyclopts.Parameter(name="--fresh-tab", negative="")
        ] = False,
        keep_env: Annotated[
            bool, cyclopts.Parameter(name="--keep-env", negative="")
        ] = False,
        close_self_on_ok: Annotated[
            bool, cyclopts.Parameter(name="--close-self-on-ok", negative="")
        ] = False,
    ) -> int:
        """Restart targeted Herdr tabs cleanly."""
        return cmd_restart(
            tabs=_split_csv(tabs),
            spaces=_split_csv(spaces),
            harnesses=_parse_harnesses(harnesses),
            all_targets=all_targets,
            exclude=_split_csv(exclude),
            concurrency=_validate_positive_int("--concurrency", concurrency),
            timing=Timing(
                wait=_validate_non_negative("--wait", wait),
                settle=_validate_non_negative("--settle", settle),
                busy_wait=_validate_non_negative("--busy-wait", busy_wait),
            ),
            handshake=handshake,
            verify_turn=verify_turn,
            fresh_tab=fresh_tab,
            force_clean_env=not keep_env,
            close_self_on_ok=close_self_on_ok,
            apply=apply,
            argv=raw_argv,
        )

    @app.command(name="stop")
    def stop_cmd(
        *,
        tabs: Annotated[list[str] | None, cyclopts.Parameter(name="--tabs")] = None,
        spaces: Annotated[list[str] | None, cyclopts.Parameter(name="--spaces")] = None,
        harnesses: Annotated[
            list[str] | None, cyclopts.Parameter(name="--harnesses")
        ] = None,
        all_targets: Annotated[
            bool, cyclopts.Parameter(name="--all", negative="")
        ] = False,
        exclude: Annotated[
            list[str] | None, cyclopts.Parameter(name="--exclude")
        ] = None,
        apply: Annotated[bool, cyclopts.Parameter(name="--apply", negative="")] = False,
        wait: Annotated[float, cyclopts.Parameter(name="--wait")] = 180.0,
        settle: Annotated[float, cyclopts.Parameter(name="--settle")] = 45.0,
        handshake: Annotated[
            bool, cyclopts.Parameter(name="--handshake", negative="")
        ] = False,
    ) -> int:
        """Safely stop targeted tabs to bare shells."""
        return cmd_stop(
            tabs=_split_csv(tabs),
            spaces=_split_csv(spaces),
            harnesses=_parse_harnesses(harnesses),
            all_targets=all_targets,
            exclude=_split_csv(exclude),
            timing=Timing(
                wait=_validate_non_negative("--wait", wait),
                settle=_validate_non_negative("--settle", settle),
            ),
            handshake=handshake,
            apply=apply,
            argv=raw_argv,
        )

    @app.command(name="start")
    def start_cmd(
        *,
        tabs: Annotated[list[str] | None, cyclopts.Parameter(name="--tabs")] = None,
        spaces: Annotated[list[str] | None, cyclopts.Parameter(name="--spaces")] = None,
        harnesses: Annotated[
            list[str] | None, cyclopts.Parameter(name="--harnesses")
        ] = None,
        all_targets: Annotated[
            bool, cyclopts.Parameter(name="--all", negative="")
        ] = False,
        exclude: Annotated[
            list[str] | None, cyclopts.Parameter(name="--exclude")
        ] = None,
        apply: Annotated[bool, cyclopts.Parameter(name="--apply", negative="")] = False,
        run_id: Annotated[str | None, cyclopts.Parameter(name="--run")] = None,
        graph: Annotated[Path | None, cyclopts.Parameter(name="--graph")] = None,
        prompt: Annotated[str | None, cyclopts.Parameter(name="--prompt")] = None,
        fresh_tab: Annotated[
            bool, cyclopts.Parameter(name="--fresh-tab", negative="")
        ] = False,
        concurrency: Annotated[int, cyclopts.Parameter(name="--concurrency")] = 4,
        settle: Annotated[float, cyclopts.Parameter(name="--settle")] = 45.0,
    ) -> int:
        """Resume sessions from saved graph.json."""
        return cmd_start(
            run_id=run_id,
            graph_path=graph,
            tabs=_split_csv(tabs),
            spaces=_split_csv(spaces),
            harnesses=_parse_harnesses(harnesses),
            all_targets=all_targets,
            exclude=_split_csv(exclude),
            prompt=prompt,
            fresh_tab=fresh_tab,
            concurrency=_validate_positive_int("--concurrency", concurrency),
            timing=Timing(settle=_validate_non_negative("--settle", settle)),
            apply=apply,
            argv=raw_argv,
        )

    @app.command(name="verify")
    def verify_cmd(
        *,
        tabs: Annotated[list[str] | None, cyclopts.Parameter(name="--tabs")] = None,
        spaces: Annotated[list[str] | None, cyclopts.Parameter(name="--spaces")] = None,
        harnesses: Annotated[
            list[str] | None, cyclopts.Parameter(name="--harnesses")
        ] = None,
        all_targets: Annotated[
            bool, cyclopts.Parameter(name="--all", negative="")
        ] = False,
        exclude: Annotated[
            list[str] | None, cyclopts.Parameter(name="--exclude")
        ] = None,
        apply: Annotated[bool, cyclopts.Parameter(name="--apply", negative="")] = False,
        run_id: Annotated[str | None, cyclopts.Parameter(name="--run")] = None,
        settle: Annotated[float, cyclopts.Parameter(name="--settle")] = 45.0,
    ) -> int:
        """Verify restarted or resumed sessions."""
        return cmd_verify(
            run_id=run_id,
            tabs=_split_csv(tabs),
            spaces=_split_csv(spaces),
            harnesses=_parse_harnesses(harnesses),
            all_targets=all_targets,
            exclude=_split_csv(exclude),
            timing=Timing(settle=_validate_non_negative("--settle", settle)),
            apply=apply,
            argv=raw_argv,
        )

    @app.command(name="self")
    def self_cmd(
        *,
        fresh_tab: Annotated[
            bool, cyclopts.Parameter(name="--fresh-tab", negative="")
        ] = False,
        busy_wait: Annotated[float, cyclopts.Parameter(name="--busy-wait")] = 30.0,
        settle: Annotated[float, cyclopts.Parameter(name="--settle")] = 45.0,
        handshake: Annotated[
            bool, cyclopts.Parameter(name="--handshake", negative="")
        ] = False,
        apply: Annotated[bool, cyclopts.Parameter(name="--apply", negative="")] = False,
    ) -> int:
        """Spawn a detached roll tab to restart current tab safely."""
        return cmd_self(
            fresh_tab=fresh_tab,
            busy_wait=_validate_non_negative("--busy-wait", busy_wait),
            settle=_validate_non_negative("--settle", settle),
            handshake=handshake,
            apply=apply,
        )

    return app


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments via `cyclopts` and return deterministic exit code."""
    arg_list = list(sys.argv[1:] if argv is None else argv)
    app = _build_app(tuple(arg_list))
    try:
        result = app(arg_list, exit_on_error=False, result_action="return_value")
        return int(result) if isinstance(result, int) else 0
    except cyclopts.CycloptsError as err:
        ts, ns = now_ns_pair()
        resp = HerdrRollResponse(
            step=arg_list[0] if arg_list else "cli",
            rc=EXIT_REFUSED,
            apply=False,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                HerdrRollErrorEntry(
                    code=HerdrRollErrorCode.ERR_REFUSED,
                    message=str(err),
                )
            ],
            returns=[HerdrRollReturnEntry(code=HerdrRollReturnCode.RET_REFUSED)],
        )
        sys.stderr.write(
            orjson.dumps(resp.model_dump(mode="json")).decode("utf-8") + "\n"
        )
        return EXIT_REFUSED
    except (RefusedError, GraphError, OSError) as err:
        is_refused = isinstance(err, RefusedError)
        rc = EXIT_REFUSED if is_refused else EXIT_FAILED
        err_code = (
            HerdrRollErrorCode.ERR_REFUSED
            if is_refused
            else (
                HerdrRollErrorCode.ERR_GRAPH_INVALID
                if isinstance(err, GraphError)
                else HerdrRollErrorCode.ERR_IO_FAILURE
            )
        )
        ret_code = (
            HerdrRollReturnCode.RET_REFUSED
            if is_refused
            else HerdrRollReturnCode.RET_FAILED
        )
        ts, ns = now_ns_pair()
        resp = HerdrRollResponse(
            step=arg_list[0] if arg_list else "cli",
            rc=rc,
            apply=False,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[HerdrRollErrorEntry(code=err_code, message=str(err))],
            returns=[HerdrRollReturnEntry(code=ret_code, message=str(err))],
        )
        sys.stderr.write(
            orjson.dumps(resp.model_dump(mode="json")).decode("utf-8") + "\n"
        )
        return rc


if __name__ == "__main__":
    raise SystemExit(main())
