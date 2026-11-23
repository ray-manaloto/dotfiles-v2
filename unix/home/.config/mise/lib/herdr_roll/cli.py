"""Command-line entrypoint for `herdr_roll` global mise tasks."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

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
from herdr_roll.models import HarnessKind, Timing


def _split_csv(values: list[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    out: list[str] = []
    for item in values:
        for part in item.split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return tuple(out)


def _parse_harnesses(values: list[str] | None) -> tuple[HarnessKind, ...]:
    raw = _split_csv(values)
    out: list[HarnessKind] = []
    valid = {k.value: k for k in HarnessKind if k is not HarnessKind.shell}
    for name in raw:
        kind = valid.get(name.lower())
        if kind is None:
            raise RefusedError(
                f"Unsupported harness {name!r}; choose from {', '.join(sorted(valid))}"
            )
        if kind not in out:
            out.append(kind)
    return tuple(out)


def _add_selector_flags(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--tabs",
        action="append",
        help="Target tab name(s), space/tab, or terminal address(es)",
    )
    sub.add_argument("--spaces", action="append", help="Target space name(s)")
    sub.add_argument(
        "--harnesses",
        action="append",
        help="Filter by harness (agy,codex,claude,opencode,hermes,zcode)",
    )
    sub.add_argument(
        "--all",
        dest="all_targets",
        action="store_true",
        help="Target all non-excluded live agent tabs",
    )
    sub.add_argument(
        "--exclude",
        action="append",
        help="Spaces, tabs, or terminal IDs to leave untouched",
    )
    sub.add_argument(
        "--apply",
        action="store_true",
        help="Apply mutations (default: read-only dry-run)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdr_roll")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_snap = subparsers.add_parser(
        "snapshot", help="Capture Herdr session graph snapshot"
    )
    _add_selector_flags(p_snap)

    p_restart = subparsers.add_parser(
        "restart", help="Restart targeted Herdr tabs cleanly"
    )
    _add_selector_flags(p_restart)
    p_restart.add_argument("--concurrency", type=int, default=4)
    p_restart.add_argument("--wait", type=float, default=180.0)
    p_restart.add_argument("--settle", type=float, default=45.0)
    p_restart.add_argument("--busy-wait", type=float, default=0.0)
    p_restart.add_argument("--handshake", action="store_true")
    p_restart.add_argument("--verify-turn", action="store_true")
    p_restart.add_argument("--fresh-tab", action="store_true")
    p_restart.add_argument(
        "--keep-env",
        action="store_true",
        help="Keep current shell env instead of clean env -i",
    )

    p_stop = subparsers.add_parser(
        "stop", help="Safely stop targeted tabs to bare shells"
    )
    _add_selector_flags(p_stop)
    p_stop.add_argument("--wait", type=float, default=180.0)
    p_stop.add_argument("--settle", type=float, default=45.0)
    p_stop.add_argument("--handshake", action="store_true")

    p_start = subparsers.add_parser(
        "start", help="Resume sessions from saved graph.json"
    )
    _add_selector_flags(p_start)
    p_start.add_argument("--run", dest="run_id", default=None)
    p_start.add_argument("--graph", type=Path, default=None)
    p_start.add_argument("--prompt", default=None)
    p_start.add_argument("--fresh-tab", action="store_true")
    p_start.add_argument("--concurrency", type=int, default=4)
    p_start.add_argument("--settle", type=float, default=45.0)

    p_verify = subparsers.add_parser(
        "verify", help="Verify restarted or resumed sessions"
    )
    _add_selector_flags(p_verify)
    p_verify.add_argument("--run", dest="run_id", default=None)
    p_verify.add_argument("--settle", type=float, default=45.0)

    p_self = subparsers.add_parser(
        "self", help="Spawn a detached roll tab to restart current tab safely"
    )
    p_self.add_argument("--fresh-tab", action="store_true")
    p_self.add_argument("--busy-wait", type=float, default=30.0)
    p_self.add_argument("--settle", type=float, default=45.0)
    p_self.add_argument("--handshake", action="store_true")
    p_self.add_argument("--apply", action="store_true")

    return parser


def _validate_positive_int(name: str, value: int) -> int:
    if value < 1:
        raise RefusedError(f"{name} must be >= 1 (got {value})")
    return value


def _validate_non_negative(name: str, value: float) -> float:
    if value < 0:
        raise RefusedError(f"{name} must be >= 0 (got {value})")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    arg_list = list(sys.argv[1:] if argv is None else argv)
    try:
        ns = build_parser().parse_args(arg_list)
        sub = ns.subcommand
        if sub == "snapshot":
            return cmd_snapshot(
                tabs=_split_csv(ns.tabs),
                spaces=_split_csv(ns.spaces),
                harnesses=_parse_harnesses(ns.harnesses),
                all_targets=ns.all_targets,
                exclude=_split_csv(ns.exclude),
                apply=ns.apply,
                argv=tuple(arg_list),
            )
        if sub == "restart":
            return cmd_restart(
                tabs=_split_csv(ns.tabs),
                spaces=_split_csv(ns.spaces),
                harnesses=_parse_harnesses(ns.harnesses),
                all_targets=ns.all_targets,
                exclude=_split_csv(ns.exclude),
                concurrency=_validate_positive_int("--concurrency", ns.concurrency),
                timing=Timing(
                    wait=_validate_non_negative("--wait", ns.wait),
                    settle=_validate_non_negative("--settle", ns.settle),
                    busy_wait=_validate_non_negative("--busy-wait", ns.busy_wait),
                ),
                handshake=ns.handshake,
                verify_turn=ns.verify_turn,
                fresh_tab=ns.fresh_tab,
                force_clean_env=not ns.keep_env,
                apply=ns.apply,
                argv=tuple(arg_list),
            )
        if sub == "stop":
            return cmd_stop(
                tabs=_split_csv(ns.tabs),
                spaces=_split_csv(ns.spaces),
                harnesses=_parse_harnesses(ns.harnesses),
                all_targets=ns.all_targets,
                exclude=_split_csv(ns.exclude),
                timing=Timing(
                    wait=_validate_non_negative("--wait", ns.wait),
                    settle=_validate_non_negative("--settle", ns.settle),
                ),
                handshake=ns.handshake,
                apply=ns.apply,
                argv=tuple(arg_list),
            )
        if sub == "start":
            return cmd_start(
                run_id=ns.run_id,
                graph_path=ns.graph,
                tabs=_split_csv(ns.tabs),
                spaces=_split_csv(ns.spaces),
                harnesses=_parse_harnesses(ns.harnesses),
                all_targets=ns.all_targets,
                exclude=_split_csv(ns.exclude),
                prompt=ns.prompt,
                fresh_tab=ns.fresh_tab,
                concurrency=_validate_positive_int("--concurrency", ns.concurrency),
                timing=Timing(settle=_validate_non_negative("--settle", ns.settle)),
                apply=ns.apply,
                argv=tuple(arg_list),
            )
        if sub == "verify":
            return cmd_verify(
                run_id=ns.run_id,
                tabs=_split_csv(ns.tabs),
                spaces=_split_csv(ns.spaces),
                harnesses=_parse_harnesses(ns.harnesses),
                all_targets=ns.all_targets,
                exclude=_split_csv(ns.exclude),
                timing=Timing(settle=_validate_non_negative("--settle", ns.settle)),
                apply=ns.apply,
                argv=tuple(arg_list),
            )
        if sub == "self":
            return cmd_self(
                fresh_tab=ns.fresh_tab,
                busy_wait=_validate_non_negative("--busy-wait", ns.busy_wait),
                settle=_validate_non_negative("--settle", ns.settle),
                handshake=ns.handshake,
                apply=ns.apply,
            )
        raise RefusedError(f"Unknown subcommand: {sub}")
    except (RefusedError, GraphError, OSError) as err:
        sys.stderr.write(json.dumps({"error": str(err)}) + "\n")
        return EXIT_REFUSED if isinstance(err, RefusedError) else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
