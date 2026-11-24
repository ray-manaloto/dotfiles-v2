"""Shared stdlib-only detection of inactive orphaned `<tool>-<8-char>` mise staging dirs."""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
from pathlib import Path
from xml.parsers.expat import ExpatError

_STAGING_DIR_RE = re.compile(r".+-[a-z2-7]{8}")


def _collect_installs_symlink_targets(
    installs_dir: Path,
) -> tuple[list[tuple[str, str]] | None, str | None]:
    """Collect `(parent_realpath, target_realpath)` pairs for symlinks at depth 1-2 under `installs_dir`."""
    pairs: list[tuple[str, str]] = []
    try:
        with os.scandir(installs_dir) as top_it:
            top_entries = list(top_it)
    except OSError as exc:
        return None, f"failed to scan {installs_dir}: {exc}"
    installs_real = os.path.realpath(installs_dir)
    for entry in top_entries:
        try:
            if entry.is_symlink():
                pairs.append((installs_real, os.path.realpath(entry.path)))
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            parent_real = os.path.realpath(entry.path)
            with os.scandir(entry.path) as sub_it:
                for sub in sub_it:
                    if sub.is_symlink():
                        pairs.append((parent_real, os.path.realpath(sub.path)))
        except OSError as exc:
            return None, f"failed to scan {entry.path}: {exc}"
    return pairs, None


