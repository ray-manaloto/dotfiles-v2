#!/usr/bin/env python3
"""Fast deterministic project & Option A host checks for `mise doctor project` and `hk.pkl`."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

FORBIDDEN_PROJECT_RUNTIME_DEPS = frozenset(
    {
        "ruff",
        "ty",
        "uv",
        "hk",
        "pkl",
        "agnix",
        "betterleaks",
        "tombi",
        "datamodel-code-generator",
        "pytest",
        "pytest-xdist",
        "pytest-cov",
        "dirty-equals",
    }
)


def _git_tracked(repo_root: Path, *patterns: str) -> list[Path]:
    cmd = ["git", "-C", str(repo_root), "ls-files", "--", *patterns]
    out = subprocess.check_output(cmd, text=True)
    return [repo_root / line for line in out.splitlines() if line.strip()]


def _pkg_name(req: str) -> str:
    return re.split(r"[<>=!~;\[\s]", req.strip(), maxsplit=1)[0].lower()


def check_no_project_tools(repo_root: Path) -> list[str]:
    """Verify root/project mise TOMLs declare zero `[tools]` and `pyproject.toml` stays slim."""
    errors: list[str] = []
    mise_tomls = _git_tracked(
        repo_root,
        "mise.toml",
        "mise.*.toml",
        ".mise.toml",
        ".mise.*.toml",
        "mise/**/mise*.toml",
    )
    for path in mise_tomls:
        if not path.is_file():
            continue
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        rel = path.relative_to(repo_root).as_posix()
        if data.get("tools"):
            errors.append(f"{rel}: project mise config must not declare [tools]")
        if data.get("tool_alias"):
            errors.append(f"{rel}: project mise config must not declare [tool_alias]")

    for forbidden_root_file in ("mise.lock", ".tool-versions", "package.json"):
        if (repo_root / forbidden_root_file).exists():
            errors.append(
                f"Forbidden root tool/node manifest present: {forbidden_root_file}"
            )

    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        py_data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        if py_data.get("tool", {}).get("uv", {}).get("package") is not False:
            errors.append("pyproject.toml: [tool.uv].package must be false")
        runtime_deps = py_data.get("project", {}).get("dependencies", [])
        for dep in runtime_deps:
            name = _pkg_name(str(dep))
            if name in FORBIDDEN_PROJECT_RUNTIME_DEPS:
                errors.append(
                    f"pyproject.toml: dev/lint/global CLI tool `{name}` must not be in [project].dependencies"
                )
    return errors


def _current_mise_os() -> str:
    sys_name = platform.system().lower()
    return "macos" if sys_name == "darwin" else sys_name


def _tool_os_list(spec: Any) -> list[str] | None:
    if isinstance(spec, dict):
        os_val = spec.get("os")
        if isinstance(os_val, list):
            return [str(x).lower() for x in os_val]
    return None


def _tool_version(spec: Any) -> str | None:
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        ver = spec.get("version")
        return str(ver) if ver is not None else None
    return None


def check_mise_lock_integrity(repo_root: Path) -> list[str]:
    """Verify `unix/home/.config/mise/{config.toml,mise.lock,locks/**}` parity and permissions."""
    errors: list[str] = []
    cfg_path = repo_root / "unix" / "home" / ".config" / "mise" / "config.toml"
    lock_path = repo_root / "unix" / "home" / ".config" / "mise" / "mise.lock"
    locks_dir = repo_root / "unix" / "home" / ".config" / "mise" / "locks"

    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    platforms = [str(p) for p in cfg.get("settings", {}).get("lockfile_platforms", [])]
    tools_cfg = cfg.get("tools", {})
    tools_lock = lock.get("tools", {})

    for tool_name, spec in tools_cfg.items():
        want_ver = _tool_version(spec)
        entries = tools_lock.get(tool_name)
        if not isinstance(entries, list) or not entries:
            errors.append(f"mise.lock missing entry for [tools].{tool_name!r}")
            continue
        first = entries[0]
        got_ver = str(first.get("version", ""))
        if want_ver and got_ver != want_ver:
            errors.append(
                f"mise.lock version mismatch for {tool_name}: config={want_ver}, lock={got_ver}"
            )
        if tool_name.startswith("npm:"):
            aube_info = first.get("aube")
            aube_rel = aube_info.get("path") if isinstance(aube_info, dict) else None
            if not isinstance(aube_rel, str) or not aube_rel:
                errors.append(f"mise.lock missing aube.path for {tool_name}@{want_ver}")
                continue
            aube_dir = repo_root / "unix" / "home" / ".config" / "mise" / aube_rel
            if not aube_dir.is_dir():
                errors.append(
                    f"Missing npm lock directory {aube_dir.relative_to(repo_root)} referenced by mise.lock"
                )
                continue
            for required_file in ("package.json", "aube-lock.yaml"):
                fpath = aube_dir / required_file
                if not fpath.is_file():
                    errors.append(f"Missing {fpath.relative_to(repo_root)}")
        else:
            allowed_os = _tool_os_list(spec)
            for plat in platforms:
                plat_os = "macos" if plat.startswith("macos-") else "linux"
                if allowed_os and plat_os not in allowed_os:
                    continue
                plat_key = f"platforms.{plat}"
                if plat_key not in first:
                    errors.append(
                        f"mise.lock missing {plat_key} for {tool_name} ({want_ver})"
                    )

    if locks_dir.is_dir():
        for file_path in sorted(locks_dir.rglob("*")):
            if file_path.is_file():
                mode = file_path.stat().st_mode & 0o777
                if (mode & 0o044) != 0o044:
                    errors.append(
                        f"Non-secret lockfile has restrictive mode {oct(mode)} (expected world-readable 0644): "
                        f"{file_path.relative_to(repo_root)}"
                    )
    return errors


def _is_dotfiles_v2_mise_cutover(home: Path | None = None) -> tuple[bool, Path]:
    base_home = home or Path.home()
    host_mise = base_home / ".config" / "mise"
    if not host_mise.is_symlink():
        return False, host_mise
    resolved = host_mise.resolve(strict=False)
    is_v2 = (
        resolved.parts[-3:] == (".config", "mise")
        or (
            len(resolved.parts) >= 4
            and resolved.parts[-4:] == ("unix", "home", ".config", "mise")
            and any("dotfiles-v2" in part for part in resolved.parts)
        )
    ) and any("dotfiles-v2" in part for part in resolved.parts)
    return is_v2, resolved


def _is_legacy_dotfiles_target(target: Path) -> bool:
    for part in target.parts:
        if part == "dotfiles" or (
            part.startswith("dotfiles.") and "dotfiles-v2" not in part
        ):
            return True
    return False


def check_host_option_a(
    repo_root: Path, *, strict_host: bool = False
) -> tuple[list[str], str]:
    """Run Option A host checks (pre-cutover aware until `~/.config/mise` points to dotfiles-v2)."""
    if os.environ.get("CI"):
        return [], "CI: skipped live host Option A checks"

    home = Path.home()
    is_cutover, resolved_mise = _is_dotfiles_v2_mise_cutover(home)
    if not is_cutover and not strict_host:
        return [], (
            f"PRE-CUTOVER: ~/.config/mise ({resolved_mise}) does not yet symlink to dotfiles-v2; "
            "strict Option A host checks deferred until Issue #9 cutover"
        )

    errors: list[str] = []
    if not is_cutover:
        errors.append(
            f"host-mise-parity: ~/.config/mise must symlink to dotfiles-v2/unix/home/.config/mise (got {resolved_mise})"
        )

    cfg = tomllib.loads(
        (repo_root / "unix" / "home" / ".config" / "mise" / "config.toml").read_text(
            encoding="utf-8"
        )
    )
    cur_os = _current_mise_os()
    try:
        ls_raw = subprocess.check_output(
            ["mise", "-C", str(home), "ls", "--json"],
            text=True,
            timeout=10,
        )
        ls_data = json.loads(ls_raw)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        errors.append(f"host-mise-parity: failed to run `mise -C ~ ls --json`: {exc}")
        ls_data = {}

    if isinstance(ls_data, dict):
        for tool_name, spec in cfg.get("tools", {}).items():
            allowed_os = _tool_os_list(spec)
            if allowed_os and cur_os not in allowed_os:
                continue
            want_ver = _tool_version(spec)
            entries = ls_data.get(tool_name)
            if not isinstance(entries, list) or not entries:
                errors.append(
                    f"host-mise-parity: missing global tool {tool_name}@{want_ver}"
                )
                continue
            active = [
                e
                for e in entries
                if isinstance(e, dict)
                and e.get("active") is True
                and (
                    not want_ver
                    or str(e.get("requested_version") or e.get("version")) == want_ver
                )
            ]
            if not active:
                errors.append(
                    f"host-mise-parity: global tool {tool_name}@{want_ver} not active in `mise -C ~ ls --json`"
                )

    # no-legacy-symlinks + orphan check
    scan_roots = (
        home / ".config" / "mise",
        home / ".config" / "agents",
        home / ".gemini" / "GEMINI.md",
        home / ".gemini" / "config" / "hooks",
        home / ".gemini" / "config" / "rules",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".config" / "opencode",
        home / ".zcode",
    )
    for root_item in scan_roots:
        if not root_item.exists() and not root_item.is_symlink():
            continue
        items = [root_item]
        if root_item.is_dir() and not root_item.is_symlink():
            items.extend(sorted(root_item.iterdir()))
        for entry in items:
            if entry.is_symlink():
                raw_target = Path(os.readlink(entry))
                resolved = entry.resolve(strict=False)
                if _is_legacy_dotfiles_target(raw_target) or _is_legacy_dotfiles_target(
                    resolved
                ):
                    errors.append(
                        f"no-legacy-symlinks: {entry} still links to legacy dotfiles ({resolved})"
                    )
                if not entry.exists() and entry.parent in {
                    home / ".gemini" / "config" / "hooks",
                    home / ".gemini" / "config" / "rules",
                    home / ".agents" / "skills",
                }:
                    errors.append(
                        f"orphan-symlink: dangling symlink {entry} -> {raw_target}"
                    )

    # roll-ownership
    try:
        tasks_raw = subprocess.check_output(
            ["mise", "-C", str(home), "tasks", "--json"],
            text=True,
            timeout=10,
        )
        tasks_list = json.loads(tasks_raw)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        errors.append(f"roll-ownership: failed to run `mise -C ~ tasks --json`: {exc}")
        tasks_list = []

    task_sources = {
        str(row.get("name")): str(row.get("source") or "")
        for row in tasks_list
        if isinstance(row, dict)
    }
    for sub in ("snapshot", "restart", "stop", "start", "verify", "self"):
        tname = f"roll:{sub}"
        src = task_sources.get(tname, "")
        if "dotfiles-v2" not in src or "/tasks/roll/" not in src:
            errors.append(
                f"roll-ownership: global task `{tname}` source {src!r} does not resolve inside dotfiles-v2/unix/home/.config/mise/tasks/roll/"
            )

    # host-dotfiles-status (`mise dot status --json`)
    try:
        dot_raw = subprocess.check_output(
            ["mise", "-C", str(home), "dot", "status", "--json"],
            text=True,
            timeout=10,
        )
        dot_data = json.loads(dot_raw)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        errors.append(
            f"host-dotfiles-status: failed to run `mise -C ~ dot status --json`: {exc}"
        )
        dot_data = []

    if isinstance(dot_data, list):
        for entry in dot_data:
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status") or "")
            if status and status not in {"ok", "synced", "match"}:
                errors.append(
                    f"host-dotfiles-status: {entry.get('target')} status={status}"
                )

    return (
        errors,
        "STRICT Option A host parity, roll-ownership, and dotfiles status verified",
    )


def post_commit_advisory(repo_root: Path) -> int:
    """Fast non-blocking post-commit notice when `unix/home/**` changed."""
    try:
        changed = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo_root),
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "HEAD",
            ],
            text=True,
            timeout=5,
        ).splitlines()
    except OSError, subprocess.SubprocessError:
        return 0
    home_changes = [line for line in changed if line.startswith("unix/home/")]
    if home_changes:
        sys.stdout.write(
            f"post-commit advisory: {len(home_changes)} unix/home/ file(s) changed; "
            "run `mise run host:sync-gemini` or `mise dot apply` if syncing live host.\n"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fast deterministic dotfiles-v2 gates")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("no-project-tools")
    sub.add_parser("mise-lock-integrity")
    p_host = sub.add_parser("host-option-a")
    p_host.add_argument("--strict-host", action="store_true")
    sub.add_parser("post-commit-advisory")

    args = parser.parse_args(argv)
    repo_root = Path.cwd().resolve()

    if args.cmd == "no-project-tools":
        errs = check_no_project_tools(repo_root)
        if errs:
            sys.stderr.write("\n".join(errs) + "\n")
            return 1
        sys.stdout.write(
            "no-project-tools: root/project mise TOMLs and pyproject.toml clean\n"
        )
        return 0

    if args.cmd == "mise-lock-integrity":
        errs = check_mise_lock_integrity(repo_root)
        if errs:
            sys.stderr.write("\n".join(errs) + "\n")
            return 1
        sys.stdout.write(
            "mise-lock-integrity: mise.lock and locks/ match config.toml across all platforms\n"
        )
        return 0

    if args.cmd == "host-option-a":
        errs, msg = check_host_option_a(repo_root, strict_host=args.strict_host)
        if errs:
            sys.stderr.write("\n".join(errs) + "\n")
            return 1
        sys.stdout.write(f"host-option-a: {msg}\n")
        return 0

    if args.cmd == "post-commit-advisory":
        return post_commit_advisory(repo_root)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
