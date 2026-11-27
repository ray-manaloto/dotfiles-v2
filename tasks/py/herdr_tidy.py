#!/usr/bin/env python3
"""Close finished Herdr agent tabs whose worktree is gone (`herdr:tidy`, and step 4b of `pr:land`).

A tab is closed only when all of these hold:
- it runs a coding agent (claude-code, codex, antigravity, opencode); plain shells are never touched;
- with `--cwd` (pr:land): its folder is that worktree or inside it, and its status is `done` or `idle`;
- without `--cwd` (sweep): its status is `done` and its folder, under $HOME on this machine, is gone;
- a `working`, `blocked` or other status is never closed; paths are compared after realpath;
- it is not this terminal (`$HC_TERMINAL_ID`) and its tab name is not in `--keep`.

Dry run by default; `--apply` runs `hc kill` then `hc rm` (the tab leaves the Herdr window).
Exit: 0 (nothing to do, or all closed), 1 (a close failed), 2 (`hc` unavailable or unreadable).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

AGENTS = frozenset({"claude-code", "codex", "antigravity", "opencode"})
FINISHED = frozenset({"done", "idle"})

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def tab_name(term: dict[str, object]) -> str:
    """`where` is "<space> / <tab>"; the tab name is the part after the last " / "."""
    where = str(term.get("where") or "")
    return where.rsplit(" / ", 1)[-1]


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def plan(
    terms: Sequence[dict[str, object]],
    *,
    cwd: Path | None = None,
    keep: frozenset[str] = frozenset(),
    me: str = "",
    exists: Callable[[Path], bool] = Path.exists,
) -> list[tuple[dict[str, object], str]]:
    """Return (terminal, reason) for every tab to close; the rules are in the module docstring."""
    out: list[tuple[dict[str, object], str]] = []
    root = Path(os.path.realpath(cwd)) if cwd is not None else None
    home = Path(os.path.realpath(Path.home()))
    for term in terms:
        address = str(term.get("address") or "")
        folder = str(term.get("cwd") or "")
        if not address or not os.path.isabs(folder) or term.get("agent") not in AGENTS:
            continue
        if address == me or tab_name(term) in keep:
            continue
        path = Path(os.path.realpath(folder))
        if root is not None:
            # pr:land just removed this worktree, so an idle agent there has nothing left to do.
            if term.get("status") in FINISHED and _inside(path, root):
                out.append((term, f"in landed worktree {root.name}"))
        elif term.get("status") == "done" and _inside(path, home) and not exists(path):
            # Sweep mode: only agents that ended their turn, only local folders (a remote
            # machine's cwd never exists here), never idle peers such as the agy leads.
            out.append((term, "folder is gone"))
    return out


def list_terminals(run: Runner = _run) -> list[dict[str, object]]:
    proc = run(["hc", "ls", "--json"])
    if proc.returncode != 0:
        raise RuntimeError(
            f"hc ls failed (rc {proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    data = json.loads(proc.stdout or "[]")
    items = data if isinstance(data, list) else data.get("terminals", [])
    return [t for t in items if isinstance(t, dict)]


def close(address: str, run: Runner = _run) -> str | None:
    """Kill then remove one terminal; return an error string, or None on success."""
    killed = run(["hc", "kill", address])
    removed = run(["hc", "rm", address])
    if removed.returncode != 0:
        return f"hc rm {address} rc {removed.returncode} (kill rc {killed.returncode}): {removed.stderr.strip()[:200]}"
    return None


def main(argv: Sequence[str] | None = None, run: Runner = _run) -> int:
    ap = argparse.ArgumentParser(prog="herdr:tidy", description=__doc__.splitlines()[0])
    ap.add_argument(
        "--apply", action="store_true", help="close the tabs (default: dry run)"
    )
    ap.add_argument(
        "--cwd",
        type=Path,
        help="only tabs in this folder (a worktree pr:land just removed)",
    )
    ap.add_argument(
        "--keep", default="", help="comma-separated tab names never to close"
    )
    args = ap.parse_args(argv)

    if run is _run and shutil.which("hc") is None:
        sys.stderr.write("herdr:tidy: hc not on PATH; skipped\n")
        return 2
    try:
        terms = list_terminals(run)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:  # fmt: skip
        sys.stderr.write(f"herdr:tidy: cannot list terminals: {exc}\n")
        return 2

    keep = frozenset(k.strip() for k in args.keep.split(",") if k.strip())
    cwd = args.cwd.expanduser().absolute() if args.cwd else None
    todo = plan(terms, cwd=cwd, keep=keep, me=os.environ.get("HC_TERMINAL_ID", ""))
    if not todo:
        sys.stdout.write("herdr:tidy: no finished agent tabs to close\n")
        return 0

    failures = 0
    verb = "closed" if args.apply else "would close"
    for term, reason in todo:
        address = str(term["address"])
        label = f"{tab_name(term)} ({address}) [{term.get('agent')}, {term.get('status')}]: {reason}"
        err = close(address, run) if args.apply else None
        if err:
            failures += 1
            sys.stderr.write(f"herdr:tidy: FAILED {label}: {err}\n")
        else:
            sys.stdout.write(f"herdr:tidy: {verb} {label}\n")
    if not args.apply:
        sys.stdout.write("herdr:tidy: dry run; pass --apply to close them\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