def _collect_mise_ls_install_refs(home: Path) -> tuple[set[str] | None, str | None]:
    """Query `mise -C <home> ls --installed --json` and collect raw/norm/real path references."""
    try:
        proc = subprocess.run(
            ["mise", "-C", str(home), "ls", "--installed", "--json"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"`mise -C {home} ls --installed --json` failed: {exc}"
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"rc={proc.returncode}"
        return (
            None,
            f"`mise -C {home} ls --installed --json` exited {proc.returncode}: {detail}",
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return (
            None,
            f"`mise -C {home} ls --installed --json` returned invalid JSON: {exc}",
        )
    if not isinstance(data, dict):
        return (
            None,
            f"`mise -C {home} ls --installed --json` returned {type(data).__name__}, expected object",
        )
    refs: set[str] = set()
    for rows in data.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("install_path", "symlinked_to"):
                val = row.get(key)
                if isinstance(val, str) and val:
                    refs.add(val)
                    refs.add(os.path.normpath(val))
                    refs.add(os.path.realpath(val))
    return refs, None


def _collect_ps_cmdlines() -> tuple[list[str] | None, str | None]:
    """Return running process command lines from `ps -axo args=` or an error string."""
    try:
        proc = subprocess.run(
            ["ps", "-axo", "args="],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"`ps -axo args=` failed: {exc}"
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"rc={proc.returncode}"
        return None, f"`ps -axo args=` exited {proc.returncode}: {detail}"
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return None, "`ps -axo args=` returned empty process list"
    return lines, None


def _add_path_variants(raw: str, home: Path, out: set[str]) -> None:
    """Record raw, tilde-expanded, normalized, and realpath variants of a string."""
    if not raw:
        return
    out.add(raw)
    expanded = os.path.join(str(home), raw[2:]) if raw.startswith("~/") else raw
    out.add(expanded)
    if expanded.startswith("/"):
        out.add(os.path.normpath(expanded))
        out.add(os.path.realpath(expanded))


def _extract_plist_strings(obj: object, home: Path, out: set[str]) -> None:
    """Recursively collect string and path tokens from a parsed LaunchAgent plist."""
    if isinstance(obj, str):
        _add_path_variants(obj, home, out)
        if "/" in obj and ":" in obj:
            for part in obj.split(":"):
                if "/" in part:
                    _add_path_variants(part, home, out)
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _extract_plist_strings(k, home, out)
            _extract_plist_strings(v, home, out)
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _extract_plist_strings(item, home, out)


def _collect_launchagent_refs(home: Path) -> tuple[set[str] | None, str | None]:
    """Collect string/path references from `~/Library/LaunchAgents/*.plist` (fail closed on error)."""
    agents_dir = home / "Library" / "LaunchAgents"
    if not os.path.lexists(agents_dir):
        return set(), None
    if agents_dir.is_symlink() and not agents_dir.exists():
        return set(), None
    if not agents_dir.is_dir():
        return None, f"LaunchAgents path is not a directory: {agents_dir}"
    try:
        with os.scandir(agents_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as exc:
        return None, f"failed to scan {agents_dir}: {exc}"

    refs: set[str] = set()
    for entry in entries:
        if not entry.name.endswith(".plist"):
            continue
        try:
            if entry.is_symlink() and not os.path.exists(entry.path):
                continue
            with open(entry.path, "rb") as fh:
                payload = plistlib.load(fh)
        except (OSError, plistlib.InvalidFileException, ExpatError, ValueError) as exc:
            return None, f"failed to parse LaunchAgent plist {entry.path}: {exc}"
        _extract_plist_strings(payload, home, refs)
    return refs, None


def find_orphaned_mise_staging_dirs(
    installs_dir: Path, home: Path
) -> tuple[list[Path], list[str]]:
    """Return `(orphaned_dirs, probe_errors)` for inactive `<tool>-<8-char>` mise staging dirs."""
    if not installs_dir.is_dir():
        return [], []
    try:
        with os.scandir(installs_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as exc:
        return [], [f"staging-dir-check-failed: failed to scan {installs_dir}: {exc}"]

    candidates: list[Path] = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError as exc:
            return [], [
                f"staging-dir-check-failed: failed to inspect {entry.path}: {exc}"
            ]
        if not _STAGING_DIR_RE.fullmatch(entry.name):
            continue
        cand = Path(entry.path)
        backend_marker = cand / ".mise.backend.toml"
        try:
            os.lstat(backend_marker)
            continue
        except FileNotFoundError:
            pass
        except OSError as exc:
            return [], [
                f"staging-dir-check-failed: failed to inspect {backend_marker}: {exc}"
            ]
        candidates.append(cand)
    if not candidates:
        return [], []

    ls_refs, ls_err = _collect_mise_ls_install_refs(home)
    if ls_refs is None:
        return [], [f"staging-dir-check-failed: {ls_err}"]
    ps_lines, ps_err = _collect_ps_cmdlines()
    if ps_lines is None:
        return [], [f"staging-dir-check-failed: {ps_err}"]
    launch_refs, la_err = _collect_launchagent_refs(home)
    if launch_refs is None:
        return [], [f"staging-dir-check-failed: {la_err}"]
    symlink_pairs, sym_err = _collect_installs_symlink_targets(installs_dir)
    if symlink_pairs is None:
        return [], [f"staging-dir-check-failed: {sym_err}"]

    orphans: list[Path] = []
    for cand in candidates:
        cand_str = str(cand)
        cand_norm = os.path.normpath(cand_str)
        cand_real = os.path.realpath(cand)
        real_prefix = f"{cand_real}{os.sep}"
        norm_prefix = f"{cand_norm}{os.sep}"
        str_prefix = f"{cand_str}{os.sep}"

        if any(
            parent_real != cand_real
            and (target_real == cand_real or target_real.startswith(real_prefix))
            for parent_real, target_real in symlink_pairs
        ):
            continue
        if (
            cand_str in ls_refs
            or cand_norm in ls_refs
            or cand_real in ls_refs
            or any(
                ref.startswith((real_prefix, norm_prefix, str_prefix))
                for ref in ls_refs
            )
        ):
            continue
        if any(
            cand_real in line or cand_str in line or cand_norm in line
            for line in ps_lines
        ):
            continue
        if any(
            cand_real in ref or cand_str in ref or cand_norm in ref
            for ref in launch_refs
        ):
            continue
        orphans.append(cand)
    return orphans, []
