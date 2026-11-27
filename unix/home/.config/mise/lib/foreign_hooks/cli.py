"""CLI for `hooks:remove-foreign`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from foreign_hooks import core

ASSET_REL = Path("assets") / "foreign-agent-hooks.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hooks:remove-foreign")
    parser.add_argument(
        "--apply", action="store_true", help="write changes (default: dry-run)"
    )
    parser.add_argument("--home", type=Path, default=None, help="override HOME (tests)")
    parser.add_argument("--markers", type=Path, default=None, help="override asset")
    args = parser.parse_args(argv)
    home = args.home or Path.home()
    asset = args.markers or Path(__file__).resolve().parents[2] / ASSET_REL
    try:
        markers = core.load_markers(asset)
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write(f"hooks:remove-foreign: cannot load markers ({exc})\n")
        return 2
    actions, errors = core.remove(home, markers, apply=args.apply)
    for line in actions:
        sys.stdout.write(line + "\n")
    for line in errors:
        sys.stderr.write(f"ERROR {line}\n")
    if not actions and not errors:
        sys.stdout.write("hooks:remove-foreign: nothing to remove\n")
    elif not args.apply and actions:
        sys.stdout.write("dry-run: re-run with --apply to back up and remove\n")
    return 1 if errors else 0
