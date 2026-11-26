#!/usr/bin/env python3
"""Fast deterministic project & Option A host checks for `mise doctor project` and `hk.pkl`."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
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

ALLOWED_DOTFILES_MERGE_RELATIVE: frozenset[str] = frozenset()
EXPECTED_PROGRAMS: dict[str, list[str]] = {
    "antigravity": ["agy"],
    "codex": ["codex", "--no-daemon"],
    "opencode": ["opencode", "--standalone"],
}
COMPILED_BINARY_MAGICS = (
    b"\x7fELF",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
)
BINARY_HARDEN_SKIP_DIRS = frozenset(
    {"site-packages", "node_modules", ".venv", ".git", "__pycache__"}
)


def _live_host_skipped() -> bool:
    """True when live-host probes must not run: CI runners, or pre-push repo scope.

    The pre-push hook sets DOTFILES_DOCTOR_SCOPE=repo so a push depends only on the
    branch; committed-file assertions in each check still run in that scope.
    """
    return (
        bool(os.environ.get("CI")) or os.environ.get("DOTFILES_DOCTOR_SCOPE") == "repo"
    )


def _skip_reason(what: str) -> str:
    scope = "CI" if os.environ.get("CI") else "DOTFILES_DOCTOR_SCOPE=repo"
    return f"{scope}: {what}"


def _git_tracked(repo_root: Path, *patterns: str) -> list[Path]:
    cmd = ["git", "-C", str(repo_root), "ls-files", "--", *patterns]
    out = subprocess.check_output(cmd, text=True)
    return [repo_root / line for line in out.splitlines() if line.strip()]


def _pkg_name(req: str) -> str:
    base = re.split(r"[<>=!~;\[\s]", req.strip(), maxsplit=1)[0].lower()
    return re.sub(r"[-_.]+", "-", base)


def _main_checkout_root(repo_root: Path) -> Path:
    try:
        common_raw = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError):  # fmt: skip
        return repo_root.resolve(strict=False)
    if not common_raw:
        return repo_root.resolve(strict=False)
    common_path = Path(common_raw)
    if not common_path.is_absolute():
        common_path = repo_root / common_path
    common_resolved = common_path.resolve(strict=False)
    if common_resolved.name == ".git":
        return common_resolved.parent
    return repo_root.resolve(strict=False)


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


def _is_npm_or_aube_lock_entries(tool_name: str, entries: list[dict[str, Any]]) -> bool:
    return tool_name.startswith("npm:") or any(
        str(e.get("backend", "")).startswith("npm:") or isinstance(e.get("aube"), dict)
        for e in entries
    )


def _normalize_registry_entries(
    raw_reg_map: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, list[str]]], dict[str, list[str]]]:
    reg_map: dict[str, tuple[str, list[str]]] = {}
    short_to_backends: dict[str, list[str]] = {}
    for backend, val in raw_reg_map.items():
        items: list[tuple[str, list[str]]]
        if isinstance(val, tuple) and len(val) == 2 and isinstance(val[0], str):
            items = [(val[0], list(val[1]))]
        elif isinstance(val, list):
            items = [(str(s), list(b)) for s, b in val]
        else:
            continue
        backend_norm = str(backend).split("[", 1)[0].lower()
        for short, backends in items:
            if backend_norm:
                reg_map.setdefault(backend_norm, (short, backends))
            short_to_backends.setdefault(short.lower(), backends)
    return reg_map, short_to_backends


def check_mise_lock_integrity(repo_root: Path) -> list[str]:
    """Verify `unix/home/.config/mise/{config*.toml,mise*.lock,locks/**}` parity and permissions."""
    errors: list[str] = []
    cfg_path = repo_root / "unix" / "home" / ".config" / "mise" / "config.toml"
    lock_path = repo_root / "unix" / "home" / ".config" / "mise" / "mise.lock"
    locks_dir = repo_root / "unix" / "home" / ".config" / "mise" / "locks"

    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    platforms = [str(p) for p in cfg.get("settings", {}).get("lockfile_platforms", [])]
    tools_cfg = cfg.get("tools", {})
    tools_lock = lock.get("tools", {})

    short_to_backends: dict[str, list[str]] = {}
    try:
        _, short_to_backends = _normalize_registry_entries(_load_mise_registry_map())
    except (OSError, subprocess.SubprocessError):  # fmt: skip
        short_to_backends = {}

    def _is_canonical_npm_tool(
        name: str, entry_dicts: list[dict[str, Any]] | None = None
    ) -> bool:
        base = name.split("[", 1)[0].lower()
        if base.startswith("npm:"):
            return True
        if entry_dicts and _is_npm_or_aube_lock_entries(name, entry_dicts):
            return True
        backends = short_to_backends.get(base, [])
        return bool(backends and backends[0].lower().startswith("npm:"))

    resolved_root = repo_root.resolve(strict=False)
    mise_cfg_root = resolved_root / "unix" / "home" / ".config" / "mise"
    referenced_aube_dirs: set[Path] = set()
    npm_lock_unresolved = False
    for tool_name, spec in tools_cfg.items():
        want_ver = _tool_version(spec)
        entries = tools_lock.get(tool_name)
        if not isinstance(entries, list) or not entries:
            errors.append(f"mise.lock missing entry for [tools].{tool_name!r}")
            if _is_canonical_npm_tool(tool_name):
                npm_lock_unresolved = True
            continue
        entry_dicts = [e for e in entries if isinstance(e, dict)]
        if not entry_dicts:
            errors.append(f"mise.lock invalid entries for [tools].{tool_name!r}")
            if _is_canonical_npm_tool(tool_name):
                npm_lock_unresolved = True
            continue
        if want_ver:
            matching = [e for e in entry_dicts if str(e.get("version", "")) == want_ver]
            if not matching:
                got_vers = ", ".join(str(e.get("version", "")) for e in entry_dicts)
                errors.append(
                    f"mise.lock version mismatch for {tool_name}: config={want_ver}, lock=[{got_vers}]"
                )
                if _is_canonical_npm_tool(tool_name, entry_dicts):
                    npm_lock_unresolved = True
                continue
        else:
            matching = entry_dicts
        if _is_canonical_npm_tool(tool_name, matching):
            aube_rel: str | None = None
            for entry in matching:
                aube_info = entry.get("aube")
                cand = aube_info.get("path") if isinstance(aube_info, dict) else None
                if isinstance(cand, str) and cand:
                    aube_rel = cand
                    break
            if not aube_rel:
                errors.append(f"mise.lock missing aube.path for {tool_name}@{want_ver}")
                npm_lock_unresolved = True
                continue
            aube_dir = (mise_cfg_root / aube_rel).resolve(strict=False)
            if not aube_dir.is_relative_to(resolved_root):
                errors.append(
                    f"mise.lock aube.path escapes repository root for {tool_name}@{want_ver}: {aube_rel!r}"
                )
                npm_lock_unresolved = True
                continue
            referenced_aube_dirs.add(aube_dir)
            if not aube_dir.is_dir():
                errors.append(
                    f"Missing npm lock directory {aube_dir.relative_to(resolved_root)} referenced by mise.lock"
                )
                continue
            for required_file in ("package.json", "aube-lock.yaml"):
                fpath = aube_dir / required_file
                if not fpath.is_file():
                    errors.append(f"Missing {fpath.relative_to(resolved_root)}")
        elif any(isinstance(e.get("uv"), dict) for e in matching):
            # pypi tools lock a uv dependency graph (locks/<tool>/<ver>~<hash>/{pyproject.toml,uv.lock})
            # instead of per-platform download URLs.
            uv_rel = next(
                (
                    str(e["uv"].get("path"))
                    for e in matching
                    if isinstance(e.get("uv"), dict) and e["uv"].get("path")
                ),
                "",
            )
            uv_dir = (mise_cfg_root / uv_rel).resolve(strict=False)
            if not uv_rel or not uv_dir.is_relative_to(resolved_root):
                errors.append(
                    f"mise.lock invalid uv.path for {tool_name}@{want_ver}: {uv_rel!r}"
                )
                continue
            referenced_aube_dirs.add(uv_dir)
            for required_file in ("pyproject.toml", "uv.lock"):
                fpath = uv_dir / required_file
                if not fpath.is_file():
                    errors.append(f"Missing {fpath.relative_to(resolved_root)}")
        else:
            allowed_os = _tool_os_list(spec)
            for plat in platforms:
                plat_os = "macos" if plat.startswith("macos-") else "linux"
                if allowed_os and plat_os not in allowed_os:
                    continue
                plat_key = f"platforms.{plat}"
                if not any(plat_key in entry for entry in matching):
                    errors.append(
                        f"mise.lock missing {plat_key} for {tool_name} ({want_ver})"
                    )

    for orphan_lock_tool in sorted(set(tools_lock) - set(tools_cfg)):
        errors.append(
            f"mise.lock has orphaned tool entry not in config.toml: {orphan_lock_tool!r}"
        )

    if lock_path.is_file():
        lock_mode = lock_path.stat().st_mode & 0o777
        if (lock_mode & 0o044) != 0o044:
            errors.append(
                f"Non-secret mise.lock has restrictive mode {oct(lock_mode)} (expected world-readable 0644)"
            )

    for env_lock in sorted(mise_cfg_root.glob("mise.*.lock")):
        env_mode = env_lock.stat().st_mode & 0o777
        if (env_mode & 0o044) != 0o044:
            errors.append(
                f"Non-secret {env_lock.name} has restrictive mode {oct(env_mode)} (expected world-readable 0644)"
            )
        try:
            env_lock_data = tomllib.loads(env_lock.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:  # fmt: skip
            errors.append(f"Invalid TOML in {env_lock.name}: {exc}")
            continue
        env_name = env_lock.name[len("mise.") : -len(".lock")]
        env_cfg = mise_cfg_root / f"config.{env_name}.toml"
        if env_cfg.is_file():
            env_cfg_tools = tomllib.loads(env_cfg.read_text(encoding="utf-8")).get(
                "tools", {}
            )
            env_lock_tools = env_lock_data.get("tools", {})
            for tname, tspec in env_cfg_tools.items():
                if (
                    not isinstance(env_lock_tools.get(tname), list)
                    or not env_lock_tools[tname]
                ):
                    errors.append(
                        f"{env_lock.name} missing entry for [tools].{tname!r}"
                    )
                    continue
                want = _tool_version(tspec)
                if want and not any(
                    isinstance(e, dict) and str(e.get("version", "")) == want
                    for e in env_lock_tools[tname]
                ):
                    errors.append(
                        f"{env_lock.name} version mismatch for {tname}: config={want}"
                    )

    if locks_dir.is_dir():
        for file_path in sorted(locks_dir.rglob("*")):
            if file_path.is_file():
                mode = file_path.stat().st_mode & 0o777
                if (mode & 0o044) != 0o044:
                    errors.append(
                        f"Non-secret lockfile has restrictive mode {oct(mode)} (expected world-readable 0644): "
                        f"{file_path.resolve(strict=False).relative_to(resolved_root)}"
                    )
        if not npm_lock_unresolved:
            for pkg_dir in sorted(locks_dir.iterdir()):
                if not pkg_dir.is_dir():
                    continue
                for ver_dir in sorted(pkg_dir.iterdir()):
                    if (
                        ver_dir.is_dir()
                        and ver_dir.resolve(strict=False) not in referenced_aube_dirs
                    ):
                        errors.append(
                            f"Unreferenced npm lock directory in locks/: {ver_dir.resolve(strict=False).relative_to(resolved_root)}"
                        )
    return errors


def _is_dotfiles_v2_mise_cutover(
    home: Path | None = None, *, repo_root: Path | None = None
) -> tuple[bool, Path]:
    base_home = home or Path.home()
    host_mise = base_home / ".config" / "mise"
    if not host_mise.is_symlink():
        return False, host_mise
    if not host_mise.is_dir():
        return False, host_mise.resolve(strict=False)
    try:
        resolved = host_mise.resolve(strict=True)
    except OSError:
        return False, host_mise.resolve(strict=False)
    if repo_root is not None:
        main_root = _main_checkout_root(repo_root)
        expected = (main_root / "unix" / "home" / ".config" / "mise").resolve(
            strict=False
        )
        return resolved == expected, resolved
    is_v2 = (
        len(resolved.parts) >= 4
        and resolved.parts[-4:] == ("unix", "home", ".config", "mise")
        and any("dotfiles-v2" in part for part in resolved.parts)
    )
    return is_v2, resolved


def _is_legacy_dotfiles_target(target: Path) -> bool:
    for part in target.parts:
        if part == "dotfiles" or (
            part.startswith(("dotfiles.", "dotfiles-legacy"))
            and "dotfiles-v2" not in part
        ):
            return True
    return False


_SKIP_MANAGED_DIR_NAMES = frozenset(
    {
        ".git",
        ".tmp",
        ".venv",
        "__pycache__",
        "archived_sessions",
        "cache",
        "Cache",
        "log",
        "logs",
        "node_modules",
        "sessions",
        "sqlite",
        "tmp",
        "worktrees",
    }
)


def _iter_managed_entries(root_item: Path, *, max_depth: int = 3) -> list[Path]:
    items = [root_item]
    if not root_item.is_dir() or root_item.is_symlink():
        return items

    def _walk(cur: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(cur.iterdir())
        except OSError:
            return
        for child in children:
            items.append(child)
            if (
                child.name not in _SKIP_MANAGED_DIR_NAMES
                and child.is_dir()
                and not child.is_symlink()
                and depth < max_depth
            ):
                _walk(child, depth + 1)

    _walk(root_item, 1)
    return items


def check_host_option_a(
    repo_root: Path, *, strict_host: bool = False
) -> tuple[list[str], str]:
    """Run Option A host checks (pre-cutover aware until `~/.config/mise` points to dotfiles-v2)."""
    if _live_host_skipped():
        return [], _skip_reason("skipped live host Option A checks")

    home = Path.home()
    is_cutover, resolved_mise = _is_dotfiles_v2_mise_cutover(home, repo_root=repo_root)
    if not is_cutover and not strict_host:
        return [], (
            f"PRE-CUTOVER: ~/.config/mise ({resolved_mise}) does not yet symlink to dotfiles-v2; "
            "strict Option A host checks deferred until Issue #9 cutover"
        )

    errors: list[str] = []
    expected_mise = (
        _main_checkout_root(repo_root) / "unix" / "home" / ".config" / "mise"
    ).resolve(strict=False)
    if not is_cutover:
        errors.append(
            f"host-mise-parity: ~/.config/mise must be an existing symlink to {expected_mise} (got {resolved_mise})"
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

    worktree_ls_data: dict[str, Any] = {}
    if repo_root.resolve(strict=False) != _main_checkout_root(repo_root).resolve(
        strict=False
    ):
        try:
            wt_raw = subprocess.check_output(
                [
                    "mise",
                    "-C",
                    str(repo_root / "unix" / "home" / ".config" / "mise"),
                    "ls",
                    "--json",
                ],
                text=True,
                timeout=10,
            )
            parsed_wt = json.loads(wt_raw)
            if isinstance(parsed_wt, dict):
                worktree_ls_data = parsed_wt
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):  # fmt: skip
            worktree_ls_data = {}

    if isinstance(ls_data, dict):
        for tool_name, spec in cfg.get("tools", {}).items():
            allowed_os = _tool_os_list(spec)
            if allowed_os and cur_os not in allowed_os:
                continue
            want_ver = _tool_version(spec)
            entries = ls_data.get(tool_name)
            active = [
                e
                for e in (entries if isinstance(entries, list) else [])
                if isinstance(e, dict)
                and e.get("active") is True
                and (
                    not want_ver
                    or str(e.get("requested_version") or e.get("version")) == want_ver
                )
            ]
            if not active and worktree_ls_data:
                wt_entries = worktree_ls_data.get(tool_name)
                active = [
                    e
                    for e in (wt_entries if isinstance(wt_entries, list) else [])
                    if isinstance(e, dict)
                    and e.get("installed") is True
                    and (
                        not want_ver
                        or str(e.get("requested_version") or e.get("version"))
                        == want_ver
                    )
                ]
            if not entries and not active:
                errors.append(
                    f"host-mise-parity: missing global tool {tool_name}@{want_ver}"
                )
                continue
            if not active:
                errors.append(
                    f"host-mise-parity: global tool {tool_name}@{want_ver} not active in `mise -C ~ ls --json`"
                )

    # no-legacy-symlinks + orphan check (bounded depth across managed trees)
    scan_roots = (
        home / ".config" / "mise",
        home / ".agents" / "AGENTS.md",
        home / ".config" / "agents",
        home / ".codex",
        home / ".local" / "bin",
        home / ".config" / "fnox",
        home / ".config" / "git",
        home / ".config" / "herdr-app",
        home / ".config" / "jouet" / "rules",
        home / ".config" / "zsh",
        home / ".config" / "starship.toml",
        home / ".zshenv",
        home / ".claude" / "CLAUDE.md",
        home / ".gemini" / "GEMINI.md",
        home / ".gemini" / "config" / "AGENTS.md",
        home / ".gemini" / "config" / "hooks",
        home / ".gemini" / "config" / "rules",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".config" / "opencode",
        home / ".zcode",
    )
    orphan_managed_parents = {
        home / ".gemini" / "config" / "hooks",
        home / ".gemini" / "config" / "rules",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".local" / "bin",
        home / ".config" / "opencode",
        home / ".zcode",
        home / ".zcode" / "cli",
    }
    codex_dir = home / ".codex"
    for root_item in scan_roots:
        if not root_item.exists() and not root_item.is_symlink():
            continue
        depth_limit = 1 if root_item == codex_dir else 3
        for entry in _iter_managed_entries(root_item, max_depth=depth_limit):
            if entry.is_symlink():
                raw_target = Path(os.readlink(entry))
                resolved = entry.resolve(strict=False)
                if _is_legacy_dotfiles_target(raw_target) or _is_legacy_dotfiles_target(
                    resolved
                ):
                    errors.append(
                        f"no-legacy-symlinks: {entry} still links to legacy dotfiles ({resolved})"
                    )
                if not entry.exists() and entry.parent in orphan_managed_parents:
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
    allowed_repo_prefixes = {
        repo_root.resolve(strict=False).as_posix().rstrip("/") + "/",
        _main_checkout_root(repo_root).resolve(strict=False).as_posix().rstrip("/")
        + "/",
    }
    for sub in ("snapshot", "restart", "stop", "start", "verify", "self"):
        tname = f"roll:{sub}"
        src = task_sources.get(tname, "")
        resolved_src = (
            Path(src).expanduser().resolve(strict=False).as_posix() if src else ""
        )
        if "/unix/home/.config/mise/tasks/roll/" not in resolved_src or not any(
            resolved_src.startswith(prefix) for prefix in allowed_repo_prefixes
        ):
            errors.append(
                f"roll-ownership: global task `{tname}` source {src!r} does not resolve inside repo unix/home/.config/mise/tasks/roll/"
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
        dot_data = {}

    healthy_states = {
        "applied",
        "tracked",
        "absent",
        "not_on_this_machine",
        "ok",
        "synced",
        "match",
    }
    if isinstance(dot_data, dict):
        entries = (dot_data.get("files") or []) + (dot_data.get("edits") or [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            state = str(entry.get("state") or entry.get("status") or "")
            if state and state not in healthy_states:
                target_lbl = (
                    entry.get("target") or entry.get("path") or entry.get("edit")
                )
                errors.append(f"host-dotfiles-status: {target_lbl} state={state}")
        for orphan in dot_data.get("orphaned") or []:
            if isinstance(orphan, dict):
                errors.append(
                    f"host-dotfiles-status: orphaned {orphan.get('target')} state={orphan.get('state', 'orphaned')}"
                )
    elif isinstance(dot_data, list):
        for entry in dot_data:
            if not isinstance(entry, dict):
                continue
            state = str(entry.get("state") or entry.get("status") or "")
            if state and state not in healthy_states:
                errors.append(
                    f"host-dotfiles-status: {entry.get('target')} state={state}"
                )

    return (
        errors,
        "STRICT Option A host parity, roll-ownership, and dotfiles status verified",
    )


def _strip_git_core_from_path(path_value: str, git_exec_path: str = "") -> str:
    """Drop git hook-injected `*/git-core` / `GIT_EXEC_PATH` prefixes without masking real PATH order."""
    norm_exec = git_exec_path.rstrip(os.sep)
    suffix = f"{os.sep}git-core"
    kept = [
        entry
        for entry in path_value.split(os.pathsep)
        if not entry.rstrip(os.sep).endswith(suffix)
        and (not norm_exec or entry.rstrip(os.sep) != norm_exec)
    ]
    return os.pathsep.join(kept)


def _mise_env_path_entries(env: Mapping[str, str], home: Path) -> frozenset[str]:
    """PATH entries mise itself adds (`[env]._.path`, tool bins, wrappers), from `mise env --json` on a bare PATH."""
    mise_bin = shutil.which("mise", path=env.get("PATH"))
    if not mise_bin:
        return frozenset()
    base = ("/usr/bin", "/bin")
    probe_env = {k: v for k, v in env.items() if k not in ("MISE_SHELL", "__MISE_DIFF")}
    probe_env["PATH"] = os.pathsep.join(base)
    try:
        proc = subprocess.run(
            [mise_bin, "-C", str(home), "env", "--json"],
            capture_output=True,
            text=True,
            env=probe_env,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):  # fmt: skip
        return frozenset()
    mise_path = data.get("PATH") if isinstance(data, dict) else None
    if not isinstance(mise_path, str):
        return frozenset()
    return frozenset(
        os.path.normpath(entry)
        for entry in mise_path.split(os.pathsep)
        if entry and os.path.normpath(entry) not in base
    )


def _normalize_path_for_mise_doctor(
    path_value: str,
    env: Mapping[str, str],
    repo_root: Path,
    home: Path,
    mise_env_entries: frozenset[str] = frozenset(),
) -> str:
    """Strip shim-mode `mise` task-injected tool/wrapper/repo-venv and mise `[env]._.path` prefixes when `shims` stays in PATH."""
    cleaned = _strip_git_core_from_path(path_value, env.get("GIT_EXEC_PATH", ""))
    if env.get("MISE_SHELL") or env.get("__MISE_DIFF"):
        return cleaned

    raw_data_dir = env.get("MISE_DATA_DIR")
    if raw_data_dir:
        data_dir = Path(raw_data_dir).expanduser()
    elif env.get("XDG_DATA_HOME"):
        data_dir = Path(env["XDG_DATA_HOME"]).expanduser() / "mise"
    else:
        data_dir = home / ".local" / "share" / "mise"

    bases = tuple(dict.fromkeys([data_dir, data_dir.resolve(strict=False)]))
    shims_dirs = {os.path.normpath(str(d / "shims")) for d in bases}
    entries = [entry for entry in cleaned.split(os.pathsep) if entry]
    if not any(
        os.path.normpath(entry) in shims_dirs or os.path.realpath(entry) in shims_dirs
        for entry in entries
    ):
        return cleaned

    installs_exact = {os.path.normpath(str(d / "installs")) for d in bases}
    installs_prefixes = tuple(f"{p}{os.sep}" for p in installs_exact)
    wrappers_dirs = {
        os.path.normpath(str(d / "command-wrappers" / "bin")) for d in bases
    }
    repo_roots = (repo_root, repo_root.resolve(strict=False))
    venv_dirs = {os.path.normpath(str(r / ".venv" / "bin")) for r in repo_roots}

    def _is_mise_injected(entry: str) -> bool:
        for candidate in (os.path.normpath(entry), os.path.realpath(entry)):
            if (
                candidate in installs_exact
                or candidate.startswith(installs_prefixes)
                or candidate in wrappers_dirs
                or candidate in venv_dirs
                or candidate in mise_env_entries
            ):
                return True
        return False

    return os.pathsep.join(entry for entry in entries if not _is_mise_injected(entry))


def check_mise_doctor_clean(repo_root: Path | None = None) -> tuple[list[str], str]:
    """Verify `mise -C ~ doctor --json` reports 0 errors and 0 warnings."""
    if _live_host_skipped():
        return [], _skip_reason("skipped live host mise-doctor-clean")
    home = Path.home()
    root = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    # Resolve mise before PATH normalization can drop the directory that holds it.
    mise_cmd = shutil.which("mise", path=env.get("PATH")) or "mise"
    if "PATH" in env:
        activated = bool(env.get("MISE_SHELL") or env.get("__MISE_DIFF"))
        mise_env_entries = (
            frozenset() if activated else _mise_env_path_entries(env, home)
        )
        env["PATH"] = _normalize_path_for_mise_doctor(
            env["PATH"], env, root, home, mise_env_entries
        )
    try:
        proc = subprocess.run(
            [mise_cmd, "-C", str(home), "doctor", "--json"],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
        return [
            f"mise-doctor-clean: failed to run `mise -C ~ doctor --json`: {exc}"
        ], ""
    errors: list[str] = []
    if not proc.stdout.strip():
        return [
            f"mise-doctor-clean: empty JSON output (rc={proc.returncode}): {proc.stderr.strip()}"
        ], ""
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return [f"mise-doctor-clean: invalid JSON: {exc}"], ""
    if not isinstance(data, dict):
        return [
            f"mise-doctor-clean: expected JSON object, got {type(data).__name__}"
        ], ""
    if proc.returncode != 0:
        errors.append(f"mise-doctor-clean: exited {proc.returncode}")
    for err in data.get("errors") or []:
        errors.append(f"mise-doctor-clean error: {err}")
    for warn in data.get("warnings") or []:
        errors.append(f"mise-doctor-clean warning: {warn}")
    return errors, "mise -C ~ doctor --json reported 0 errors and 0 warnings"


def _load_ignored_prefixes(mise_dir: Path) -> tuple[Path, ...]:
    miserc = mise_dir / "miserc.toml"
    if not miserc.is_file():
        return ()
    try:
        data = tomllib.loads(miserc.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):  # fmt: skip
        return ()
    raw = data.get("ignored_config_paths")
    if not isinstance(raw, list):
        return ()
    out: list[Path] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            out.append(Path(entry).expanduser().resolve(strict=False))
    return tuple(out)


def _is_ignored_path(path: Path, ignored_prefixes: tuple[Path, ...]) -> bool:
    resolved = path.resolve(strict=False)
    return any(
        resolved == prefix or prefix in resolved.parents for prefix in ignored_prefixes
    )


def _collect_host_tools_and_wrappers(
    mise_dir: Path, state_dir: Path
) -> tuple[set[str], set[str]]:
    declared_wrappers: set[str] = set()
    used_tools: set[str] = set()

    def _collect(cfg_path: Path, *, collect_wrappers: bool = False) -> None:
        if not cfg_path.is_file():
            return
        try:
            data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):  # fmt: skip
            return
        if collect_wrappers and isinstance(data.get("wrappers"), dict):
            declared_wrappers.update(str(k) for k in data["wrappers"])
        if isinstance(data.get("tools"), dict):
            used_tools.update(str(k) for k in data["tools"])

    for name in (
        "config.toml",
        "config.macos.toml",
        "config.linux.toml",
        "config.vm.toml",
        "config.container.toml",
    ):
        _collect(mise_dir / name, collect_wrappers=True)
    conf_d = mise_dir / "conf.d"
    if conf_d.is_dir():
        for fragment in sorted(conf_d.rglob("*.toml")):
            _collect(fragment, collect_wrappers=True)

    ignored_prefixes = _load_ignored_prefixes(mise_dir)
    tracked_dir = state_dir / "tracked-configs"
    if tracked_dir.is_dir():
        for link in sorted(tracked_dir.iterdir()):
            try:
                target = link.resolve(strict=True)
            except OSError:
                continue
            if not target.is_file() or _is_ignored_path(target, ignored_prefixes):
                continue
            _collect(target)
    return declared_wrappers, used_tools


def find_orphaned_mise_staging_dirs(
    installs_dir: Path, home: Path
) -> tuple[list[Path], list[str]]:
    """Delegate to shared stdlib `mise_staging_prune.find_orphaned_mise_staging_dirs`."""
    lib_dir = str(
        Path(__file__).resolve().parents[2]
        / "unix"
        / "home"
        / ".config"
        / "mise"
        / "lib"
    )
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from mise_staging_prune import (
        find_orphaned_mise_staging_dirs as _find_orphaned_mise_staging_dirs,
    )

    return _find_orphaned_mise_staging_dirs(installs_dir, home)


def find_dead_codex_otel(codex_cfg: Path) -> list[str]:
    """Return descriptions of dead `[otel]` stanzas (e.g. `symphony-cpp-local` or `127.0.0.1:4318`) in *codex_cfg*."""
    if not codex_cfg.is_file():
        return []
    try:
        data = tomllib.loads(codex_cfg.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):  # fmt: skip
        return []
    otel_tbl = data.get("otel")
    if not isinstance(otel_tbl, dict):
        return []
    reasons: list[str] = []
    if otel_tbl.get("environment") == "symphony-cpp-local":
        reasons.append("environment=symphony-cpp-local")
    stack: list[dict[object, object]] = [otel_tbl]
    while stack:
        cur = stack.pop()
        ep = cur.get("endpoint")
        if isinstance(ep, str) and (
            ep == "http://127.0.0.1:4318/v1/logs"
            or ep.startswith(("http://127.0.0.1:4318", "http://localhost:4318"))
        ):
            reasons.append(f"endpoint={ep}")
        for val in cur.values():
            if isinstance(val, dict):
                stack.append(val)
    return reasons


def _collect_installed_bin_names(installs_dir: Path) -> set[str]:
    """Collect executable filenames under `installs/` cleanly without walking deep deps."""
    names: set[str] = set()
    if not installs_dir.is_dir():
        return names
    is_darwin = sys.platform == "darwin"

    def _norm(name: str) -> str:
        return name.lower() if is_darwin else name

    def _scan(cur: Path, depth: int) -> None:
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            return
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=True)
                is_file = entry.is_file(follow_symlinks=True)
            except OSError:
                continue
            if is_file:
                if os.access(entry.path, os.X_OK):
                    names.add(_norm(entry.name))
                continue
            if not is_dir or depth >= 4:
                continue
            if cur.name == "node_modules" and entry.name != ".bin":
                continue
            if entry.name in {".git", "__pycache__", ".venv"}:
                continue
            _scan(Path(entry.path), depth + 1)

    _scan(installs_dir, 0)
    return names


def check_host_hygiene(repo_root: Path) -> tuple[list[str], str]:
    """Verify clean host shims, wrappers, plugins, pitchfork daemons, vb, local bins, opencode.jsonc, config.local.toml, and skills."""
    if _live_host_skipped():
        return [], _skip_reason("skipped live host-hygiene")

    home = Path.home()
    data_dir = Path(
        os.environ.get("MISE_DATA_DIR", str(home / ".local" / "share" / "mise"))
    )
    state_dir = Path(
        os.environ.get("MISE_STATE_DIR", str(home / ".local" / "state" / "mise"))
    )
    host_mise_dir = home / ".config" / "mise"
    errors: list[str] = []

    # 1. Dead/broken shims + active global bin-paths check
    installs_dir = data_dir / "installs"
    shims_dir = data_dir / "shims"
    if shims_dir.is_dir():
        is_darwin = sys.platform == "darwin"
        installed_bins = _collect_installed_bin_names(installs_dir)
        shim_names = [
            entry.name
            for entry in sorted(shims_dir.iterdir())
            if entry.name != ".DS_Store" and (entry.is_file() or entry.is_symlink())
        ]
        suspect_shims = [
            name
            for name in shim_names
            if (name.lower() if is_darwin else name) not in installed_bins
        ]

        def _probe_shim(name: str) -> str | None:
            try:
                proc = subprocess.run(
                    ["mise", "-C", str(home), "which", name],
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
                return f"broken-shim: {name} ({exc})"
            if proc.returncode != 0:
                detail = (
                    proc.stderr.strip()
                    or proc.stdout.strip()
                    or f"rc={proc.returncode}"
                )
                if "is a mise bin however it is not currently active" in detail:
                    return None
                return f"broken-shim: {name} ({detail})"
            return None

        if suspect_shims:
            with ThreadPoolExecutor(max_workers=8) as pool:
                for msg in pool.map(_probe_shim, suspect_shims):
                    if msg:
                        errors.append(msg)

    try:
        bin_paths_out = subprocess.check_output(
            ["mise", "-C", str(home), "bin-paths"],
            text=True,
            timeout=10,
        )
        for line in bin_paths_out.splitlines():
            p = line.strip()
            if p and "shims" not in Path(p).parts and not Path(p).is_dir():
                errors.append(f"broken-global-bin-path: {p}")
    except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
        errors.append(f"mise-bin-paths-failed: {exc}")

    # 1b. Inactive orphaned <tool>-<8-char> mise staging/install dirs
    orphaned_staging_dirs, staging_probe_errs = find_orphaned_mise_staging_dirs(
        installs_dir, home
    )
    errors.extend(staging_probe_errs)
    for staging_dir in orphaned_staging_dirs:
        errors.append(
            f"orphaned-mise-staging-dir: {staging_dir} (run `mise run host:prune --apply`)"
        )

    declared_wrappers, used_tools = _collect_host_tools_and_wrappers(
        host_mise_dir, state_dir
    )

    # 2. Orphaned command-wrappers
    wrappers_dir = data_dir / "command-wrappers" / "bin"
    if wrappers_dir.is_dir():
        for entry in sorted(wrappers_dir.iterdir()):
            if entry.name != ".DS_Store" and entry.name not in declared_wrappers:
                errors.append(f"orphaned-command-wrapper: {entry}")

    # 3. Stale unconfigured plugins
    configured_plugin_tokens: set[str] = set()
    for tool in used_tools:
        configured_plugin_tokens.add(tool)
        if ":" in tool:
            backend, rest = tool.split(":", 1)
            configured_plugin_tokens.add(rest.replace("/", "-"))
            configured_plugin_tokens.add(f"{backend}-{rest}".replace("/", "-"))
            configured_plugin_tokens.add(rest.split("/")[-1])

    try:
        reg_plugins = [
            line.strip()
            for line in subprocess.check_output(
                ["mise", "-C", str(home), "plugins", "ls"],
                text=True,
                timeout=10,
            ).splitlines()
            if line.strip()
        ]
    except (OSError, subprocess.SubprocessError):  # fmt: skip
        reg_plugins = []

    for plugin in reg_plugins:
        if plugin not in configured_plugin_tokens:
            errors.append(f"stale-mise-plugin: {plugin}")

    plugins_dir = data_dir / "plugins"
    if plugins_dir.is_dir():
        for pdir in sorted(plugins_dir.iterdir()):
            if pdir.name != ".DS_Store" and pdir.name not in configured_plugin_tokens:
                errors.append(f"orphaned-plugin-dir: {pdir}")

    # 4. Retired stopped pitchfork daemons
    if shutil.which("pitchfork"):
        try:
            pf_raw = subprocess.check_output(
                ["pitchfork", "list", "--json"],
                text=True,
                timeout=10,
            )
            pf_list = json.loads(pf_raw)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            errors.append(
                f"pitchfork-list: failed to query `pitchfork list --json`: {exc}"
            )
            pf_list = []
        if isinstance(pf_list, list):
            for row in pf_list:
                if not isinstance(row, dict):
                    continue
                daemon_id = str(row.get("id") or "")
                ns = str(row.get("namespace") or "")
                status = str(row.get("status") or "")
                is_retired = daemon_id in {
                    "dotfiles-v2/vercel-board",
                    "legacy/pitchfork",
                } or ns.startswith("test.results.")
                if status == "stopped" and is_retired:
                    errors.append(f"retired-pitchfork-daemon: {daemon_id} (stopped)")

    # 5. Interactive zsh `which vb && vb whoami` (only where board env is configured)
    if shutil.which("zsh") and (home / ".config" / "board" / "env.sh").is_file():
        try:
            vb_proc = subprocess.run(
                ["zsh", "-ic", "which vb && vb whoami"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
            errors.append(
                f"vb-zsh-check: `zsh -ic 'which vb && vb whoami'` failed: {exc}"
            )
        else:
            if vb_proc.returncode != 0:
                errors.append(
                    f"vb-zsh-check: `zsh -ic 'which vb && vb whoami'` failed (rc={vb_proc.returncode}): "
                    f"{vb_proc.stderr.strip() or vb_proc.stdout.strip()}"
                )

    # 6. Stale ~/.local/bin/{uv,uvx,hermes,hermes-acp,hermes-agent}
    local_bin = home / ".local" / "bin"
    for tool_bin in ("uv", "uvx"):
        cand = local_bin / tool_bin
        if cand.exists() or cand.is_symlink():
            try:
                managed_bin = subprocess.check_output(
                    ["mise", "-C", str(home), "which", tool_bin],
                    text=True,
                    timeout=10,
                ).strip()
            except (OSError, subprocess.SubprocessError):  # fmt: skip
                managed_bin = ""
            if not managed_bin or Path(managed_bin).resolve(
                strict=False
            ) != cand.resolve(strict=False):
                errors.append(
                    f"stale-local-bin: {cand} conflicts with managed mise `{tool_bin}` ({managed_bin})"
                )

    for hbin in ("hermes", "hermes-acp", "hermes-agent"):
        hcand = local_bin / hbin
        if not (hcand.exists() or hcand.is_symlink()):
            continue
        is_legacy = False
        if hcand.is_symlink():
            raw_link = os.readlink(hcand)
            resolved_link = str(hcand.resolve(strict=False))
            is_legacy = (
                "/.hermes/hermes-agent/" in raw_link
                or "/.hermes/hermes-agent/" in resolved_link
            )
        elif hcand.is_file() and hcand.stat().st_size <= 4096:
            is_legacy = "/.hermes/hermes-agent/" in hcand.read_text(
                encoding="utf-8", errors="ignore"
            )
        if is_legacy or hbin == "hermes":
            managed_hermes = (
                home / ".local" / "share" / "hermes-cli.noindex" / "bin" / "hermes"
            )
            if is_legacy or (
                hbin == "hermes"
                and managed_hermes.is_file()
                and hcand.resolve(strict=False) != managed_hermes.resolve(strict=False)
            ):
                errors.append(f"stale-local-bin: {hcand} shadows managed hermes")

    # 6b. Dangling symlinks or zero-byte completion files in ~/.cache/zsh/completions
    zsh_completions = home / ".cache" / "zsh" / "completions"
    if zsh_completions.is_dir():
        for entry in sorted(zsh_completions.iterdir()):
            if entry.is_symlink() and not entry.exists():
                errors.append(
                    f"stale-zsh-completion: dead symlink {entry} (run `mise run host:prune --apply`)"
                )
            elif entry.is_file() and not entry.is_symlink():
                try:
                    if entry.stat().st_size == 0:
                        errors.append(
                            f"stale-zsh-completion: zero-byte file {entry} (run `mise run host:prune --apply`)"
                        )
                except OSError:
                    pass

    # 7. Unmanaged ~/.config/opencode/opencode.jsonc
    oc_jsonc = home / ".config" / "opencode" / "opencode.jsonc"
    if oc_jsonc.exists() or oc_jsonc.is_symlink():
        errors.append(f"unmanaged-opencode-jsonc: {oc_jsonc}")

    # 7b. Dead [otel] stanza in ~/.codex/config.toml (Tier 0 B1)
    codex_cfg = home / ".codex" / "config.toml"
    dead_otel_reasons = find_dead_codex_otel(codex_cfg)
    if dead_otel_reasons:
        errors.append(
            f"dead-codex-otel: {codex_cfg} has dead [otel] stanza ({', '.join(dead_otel_reasons)}; run `mise run host:prune-codex-otel --apply`)"
        )

    # 8. Untracked config.local.toml
    checked_local_paths: set[Path] = set()
    for base_mise in (
        _main_checkout_root(repo_root) / "unix" / "home" / ".config" / "mise",
        host_mise_dir.resolve(strict=False),
    ):
        cand_local = base_mise / "config.local.toml"
        if cand_local in checked_local_paths:
            continue
        checked_local_paths.add(cand_local)
        if cand_local.exists() or cand_local.is_symlink():
            errors.append(f"untracked-config-local: {cand_local}")

    # 9. ~/.agents/skills/.mise-skills.json + exact parity for `mise -C ~ skills ls`
    skills_dir = home / ".agents" / "skills"
    manifest_path = skills_dir / ".mise-skills.json"
    recorded_links: dict[str, Any] = {}
    if not manifest_path.is_file():
        errors.append(f"skills-manifest-missing: {manifest_path}")
    else:
        try:
            mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(mdata, dict) and isinstance(mdata.get("links"), dict):
                recorded_links = mdata["links"]
            else:
                errors.append(f"skills-manifest-invalid: {manifest_path}")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"skills-manifest-invalid: {manifest_path}: {exc}")

    try:
        skills_raw = subprocess.check_output(
            ["mise", "-C", str(home), "skills", "ls", "--json"],
            text=True,
            timeout=10,
        )
        skills_list = json.loads(skills_raw)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        errors.append(f"skills-ls-failed: {exc}")
        skills_list = []

    if isinstance(skills_list, list):
        for row in skills_list:
            if not isinstance(row, dict):
                continue
            sname = str(row.get("name") or "")
            spath = str(row.get("path") or "")
            if not sname or not spath:
                continue
            link = skills_dir / sname
            if not link.is_symlink() or not link.exists():
                errors.append(f"skills-parity: missing or broken link {link}")
                continue
            if link.resolve(strict=False) != Path(spath).resolve(strict=False):
                errors.append(
                    f"skills-parity: {link} -> {link.resolve(strict=False)} (expected {spath})"
                )
            if manifest_path.is_file() and sname not in recorded_links:
                errors.append(f"skills-parity: {sname} not tracked in {manifest_path}")

    # 10. Ignored tracked-configs symlinks still lingering under state/tracked-configs
    ignored_prefixes = _load_ignored_prefixes(host_mise_dir)
    tracked_dir = state_dir / "tracked-configs"
    if tracked_dir.is_dir() and ignored_prefixes:
        for link in sorted(tracked_dir.iterdir()):
            try:
                target = link.resolve(strict=True)
            except OSError:
                continue
            if _is_ignored_path(target, ignored_prefixes):
                errors.append(f"ignored-tracked-config: {link.name} -> {target}")

    return (
        errors,
        "host hygiene, shims, wrappers, plugins, daemons, vb, local bins, zsh completions, and skills parity verified",
    )


def check_jouet_rules(repo_root: Path) -> list[str]:
    """Run `jouet rule test` on committed repo rules and `--all`."""
    errors: list[str] = []
    rules_dir = repo_root / "unix" / "home" / ".config" / "jouet" / "rules"
    if rules_dir.is_dir():
        for rule_file in sorted(
            p
            for p in rules_dir.rglob("*")
            if p.suffix in {".toml", ".luau"} and p.is_file()
        ):
            try:
                proc = subprocess.run(
                    ["jouet", "rule", "test", str(rule_file)],
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
                errors.append(
                    f"jouet-rules: {rule_file.relative_to(repo_root)} failed: {exc}"
                )
                continue
            if proc.returncode != 0:
                errors.append(
                    f"jouet-rules: {rule_file.relative_to(repo_root)} failed: "
                    f"{proc.stderr.strip() or proc.stdout.strip()}"
                )
    try:
        all_proc = subprocess.run(
            ["jouet", "rule", "test", "--all"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
        errors.append(f"jouet-rules: `jouet rule test --all` failed: {exc}")
    else:
        if all_proc.returncode != 0:
            errors.append(
                f"jouet-rules: `jouet rule test --all` failed: "
                f"{all_proc.stderr.strip() or all_proc.stdout.strip()}"
            )
    return errors


def check_usage_lint(repo_root: Path) -> list[str]:
    """Run `usage lint -W -q` on every `#USAGE`-bearing task script."""
    errors: list[str] = []
    task_roots = (
        repo_root / "tasks",
        repo_root / "unix" / "home" / ".config" / "mise" / "tasks",
    )
    checked = 0
    for root in task_roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not re.search(r"^#USAGE ", text, re.MULTILINE):
                continue
            checked += 1
            try:
                proc = subprocess.run(
                    ["usage", "lint", "-W", "-q", str(path)],
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
                errors.append(f"usage-lint: {path.relative_to(repo_root)}: {exc}")
                continue
            if proc.returncode != 0 or proc.stderr.strip() or proc.stdout.strip():
                detail = (
                    proc.stderr.strip()
                    or proc.stdout.strip()
                    or f"rc={proc.returncode}"
                )
                errors.append(f"usage-lint: {path.relative_to(repo_root)}: {detail}")
    if checked == 0:
        errors.append("usage-lint: found zero #USAGE task files")
    return errors


def check_pitchfork_config(repo_root: Path) -> list[str]:
    """Validate committed pitchfork config cleanly via `pitchfork daemons --json`."""
    errors: list[str] = []
    configs: list[Path] = []
    for cand in (
        repo_root / "pitchfork.toml",
        repo_root / "unix" / "home" / ".config" / "pitchfork" / "config.toml",
    ):
        if cand.is_file():
            configs.append(cand)
    if not configs:
        return ["pitchfork-config: no pitchfork config file found"]

    for cfg in configs:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "pitchfork.toml").write_text(
                cfg.read_text(encoding="utf-8"), encoding="utf-8"
            )
            env = dict(os.environ)
            env["HOME"] = str(tmp_dir)
            env["XDG_CONFIG_HOME"] = str(tmp_dir / ".config")
            try:
                proc = subprocess.run(
                    ["pitchfork", "daemons", "--json"],
                    cwd=tmp_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
                errors.append(
                    f"pitchfork-config: {cfg.relative_to(repo_root)} failed: {exc}"
                )
                continue
            if proc.returncode != 0:
                errors.append(
                    f"pitchfork-config: {cfg.relative_to(repo_root)} failed: "
                    f"{proc.stderr.strip() or proc.stdout.strip()}"
                )
                continue
            try:
                parsed = json.loads(proc.stdout)
                if not isinstance(parsed, list) or not parsed:
                    errors.append(
                        f"pitchfork-config: {cfg.relative_to(repo_root)} parsed zero daemons"
                    )
            except json.JSONDecodeError as exc:
                errors.append(
                    f"pitchfork-config: {cfg.relative_to(repo_root)} invalid JSON: {exc}"
                )
    return errors


def check_dockerignore_secret_guard(repo_root: Path) -> list[str]:
    """Verify `.dockerignore` excludes local secrets and Python/macOS build artifacts after `!unix/home/.config/mise/**`."""
    errors: list[str] = []
    dockerignore = repo_root / ".dockerignore"
    if not dockerignore.is_file():
        return [".dockerignore is missing"]
    lines = [
        line.strip()
        for line in dockerignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    include_idx = -1
    for idx, line in enumerate(lines):
        if line == "!unix/home/.config/mise/**":
            include_idx = idx
    required_after = (
        "**/*.local.*",
        "**/__pycache__",
        "**/__pycache__/**",
        "**/*.pyc",
        "**/.DS_Store",
    )
    for pat in required_after:
        if pat not in lines:
            errors.append(
                f"dockerignore-secret-guard: missing `{pat}` in .dockerignore"
            )
        elif include_idx >= 0 and lines.index(pat) < include_idx:
            errors.append(
                f"dockerignore-secret-guard: `{pat}` must appear after `!unix/home/.config/mise/**` in .dockerignore"
            )
    dockerfile = repo_root / "docker" / "ubuntu" / "Dockerfile"
    if dockerfile.is_file():
        for lineno, line in enumerate(
            dockerfile.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if ".local." in line:
                errors.append(
                    f"dockerignore-secret-guard: {dockerfile.relative_to(repo_root)}:{lineno} references `.local.`"
                )
    return errors


_OPTIONAL_LOCKS_ENV = re.compile(
    r'^env\s*\{\s*\["GIT_OPTIONAL_LOCKS"\]\s*=\s*"0"\s*\}', re.MULTILINE
)


def check_git_index_locks(
    repo_root: Path, *, now: float | None = None, max_age_s: int = 120
) -> list[str]:
    """Keep hk read-only git calls lock-free and name stale `index.lock` files in any worktree.

    hk's fail_fast kills in-flight steps; a killed `git status` used to leave a 0-byte
    index.lock that blocked every later commit (fixed by `GIT_OPTIONAL_LOCKS=0`, jdx/hk#1911).
    A lock older than ``max_age_s`` that no process has open is reported with its path.
    """
    errors: list[str] = []
    hk_pkl = repo_root / "hk.pkl"
    if hk_pkl.is_file() and not _OPTIONAL_LOCKS_ENV.search(
        hk_pkl.read_text(encoding="utf-8")
    ):
        errors.append(
            'git-index-locks: hk.pkl must keep top-level `env { ["GIT_OPTIONAL_LOCKS"] = "0" }` '
            "(fail_fast kills a step's `git status` mid-refresh and strands index.lock)"
        )
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    out = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if out.returncode != 0:
        return errors
    common = Path(out.stdout.strip())
    locks = [
        common / "index.lock",
        *sorted((common / "worktrees").glob("*/index.lock")),
    ]
    now = time.time() if now is None else now
    lsof = shutil.which("lsof")
    for lock in locks:
        try:
            age = now - lock.stat().st_mtime
        except OSError:
            continue
        if age < max_age_s:
            continue
        if (
            lsof
            and subprocess.run(
                [lsof, "-t", str(lock)], capture_output=True, text=True, check=False
            ).stdout.strip()
        ):
            continue
        errors.append(
            f"git-index-locks: stale {lock} ({int(age)}s old, no process has it open); "
            f"confirm no git command is running in that worktree, then remove it: rm {lock}"
        )
    return errors


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _mise_action_with_version(lines: list[str], uses_idx: int) -> str | None:
    """Return `with.version` of the workflow step whose `uses:` is on `uses_idx`.

    Keys of a step mapping may come in any order, so this reads the whole step
    block and only accepts `version:` directly under that step's `with:`.
    """
    start = uses_idx
    while start >= 0 and not re.match(r"^\s*-\s", lines[start]):
        start -= 1
    if start < 0:
        return None
    item = re.match(r"^(\s*)-\s+", lines[start])
    assert item is not None
    item_indent, key_indent = len(item.group(1)), item.end()
    end = start + 1
    while end < len(lines) and (
        not lines[end].strip() or _indent(lines[end]) > item_indent
    ):
        end += 1
    block = lines[start:end]
    for i, row in enumerate(block):
        row_key = row[key_indent:] if i else row[item.end() :]
        if (i and _indent(row) != key_indent) or not re.match(
            r"with:\s*(?:#.*)?$", row_key
        ):
            continue
        child_indent = None
        for child in block[i + 1 :]:
            if not child.strip():
                continue
            if _indent(child) <= key_indent:
                break
            child_indent = child_indent or _indent(child)
            if _indent(child) != child_indent:
                continue
            m = re.match(r"^\s*version:\s*['\"]?([0-9]{4}\.[0-9]+\.[0-9]+)", child)
            if m:
                return m.group(1)
    return None


def check_mise_version_parity(repo_root: Path) -> list[str]:
    """Verify pinned mise versions match across Dockerfile, vm:up, pc:up, env:doctor, CI workflows, AGENTS.md, and configs."""
    errors: list[str] = []
    observed: dict[str, str] = {}

    dockerfile = repo_root / "docker" / "ubuntu" / "Dockerfile"
    if dockerfile.is_file():
        m = re.search(
            r"FROM\s+(?:ghcr\.io/jdx|jdxcode)/mise:([0-9]+\.[0-9]+\.[0-9]+)@",
            dockerfile.read_text(encoding="utf-8"),
        )
        if m:
            observed["docker/ubuntu/Dockerfile"] = m.group(1)
        else:
            errors.append(
                "mise-version-parity: missing pinned mise image in docker/ubuntu/Dockerfile"
            )

    vm_up = repo_root / "tasks" / "vm" / "up"
    if vm_up.is_file():
        m = re.search(
            r'^MISE_VER="([0-9]+\.[0-9]+\.[0-9]+)"',
            vm_up.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if m:
            observed["tasks/vm/up"] = m.group(1)
        else:
            errors.append("mise-version-parity: missing MISE_VER in tasks/vm/up")

    pc_up = repo_root / "tasks" / "pc" / "up"
    if pc_up.is_file():
        pc_text = pc_up.read_text(encoding="utf-8")
        m = re.search(r'^MISE_VER="([0-9]+\.[0-9]+\.[0-9]+)"', pc_text, re.MULTILINE)
        if m:
            observed["tasks/pc/up"] = m.group(1)
        else:
            errors.append("mise-version-parity: missing MISE_VER in tasks/pc/up")
        if vm_up.is_file():
            pin_re = re.compile(r'^PACKSLIP_MISE_PIN="(ps1_[a-z0-9]+)"', re.MULTILINE)
            vm_pin = pin_re.search(vm_up.read_text(encoding="utf-8"))
            pc_pin = pin_re.search(pc_text)
            if not vm_pin or not pc_pin:
                errors.append(
                    "mise-version-parity: missing PACKSLIP_MISE_PIN in tasks/vm/up or tasks/pc/up"
                )
            elif vm_pin.group(1) != pc_pin.group(1):
                errors.append(
                    "mise-version-parity: PACKSLIP_MISE_PIN skew between tasks/vm/up "
                    f"({vm_pin.group(1)}) and tasks/pc/up ({pc_pin.group(1)})"
                )

    workflows_dir = repo_root / ".github" / "workflows"
    if workflows_dir.is_dir():
        for wf in sorted(workflows_dir.glob("*.y*ml")):
            lines = wf.read_text(encoding="utf-8").splitlines()
            for idx, line in enumerate(lines):
                if "jdx/mise-action@" not in line:
                    continue
                found = _mise_action_with_version(lines, idx)
                rel = f"{wf.relative_to(repo_root)}:{idx + 1}"
                if found:
                    observed[rel] = found
                else:
                    errors.append(
                        f"mise-version-parity: {rel} uses jdx/mise-action without a pinned `version:`"
                    )

    agents_md = repo_root / "AGENTS.md"
    if agents_md.is_file():
        for ver in sorted(
            set(
                re.findall(
                    r"mise[ :]([0-9]{4}\.[0-9]+\.[0-9]+)",
                    agents_md.read_text(encoding="utf-8"),
                )
            )
        ):
            observed[f"AGENTS.md ({ver})"] = ver

    env_doctor = repo_root / "tasks" / "env" / "doctor"
    if env_doctor.is_file():
        ed_text = env_doctor.read_text(encoding="utf-8")
        ed_vers = re.findall(r'"([0-9]{4}\.[0-9]+\.[0-9]+)"', ed_text)
        if not ed_vers:
            errors.append(
                "mise-version-parity: missing mise version check in tasks/env/doctor"
            )
        elif len(set(ed_vers)) != 1:
            errors.append(
                f"mise-version-parity: inconsistent versions in tasks/env/doctor: {sorted(set(ed_vers))}"
            )
        else:
            observed["tasks/env/doctor"] = ed_vers[0]

    for rel_toml in ("mise.toml", "unix/home/.config/mise/config.toml"):
        p = repo_root / rel_toml
        if p.is_file():
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            mv = data.get("min_version")
            if isinstance(mv, str) and mv:
                observed[rel_toml] = mv
            else:
                errors.append(f"mise-version-parity: missing min_version in {rel_toml}")

    unique_vers = set(observed.values())
    if len(unique_vers) > 1:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(observed.items()))
        errors.append(f"mise-version-parity: version skew detected: {summary}")
    return errors


def _load_mise_registry_map() -> dict[str, list[tuple[str, list[str]]]]:
    out = subprocess.check_output(["mise", "registry"], text=True, timeout=15)
    by_backend: dict[str, list[tuple[str, list[str]]]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        short = parts[0]
        backends = parts[1:]
        for backend in backends:
            backend_norm = backend.split("[", 1)[0].lower()
            if backend_norm:
                by_backend.setdefault(backend_norm, []).append((short, backends))
    return by_backend


# Env vars that make Claude Code skip feature-flag fetching (code.claude.com/docs/en/env-vars#features-that-need-feature-flag-fetching),
# plus CLAUDE_CODE_DISABLE_SHIP. Set globally they silently remove /ship and /steward, so they may only be scoped to a single command.
_FLAG_FETCH_KILLERS = frozenset(
    {
        "CLAUDE_CODE_DISABLE_FEATURE_FLAGS",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "CLAUDE_CODE_DISABLE_SHIP",
        "DISABLE_GROWTHBOOK",
        "DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
    }
)


def _check_flag_fetch_env(repo_root: Path, mise_dir: Path) -> list[str]:
    """Reject global [env] / Dockerfile ENV entries that turn off Claude Code feature-flag fetching."""
    errors: list[str] = []
    msg = (
        "it disables Claude Code feature-flag fetching and removes /ship and /steward from every new session "
        "(scope it per command)"
    )
    # Global env files: user config*.toml, conf.d/<topic>/mise.toml fragments, and repo-root/doctor mise configs.
    env_files = {
        *mise_dir.glob("config*.toml"),
        *mise_dir.glob("conf.d/**/mise.toml"),
        *mise_dir.glob("conf.d/*.toml"),
        *repo_root.glob("mise.toml"),
        *repo_root.glob("mise.*.toml"),
        *repo_root.glob(".mise.toml"),
        *repo_root.glob(".mise.*.toml"),
        *repo_root.glob("mise/doctor/mise.toml"),
    }
    for env_file in sorted(f for f in env_files if f.is_file()):
        rel = env_file.relative_to(repo_root).as_posix()
        try:
            cfg = tomllib.loads(env_file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            errors.append(
                f"config-hygiene: {rel}: cannot parse for [env] flag-fetch check: {exc}"
            )
            continue
        env_tbl = cfg.get("env")
        settings = cfg.get("settings")
        py_settings = settings.get("python") if isinstance(settings, dict) else None
        uv_auto = (
            py_settings.get("uv_venv_auto", False)
            if isinstance(py_settings, dict)
            else False
        )
        if (
            isinstance(env_tbl, dict)
            and str(env_tbl.get("MISE_PYTHON_UV_VENV_AUTO", "false")).lower() != "false"
        ):
            uv_auto = env_tbl["MISE_PYTHON_UV_VENV_AUTO"]
        if uv_auto not in (False, "false"):
            errors.append(
                f"config-hygiene: {rel} sets python.uv_venv_auto = {uv_auto!r}; it puts the checkout's .venv/bin on "
                "PATH, so /ship and /steward refuse to watch PRs (use `uv run` or .venv/bin/python in tasks)"
            )
        if (
            isinstance(env_tbl, dict)
            and isinstance(env_tbl.get("_"), dict)
            and "python" in env_tbl["_"]
        ):
            errors.append(
                f"config-hygiene: {rel} [env] _.python.venv activates a venv on PATH; /ship and /steward refuse to "
                "watch PRs with a checkout dir on PATH (scope it to a task env instead)"
            )
        if isinstance(env_tbl, dict):
            for name in sorted(_FLAG_FETCH_KILLERS & set(env_tbl)):
                # mise `[env] NAME = false` unsets an inherited value, which is the safe direction.
                if env_tbl[name] is False:
                    continue
                errors.append(f"config-hygiene: {rel} [env] sets {name}; {msg}")
    # Only ENV instructions persist into containers (RUN-scoped `NAME=1 cmd` is fine); both `ENV K=V` and `ENV K V`.
    for dockerfile in sorted(repo_root.glob("docker/**/Dockerfile*")):
        try:
            text = dockerfile.read_text(encoding="utf-8")
        except OSError:
            continue
        logical = re.sub(
            r"\\\n",
            " ",
            "\n".join(
                ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
            ),
        )
        env_lines = [
            ln.strip()[4:]
            for ln in logical.splitlines()
            if re.match(r"ENV\s", ln.strip(), re.IGNORECASE)
        ]
        keys: set[str] = set()
        for ln in env_lines:
            try:
                args = shlex.split(ln)
            except ValueError:
                args = ln.split()
            if not args:
                continue
            # `ENV K=V K2="a b"` sets only the keys; legacy `ENV K V` sets the first word.
            keys |= (
                {a.split("=", 1)[0] for a in args if "=" in a}
                if "=" in args[0]
                else {args[0]}
            )
        for name in sorted(keys & _FLAG_FETCH_KILLERS):
            errors.append(
                f"config-hygiene: {dockerfile.relative_to(repo_root).as_posix()} sets {name}; {msg}"
            )
    # Managed shell startup files export into every session; a bare `NAME=1 cmd` prefix stays command-scoped.
    killers = "|".join(sorted(_FLAG_FETCH_KILLERS))
    sh_re = re.compile(rf"^\s*(?:export\s+({killers})=|({killers})=\S*\s*(?:#.*)?$)")
    home = repo_root / "unix" / "home"
    shell_files = {
        *home.glob(".zshenv"),
        *home.glob(".config/zsh/**/*"),
        *mise_dir.glob("assets/**/*.sh"),
    }
    for sh_file in sorted(f for f in shell_files if f.is_file()):
        try:
            lines = sh_file.read_text(encoding="utf-8").splitlines()
        except OSError, UnicodeDecodeError:
            continue
        names = {
            a or b for ln in lines if (m := sh_re.match(ln)) for a, b in [m.groups()]
        }
        for name in sorted(names):
            errors.append(
                f"config-hygiene: {sh_file.relative_to(repo_root).as_posix()} exports {name}; {msg}"
            )
    return errors


def check_config_hygiene(repo_root: Path) -> list[str]:
    """Validate registry short names, hyphenated npm prerelease, wrapper flags, programs.json, and [dotfiles] paths."""
    errors: list[str] = []
    mise_dir = repo_root / "unix" / "home" / ".config" / "mise"
    main_cfg_path = mise_dir / "config.toml"
    if not main_cfg_path.is_file():
        return ["config-hygiene: missing unix/home/.config/mise/config.toml"]

    errors.extend(_check_flag_fetch_env(repo_root, mise_dir))

    try:
        raw_reg_map = _load_mise_registry_map()
    except (OSError, subprocess.SubprocessError) as exc:
        return [*errors, f"config-hygiene: failed to load `mise registry`: {exc}"]

    reg_map, short_to_backends = _normalize_registry_entries(raw_reg_map)

    # 1, 2, 3, 4 & 6. Registry canonical short names, hyphenated npm prerelease, wrapper checks, and dotfiles paths across config*.toml.
    # Short-name rule: a backend-qualified key is an error only when `mise registry` already maps it to a
    # short name (so upstream can switch backends). Tools absent from the registry keep an explicit backend.
    for cfg_file in sorted(mise_dir.glob("config*.toml")):
        cfg_data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
        rel = cfg_file.relative_to(repo_root).as_posix()
        tools_tbl = cfg_data.get("tools")
        if isinstance(tools_tbl, dict):
            for tool_key, spec in tools_tbl.items():
                key_str = str(tool_key)
                base_key = key_str.split("[", 1)[0]
                if ":" in base_key:
                    lookup_key = base_key.lower()
                    reg_entry = reg_map.get(lookup_key)
                    if reg_entry is not None:
                        short, _backends = reg_entry
                        errors.append(
                            f"config-hygiene: {rel}: use canonical mise registry short name `{short}` instead of `{key_str}`"
                        )
                    is_npm_tool = lookup_key.startswith("npm:")
                else:
                    backends = short_to_backends.get(base_key.lower(), [])
                    is_npm_tool = bool(
                        backends and backends[0].lower().startswith("npm:")
                    )

                if is_npm_tool:
                    ver = _tool_version(spec) or ""
                    if "-" in ver and not (
                        isinstance(spec, dict) and spec.get("prerelease") is True
                    ):
                        errors.append(
                            f"config-hygiene: {rel}: hyphenated npm tool `{key_str}` ({ver}) must set `prerelease = true`"
                        )

        # 4 & 6. No raw ../ or ../../ [dotfiles] traversals + bidirectional source parity
        dotfiles = cfg_data.get("dotfiles")
        if isinstance(dotfiles, dict):
            for target_key, dspec in dotfiles.items():
                if not isinstance(dspec, dict):
                    continue
                src = dspec.get("source")
                if not isinstance(src, str) or not src:
                    continue
                parts = Path(src).parts
                if ".." in parts:
                    if (
                        dspec.get("merge") is True
                        and src in ALLOWED_DOTFILES_MERGE_RELATIVE
                    ):
                        continue
                    errors.append(
                        f"config-hygiene: {rel} [dotfiles].{target_key!r} has raw parent traversal `..` in source={src!r}"
                    )

    errors.extend(_check_dotfiles_parity(repo_root, mise_dir))

    # 5. programs.json uses declarative binary flags
    programs_path = (
        repo_root / "unix" / "home" / ".config" / "herdr-app" / "programs.json"
    )
    if not programs_path.is_file():
        errors.append(
            "config-hygiene: missing unix/home/.config/herdr-app/programs.json"
        )
    else:
        pdata = json.loads(programs_path.read_text(encoding="utf-8"))
        programs = pdata.get("programs") if isinstance(pdata, dict) else None
        if not isinstance(programs, dict):
            errors.append("config-hygiene: programs.json missing `programs` object")
        else:
            for pname, expected_argv in EXPECTED_PROGRAMS.items():
                entry = programs.get(pname)
                run_argv = entry.get("run") if isinstance(entry, dict) else None
                if not isinstance(run_argv, list) or not run_argv:
                    errors.append(
                        f"config-hygiene: programs.json `{pname}` missing `run` argv"
                    )
                elif [str(x) for x in run_argv] != expected_argv:
                    errors.append(
                        f"config-hygiene: programs.json `{pname}` must invoke {expected_argv!r} (got {run_argv!r})"
                    )
    return errors


def _strip_merge_target(target_key: str) -> str:
    """Resolve `~/.path/to/file.json/subkey` -> `.path/to/file.json`."""
    rel = target_key[2:] if target_key.startswith("~/") else target_key
    parts = Path(rel).parts
    for idx, part in enumerate(parts):
        if "." in part and idx < len(parts) - 1 and idx > 0:
            return Path(*parts[: idx + 1]).as_posix()
    return rel


def _is_group_excluded(rel_in_group: str, excludes: list[str]) -> bool:
    name = Path(rel_in_group).name
    for pat in excludes:
        if fnmatch.fnmatch(rel_in_group, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


def _check_dotfiles_parity(repo_root: Path, mise_dir: Path) -> list[str]:
    """Verify [dotfiles] and [dotfile_groups] stay bidirectionally aligned with unix/home/."""
    home_root = repo_root / "unix" / "home"
    if not home_root.is_dir():
        return []
    errors: list[str] = []
    declared_targets: set[str] = set()
    referenced_tilde_sources: list[tuple[str, str]] = []
    groups: list[tuple[str, str, list[str], dict[str, Any]]] = []
    has_real_dotfiles_tree = (home_root / ".zshenv").is_file()

    for cfg_file in sorted(mise_dir.glob("config*.toml")):
        cfg_data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
        rel_cfg = cfg_file.relative_to(repo_root).as_posix()
        dotfiles = cfg_data.get("dotfiles")
        if isinstance(dotfiles, dict):
            for target_key, dspec in dotfiles.items():
                if not isinstance(target_key, str):
                    continue
                if not isinstance(dspec, dict) or "block" in dspec or "line" in dspec:
                    declared_targets.add(target_key)
                    continue
                if dspec.get("mode") == "absent":
                    # `mode = "absent"` removes a target and cannot satisfy a source dependency.
                    continue
                declared_targets.add(target_key)
                src = dspec.get("source")
                if isinstance(src, str) and src:
                    if src.startswith("~/"):
                        referenced_tilde_sources.append((rel_cfg, src))
                    elif (
                        has_real_dotfiles_tree
                        and ".." not in Path(src).parts
                        and not (mise_dir / src).exists()
                    ):
                        errors.append(
                            f"config-hygiene: {rel_cfg} [dotfiles].{target_key!r} missing source {src!r}"
                        )
                elif dspec.get("merge") is True:
                    if has_real_dotfiles_tree:
                        base_rel = _strip_merge_target(target_key)
                        if not (home_root / base_rel).exists():
                            errors.append(
                                f"config-hygiene: {rel_cfg} [dotfiles].{target_key!r} missing base file unix/home/{base_rel}"
                            )
                elif (
                    has_real_dotfiles_tree
                    and target_key.startswith("~/")
                    and not (home_root / target_key[2:]).exists()
                ):
                    errors.append(
                        f"config-hygiene: {rel_cfg} [dotfiles].{target_key!r} missing unix/home/{target_key[2:]}"
                    )

        dot_groups = cfg_data.get("dotfile_groups")
        if isinstance(dot_groups, dict):
            for gname, gdata in dot_groups.items():
                if not isinstance(gdata, dict):
                    continue
                g_root = str(gdata.get("root") or ".")
                g_target = str(gdata.get("target") or "~")
                g_excl = [str(x) for x in gdata.get("exclude") or []]
                g_entries = gdata.get("entries")
                entries_dict = g_entries if isinstance(g_entries, dict) else {}
                base_dir = home_root if g_root == "." else home_root / g_root
                if has_real_dotfiles_tree and not base_dir.exists():
                    errors.append(
                        f"config-hygiene: {rel_cfg} [dotfile_groups.{gname}] missing root {g_root!r}"
                    )
                for e_key, e_spec in entries_dict.items():
                    declared_targets.add(str(e_key))
                    if isinstance(e_spec, dict) and isinstance(
                        e_src := e_spec.get("source"), str
                    ):
                        if e_src.startswith("~/"):
                            referenced_tilde_sources.append((rel_cfg, e_src))
                        elif has_real_dotfiles_tree and not (base_dir / e_src).exists():
                            errors.append(
                                f"config-hygiene: {rel_cfg} [dotfile_groups.{gname}.entries].{e_key!r} missing source {e_src!r}"
                            )
                groups.append((g_root, g_target, g_excl, entries_dict))

    if not has_real_dotfiles_tree:
        return errors

    for rel_cfg, tsrc in referenced_tilde_sources:
        if tsrc not in declared_targets and not _is_covered_by_groups(tsrc, groups):
            errors.append(
                f"config-hygiene: {rel_cfg} references undeclared tilde source {tsrc!r}"
            )

    skip_prefixes = (".config/mise/",)
    skip_names = {".DS_Store", "ruff.toml"}
    for path in sorted(home_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(home_root).as_posix()
        if any(rel.startswith(p) for p in skip_prefixes):
            continue
        if (
            path.name in skip_names
            or "__pycache__" in path.parts
            or path.suffix == ".pyc"
        ):
            continue
        if rel.startswith(".agents/skills/"):
            skill_parts = Path(rel).parts
            if len(skill_parts) >= 3:
                target_skill = f"~/.agents/skills/{skill_parts[2]}"
                if target_skill not in declared_targets and not _is_covered_by_groups(
                    target_skill, groups
                ):
                    errors.append(
                        f"config-hygiene: unregistered skill directory unix/home/.agents/skills/{skill_parts[2]}"
                    )
            continue
        target_tilde = f"~/{rel}"
        if target_tilde in declared_targets or _is_covered_by_groups(
            target_tilde, groups
        ):
            continue
        # Merge-only targets like hooks.json are declared as `~/...json/<key>`
        if any(dt.startswith(f"{target_tilde}/") for dt in declared_targets):
            continue
        errors.append(
            f"config-hygiene: unregistered dotfile unix/home/{rel} (missing in config*.toml [dotfiles]/[dotfile_groups])"
        )
    return errors


def _is_covered_by_groups(
    target_tilde: str,
    groups: list[tuple[str, str, list[str], dict[str, Any]]],
) -> bool:
    for g_root, g_target, g_excl, entries_dict in groups:
        if target_tilde == g_target or target_tilde in entries_dict:
            return True
        prefix = f"{g_target.rstrip('/')}/"
        if target_tilde.startswith(prefix):
            rel_in_group = target_tilde[len(prefix) :]
            if "*" not in g_excl and not _is_group_excluded(rel_in_group, g_excl):
                return True
        elif g_root == "." and g_target == "~":
            rel_in_group = (
                target_tilde[2:] if target_tilde.startswith("~/") else target_tilde
            )
            if "*" not in g_excl and not _is_group_excluded(rel_in_group, g_excl):
                return True
    return False


def evaluate_mise_audit_payload(data: Any) -> tuple[list[str], list[str]]:
    """Return `(fixable_errors, unpatched_advisories)` from `mise audit --json` payload."""
    if not isinstance(data, dict):
        return [f"expected JSON object, got {type(data).__name__}"], []
    fixable: list[str] = []
    advisories: list[str] = []
    for vuln in data.get("vulnerabilities") or []:
        if not isinstance(vuln, dict):
            continue
        tool = vuln.get("tool") or "unknown"
        ver = vuln.get("version") or ""
        vid = vuln.get("id") or "unknown"
        summary = vuln.get("summary") or ""
        fixed = vuln.get("fixed") or []
        if fixed:
            fixed_str = ", ".join(str(x) for x in fixed)
            fixable.append(f"{tool}@{ver} {vid} fixed in [{fixed_str}]: {summary}")
        else:
            advisories.append(f"{tool}@{ver} {vid} (unpatched): {summary}")
    return fixable, advisories


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
    except (OSError, subprocess.SubprocessError):  # fmt: skip
        return 0
    home_changes = [line for line in changed if line.startswith("unix/home/")]
    if home_changes:
        sys.stdout.write(
            f"post-commit advisory: {len(home_changes)} unix/home/ file(s) changed; "
            "run `mise run host:sync-gemini` or `mise dot apply` if syncing live host.\n"
        )
    return 0


def check_herdr_app_bundle() -> tuple[list[str], str]:
    """Verify macOS Herdr.app is in /Applications/Herdr.app with no duplicate ~/Applications/Herdr.app."""
    home = Path.home()
    if (
        _live_host_skipped()
        or sys.platform != "darwin"
        or (home / ".config" / "dotfiles-v2" / "vm-env.sh").is_file()
    ):
        return [], "Non-macOS/CI/VM: skipped herdr-app-bundle"
    errors: list[str] = []
    app_sys = Path("/Applications/Herdr.app")
    app_usr = home / "Applications" / "Herdr.app"
    if not app_sys.is_dir():
        errors.append("herdr-app-bundle: missing /Applications/Herdr.app")
    if app_usr.exists() or app_usr.is_symlink():
        errors.append(
            f"herdr-app-bundle: duplicate {app_usr} shadows /Applications/Herdr.app"
        )
    return errors, "Herdr.app bundle verified exclusively at /Applications/Herdr.app"


def check_codex_hook_trust(
    repo_root: Path, *, strict_host: bool = False
) -> tuple[list[str], str]:
    """Verify Codex hook-trust task availability and live trust state."""
    trust_task = (
        repo_root
        / "unix"
        / "home"
        / ".config"
        / "mise"
        / "tasks"
        / "codex"
        / "hook-trust"
    )
    update_task = (
        repo_root
        / "unix"
        / "home"
        / ".config"
        / "mise"
        / "tasks"
        / "codex"
        / "plugin-update"
    )
    for p in (trust_task, update_task):
        if not p.is_file() or not os.access(p, os.X_OK):
            return [
                f"codex-hook-trust: missing executable task {p.relative_to(repo_root)}"
            ], ""
    home = Path.home()
    if (
        _live_host_skipped()
        or not (home / ".codex" / "config.toml").is_file()
        or not shutil.which("codex")
    ):
        return [], "CI/unconfigured: skipped live codex-hook-trust check"
    venv_py = repo_root / ".venv" / "bin" / "python"
    if not venv_py.is_file():
        venv_py = _main_checkout_root(repo_root) / ".venv" / "bin" / "python"
    if not venv_py.is_file():
        return [], "skipped live codex-hook-trust (missing .venv/bin/python)"
    lib_dir = repo_root / "unix" / "home" / ".config" / "mise" / "lib"
    try:
        proc = subprocess.run(
            [
                str(venv_py),
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); from codex_trust.cli import main; raise SystemExit(main(sys.argv[2:]))",
                str(lib_dir),
                "hook-trust",
                "--check",
                "--json",
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
        return [f"codex-hook-trust: failed executing hook-trust --check: {exc}"], ""
    if not proc.stdout.strip():
        return [
            f"codex-hook-trust: empty output (rc={proc.returncode}): {proc.stderr.strip()}"
        ], ""
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError as exc:
        return [f"codex-hook-trust: invalid JSON ({exc})"], ""
    findings = data.get("findings") or []
    blocking = [
        f for f in findings if isinstance(f, dict) and f.get("status") != "orphaned"
    ]
    orphaned = [
        f for f in findings if isinstance(f, dict) and f.get("status") == "orphaned"
    ]
    errors: list[str] = []
    for err in data.get("errors") or []:
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            if (
                code in {"ERR_ORPHANED_STATE", "ERR_LEGACY_BASELINE_DRIFT"}
                and not strict_host
            ):
                continue
            errors.append(f"codex-hook-trust error: {code}: {err.get('message')}")
    for b in blocking:
        errors.append(
            f"codex-hook-trust {b.get('status')}: {b.get('key')} ({b.get('issue')})"
        )
    if strict_host:
        for o in orphaned:
            errors.append(
                f"codex-hook-trust orphaned: {o.get('key')} (run `mise run codex:hook-trust --repair`)"
            )
    if errors:
        return errors, ""
    if orphaned:
        return [], (
            f"codex-hook-trust: 0 modified/unmigrated ({len(orphaned)} orphaned entries; "
            "run `mise run codex:hook-trust --repair` to prune)"
        )
    return [], "codex-hook-trust: all plugin hook trust hashes match live hooks/list"


def check_mise_binary_integrity(repo_root: Path) -> tuple[list[str], str]:
    """Verify host:harden-bins, dcg guardrails/hooks, and 0555 binary immutability inside mise installs/."""
    errors: list[str] = []
    harden_task = (
        repo_root
        / "unix"
        / "home"
        / ".config"
        / "mise"
        / "tasks"
        / "host"
        / "harden-bins"
    )
    if not harden_task.is_file() or not os.access(harden_task, os.X_OK):
        errors.append(
            f"mise-binary-integrity: missing executable {harden_task.relative_to(repo_root)}"
        )

    dcg_cfg = repo_root / "unix" / "home" / ".config" / "dcg" / "config.toml"
    if not dcg_cfg.is_file():
        errors.append(
            "mise-binary-integrity: missing unix/home/.config/dcg/config.toml"
        )
    else:
        try:
            dcg_data = tomllib.loads(dcg_cfg.read_text(encoding="utf-8"))
            custom_paths = (
                (dcg_data.get("packs") or {}).get("custom_paths")
                if isinstance(dcg_data.get("packs"), dict)
                else None
            )
            if not isinstance(custom_paths, list) or not any(
                "packs/*.yaml" in str(p) for p in custom_paths
            ):
                errors.append(
                    "mise-binary-integrity: dcg config.toml missing packs.custom_paths for ~/.config/dcg/packs/*.yaml"
                )
        except tomllib.TOMLDecodeError as exc:
            errors.append(f"mise-binary-integrity: invalid dcg config.toml: {exc}")

    dcg_pack = (
        repo_root / "unix" / "home" / ".config" / "dcg" / "packs" / "mise-protect.yaml"
    )
    if not dcg_pack.is_file():
        errors.append(
            "mise-binary-integrity: missing unix/home/.config/dcg/packs/mise-protect.yaml"
        )
    else:
        pack_txt = dcg_pack.read_text(encoding="utf-8")
        if "custom.mise_protect" not in pack_txt:
            errors.append(
                "mise-binary-integrity: mise-protect.yaml missing id custom.mise_protect"
            )
        if shutil.which("dcg"):
            try:
                vp = subprocess.run(
                    ["dcg", "pack", "validate", str(dcg_pack)],
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
                if vp.returncode != 0:
                    errors.append(
                        f"mise-binary-integrity: dcg pack validate failed: {vp.stderr.strip() or vp.stdout.strip()}"
                    )
            except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
                errors.append(f"mise-binary-integrity: dcg pack validate error: {exc}")

    agy_hook = (
        repo_root
        / "unix"
        / "home"
        / ".gemini"
        / "config"
        / "hooks"
        / "agy-dcg-guard.py"
    )
    if not agy_hook.is_file() or not os.access(agy_hook, os.X_OK):
        errors.append(
            f"mise-binary-integrity: missing executable {agy_hook.relative_to(repo_root)}"
        )

    hooks_json = repo_root / "unix" / "home" / ".gemini" / "config" / "hooks.json"
    if not hooks_json.is_file() or "agy-dcg-guard.py" not in hooks_json.read_text(
        encoding="utf-8"
    ):
        errors.append(
            "mise-binary-integrity: hooks.json missing dcg-guard entry referencing agy-dcg-guard.py"
        )

    for rel_asset in (
        "unix/home/.config/mise/assets/claude/dcg-hooks.json",
        "unix/home/.config/mise/assets/codex/dcg-hooks.json",
    ):
        ap = repo_root / rel_asset
        if not ap.is_file():
            errors.append(f"mise-binary-integrity: missing {rel_asset}")
        else:
            try:
                adata = json.loads(ap.read_text(encoding="utf-8"))
                if not isinstance((adata.get("hooks") or {}).get("PreToolUse"), list):
                    errors.append(
                        f"mise-binary-integrity: {rel_asset} missing PreToolUse hook list"
                    )
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(
                    f"mise-binary-integrity: invalid JSON in {rel_asset}: {exc}"
                )

    if not _live_host_skipped():
        home = Path.home()
        data_dir = Path(
            os.environ.get("MISE_DATA_DIR", str(home / ".local" / "share" / "mise"))
        )
        installs_dir = data_dir / "installs"
        if installs_dir.is_dir():
            writable_bins: list[str] = []
            for cur_dir, dirnames, filenames in os.walk(
                installs_dir, followlinks=False
            ):
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d not in BINARY_HARDEN_SKIP_DIRS
                    and not (sys.platform == "darwin" and d.endswith(".app"))
                    and not os.path.islink(os.path.join(cur_dir, d))
                ]
                for fname in filenames:
                    fpath = os.path.join(cur_dir, fname)
                    try:
                        st = os.lstat(fpath)
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode) or (st.st_mode & 0o111) == 0:
                        continue
                    if (st.st_mode & 0o222) == 0:
                        continue
                    try:
                        with open(fpath, "rb") as fh:
                            hdr = fh.read(4)
                    except OSError:
                        continue
                    if hdr in COMPILED_BINARY_MAGICS:
                        writable_bins.append(fpath)
            if writable_bins:
                preview = ", ".join(writable_bins[:5])
                extra = (
                    f" (+{len(writable_bins) - 5} more)"
                    if len(writable_bins) > 5
                    else ""
                )
                errors.append(
                    f"mise-binary-integrity: {len(writable_bins)} compiled binary(ies) writable in {installs_dir}: {preview}{extra} (run `mise run host:harden-bins`)"
                )

    return (
        errors,
        "mise-binary-integrity: harden-bins, dcg guardrails/hooks, and 0555 binary immutability verified",
    )


def check_agy_rc_health(
    repo_root: Path, *, strict_host: bool = False
) -> tuple[list[str], str]:
    """Verify Antigravity CLI Remote Control guard task and LaunchAgent health when configured."""
    guard = (
        repo_root
        / "unix"
        / "home"
        / ".config"
        / "mise"
        / "tasks"
        / "host"
        / "agy-rc-guard"
    )
    if not guard.is_file() or not os.access(guard, os.X_OK):
        return [f"agy-rc-health: missing executable {guard.relative_to(repo_root)}"], ""
    home = Path.home()
    if (
        _live_host_skipped()
        or sys.platform != "darwin"
        or (home / ".config" / "dotfiles-v2" / "vm-env.sh").is_file()
    ):
        return [], "Non-macOS/CI/VM: skipped live agy-rc-health"
    plist = (
        home / "Library" / "LaunchAgents" / "com.google.antigravity-cli-daemon.plist"
    )
    try:
        proc = subprocess.run(
            [
                "mise",
                "-C",
                str(repo_root),
                "run",
                "-q",
                "host:agy-rc-guard",
                "--check",
                "--json",
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
        return [f"agy-rc-health: failed executing agy-rc-guard: {exc}"], ""
    if not proc.stdout.strip():
        return [
            f"agy-rc-health: empty output (rc={proc.returncode}): {proc.stderr.strip()}"
        ], ""
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError as exc:
        return [f"agy-rc-health: invalid JSON ({exc})"], ""
    if not plist.is_file() and not strict_host and data.get("rc_running") is not True:
        return (
            [],
            "pre-activation: com.google.antigravity-cli-daemon LaunchAgent not yet installed (tasks/host/agy-rc-guard ready)",
        )
    if proc.returncode != 0:
        issues = data.get("issues") or []
        return [f"agy-rc-health: {item}" for item in issues] or [
            f"agy-rc-health: exited {proc.returncode}"
        ], ""
    return [], "agy-rc-health: Antigravity Remote Control daemon and token healthy"


def check_skills_6harness_parity(repo_root: Path) -> tuple[list[str], str]:
    """Verify canonical ~/.agents/skills coverage across Codex, OpenCode, AGY, Claude, ZCode, and Hermes."""
    errors: list[str] = []
    repo_skills_dir = repo_root / "unix" / "home" / ".agents" / "skills"
    required_skills = (
        "agy-session-ops",
        "herdr-roll",
        "mise-bootstrap-ops",
        "post-task-learn",
        "pwf-status",
        "vercel-board",
    )
    for skill in required_skills:
        if not (repo_skills_dir / skill / "SKILL.md").is_file():
            errors.append(
                f"skills-6harness-parity: missing canonical unix/home/.agents/skills/{skill}/SKILL.md"
            )
    bootstrap_skills = (
        repo_root
        / "unix"
        / "home"
        / ".config"
        / "mise"
        / "tasks"
        / "bootstrap"
        / "skills"
    )
    hermes_sync = (
        repo_root
        / "unix"
        / "home"
        / ".config"
        / "mise"
        / "tasks"
        / "hermes"
        / "skills-sync"
    )
    for p in (bootstrap_skills, hermes_sync):
        if not p.is_file() or not os.access(p, os.X_OK):
            errors.append(
                f"skills-6harness-parity: missing executable {p.relative_to(repo_root)}"
            )
    if _live_host_skipped():
        return errors, _skip_reason("repo-level 6-harness skill structure verified")

    home = Path.home()
    is_cutover, _ = _is_dotfiles_v2_mise_cutover(home, repo_root=repo_root)
    if not is_cutover:
        return errors, "PRE-CUTOVER: repo-level 6-harness skill structure verified"

    agents_skills = home / ".agents" / "skills"
    gemini_skills = home / ".gemini" / "config" / "skills"
    claude_skills = home / ".claude" / "skills"
    if gemini_skills.exists() or gemini_skills.is_symlink():
        errors.append(
            f"skills-6harness-parity: legacy {gemini_skills} still present; agy reads {agents_skills} natively (run `mise run host:sync-gemini`)"
        )
    if shutil.which("agentsview"):
        av_skill = agents_skills / "agentsview-finding-history"
        if not (av_skill / "SKILL.md").is_file() or av_skill.is_symlink():
            errors.append(
                "skills-6harness-parity: ~/.agents/skills/agentsview-finding-history must be a real agentsview-managed dir (run `mise run bootstrap:skills`)"
            )
        if not (claude_skills / "agentsview-finding-history").is_symlink():
            errors.append(
                "skills-6harness-parity: ~/.claude/skills/agentsview-finding-history must symlink to ../../.agents/skills/agentsview-finding-history"
            )
    for skill in required_skills:
        host_req = agents_skills / skill
        if not (host_req / "SKILL.md").is_file():
            errors.append(
                f"skills-6harness-parity: missing canonical host skill ~/.agents/skills/{skill}/SKILL.md (run `mise run bootstrap:skills`)"
            )
    if agents_skills.is_dir():
        for skill_entry in sorted(agents_skills.iterdir()):
            if not (skill_entry / "SKILL.md").is_file():
                continue
            claude_target = claude_skills / skill_entry.name
            if claude_target.is_symlink():
                if claude_target.resolve(strict=False) != skill_entry.resolve(
                    strict=False
                ):
                    errors.append(
                        f"skills-6harness-parity: ~/.claude/skills/{skill_entry.name} symlink mismatch against {skill_entry}"
                    )
            elif not (
                claude_target.is_dir() and (claude_target / "SKILL.md").is_file()
            ):
                errors.append(
                    f"skills-6harness-parity: ~/.claude/skills/{skill_entry.name} missing/invalid against {skill_entry}"
                )
    zcode_skills = home / ".zcode" / "skills"
    if zcode_skills.is_symlink():
        if zcode_skills.resolve(strict=False) == agents_skills.resolve(strict=False):
            errors.append(
                f"skills-6harness-parity: stale ~/.zcode/skills root symlink -> {agents_skills}; zcode reads ~/.agents/skills natively"
            )
    elif zcode_skills.is_dir():
        for stale in sorted(zcode_skills.iterdir()):
            if (
                stale.is_symlink()
                and os.readlink(stale) == f"../../.agents/skills/{stale.name}"
            ):
                errors.append(
                    f"skills-6harness-parity: stale ~/.zcode/skills/{stale.name} mirror; zcode reads ~/.agents/skills natively"
                )
    hermes_cfg = home / ".hermes" / "config.yaml"
    if hermes_cfg.is_file():
        htext = hermes_cfg.read_text(encoding="utf-8", errors="replace")
        if "~/.agents/skills" not in htext and str(agents_skills) not in htext:
            errors.append(
                "skills-6harness-parity: ~/.hermes/config.yaml missing ~/.agents/skills under external_dirs"
            )
    return (
        errors,
        "6-harness skills parity verified across Codex, OpenCode, AGY, Claude, ZCode, and Hermes",
    )


def _load_reviewer_plugins_lib(repo_root: Path):
    import importlib.util

    path = repo_root / "unix/home/.config/mise/lib/reviewer_plugins.py"
    spec = importlib.util.spec_from_file_location("reviewer_plugins", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SHIP_SKILLS = ("ship-local", "steward")


def _claude_settings_ship_errors(label: str, data: object) -> list[str]:
    """Claude settings that remove /ship or /steward: flag-fetch killers in `env`, skill overrides, bundled skills off."""
    if not isinstance(data, dict):
        return []
    errors: list[str] = []
    env = data.get("env")
    if isinstance(env, dict):
        for name in sorted(_FLAG_FETCH_KILLERS & set(env)):
            errors.append(
                f"claude-ship-env: {label} env sets {name}; it turns off feature-flag fetching and removes /ship and /steward"
            )
    overrides = data.get("skillOverrides")
    if isinstance(overrides, dict):
        for skill in _SHIP_SKILLS:
            if str(overrides.get(skill, "")).lower() == "off":
                errors.append(
                    f"claude-ship-env: {label} skillOverrides turns off {skill}"
                )
    if data.get("disableBundledSkills") is True:
        errors.append(
            f"claude-ship-env: {label} disableBundledSkills removes /ship and /steward"
        )
    return errors


def _claude_settings_mcp_errors(label: str, data: object) -> list[str]:
    """Owner rule: claude.ai connectors stay off unless a concrete need is shown."""
    if not isinstance(data, dict):
        return []
    errors: list[str] = []
    if data.get("disableClaudeAiConnectors") is False:
        errors.append(
            f"claude-ship-env: {label} disableClaudeAiConnectors=false turns claude.ai connectors on"
        )
    env = data.get("env")
    if isinstance(env, dict) and str(
        env.get("ENABLE_CLAUDEAI_MCP_SERVERS", "")
    ).lower() in {"1", "true", "yes", "on"}:
        errors.append(
            f"claude-ship-env: {label} env ENABLE_CLAUDEAI_MCP_SERVERS turns claude.ai connectors on"
        )
    return errors


def check_claude_ship_env(repo_root: Path) -> tuple[list[str], str]:
    """Managed Claude settings (repo) and the live host keep /ship, /steward and feature-flag fetching, and keep claude.ai connectors off."""
    errors: list[str] = []
    killer_key = re.compile(r'"(' + "|".join(sorted(_FLAG_FETCH_KILLERS)) + r')"\s*:')
    managed = sorted(
        {
            *(repo_root / "unix/home/.config/mise/assets/claude").glob("**/*.json*"),
            *(repo_root / "unix/home/.claude").glob("**/*.json"),
        }
    )
    connectors_off = False
    for path in managed:
        rel = path.relative_to(repo_root).as_posix()
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            try:
                data = json.loads(text)
                errors += _claude_settings_ship_errors(rel, data)
                errors += _claude_settings_mcp_errors(rel, data)
                connectors_off |= (
                    isinstance(data, dict)
                    and data.get("disableClaudeAiConnectors") is True
                )
            except ValueError as exc:
                errors.append(f"claude-ship-env: {rel}: invalid JSON: {exc}")
        else:  # templates (.json.tera) are not JSON until rendered: match the same three settings textually
            if m := killer_key.search(text):
                errors.append(
                    f"claude-ship-env: {rel} sets {m.group(1)}; it removes /ship and /steward"
                )
            for skill in _SHIP_SKILLS:
                if re.search(rf'"{skill}"\s*:\s*"off"', text, re.I):
                    errors.append(
                        f"claude-ship-env: {rel} skillOverrides turns off {skill}"
                    )
            if re.search(r'"disableBundledSkills"\s*:\s*true', text):
                errors.append(
                    f"claude-ship-env: {rel} disableBundledSkills removes /ship and /steward"
                )
            if re.search(r'"disableClaudeAiConnectors"\s*:\s*false', text):
                errors.append(f"claude-ship-env: {rel} turns claude.ai connectors on")
    if managed and not connectors_off:
        errors.append(
            "claude-ship-env: no managed Claude settings file sets disableClaudeAiConnectors=true; claude.ai connectors stay off unless needed"
        )
    if errors or _live_host_skipped():
        return errors, (
            _skip_reason(
                f"{len(managed)} managed Claude settings files clean; live settings not probed"
            )
            if not errors
            else ""
        )
    settings = Path.home() / ".claude/settings.json"
    if settings.is_file():  # no settings file means nothing disables the ship skills
        try:
            live = json.loads(settings.read_text(encoding="utf-8"))
            errors += _claude_settings_ship_errors("~/.claude/settings.json", live)
            errors += _claude_settings_mcp_errors("~/.claude/settings.json", live)
            if (
                isinstance(live, dict)
                and live.get("disableClaudeAiConnectors") is not True
            ):
                errors.append(
                    "claude-ship-env: ~/.claude/settings.json does not set disableClaudeAiConnectors=true; run `mise dot apply`"
                )
        except (OSError, ValueError) as exc:
            errors.append(
                f"claude-ship-env: cannot read ~/.claude/settings.json: {exc}"
            )
    for name in sorted(_FLAG_FETCH_KILLERS & set(os.environ)):
        errors.append(
            f"claude-ship-env: this shell exports {name}; new Claude sessions started from it lose /ship and /steward"
        )
    venv = os.environ.get("VIRTUAL_ENV", "")
    if venv and Path(venv).resolve().is_relative_to(repo_root.resolve()):
        errors.append(
            "claude-ship-env: a checkout venv is active (VIRTUAL_ENV); /ship and /steward refuse to watch"
        )
    return (
        errors,
        "managed and live Claude settings keep /ship and /steward available and claude.ai connectors off",
    )


def check_reviewer_plugins(repo_root: Path) -> tuple[list[str], str]:
    """Declared reviewer-bot plugins are well-formed (repo) and installed/Connected (live Mac host)."""
    errors: list[str] = []
    mise_dir = repo_root / "unix/home/.config/mise"
    for rel in (
        "tasks/bootstrap/reviewer-plugins",
        "tasks/repowise/guard",
        "tasks/repowise/init",
    ):
        task = mise_dir / rel
        if not task.is_file() or not os.access(task, os.X_OK):
            errors.append(f"reviewer-plugins: missing executable {rel}")
    try:
        lib = _load_reviewer_plugins_lib(repo_root)
        manifest = lib.load_manifest(mise_dir / "assets/reviewer-plugins.json")
    except (OSError, ImportError, ValueError) as exc:
        return [*errors, f"reviewer-plugins: cannot load manifest/lib: {exc}"], ""

    pid_re = re.compile(r"[a-z0-9-]+@[a-z0-9-]+")
    for harness in ("claude", "codex"):
        spec = manifest.get(harness, {})
        mps = spec.get("marketplaces", {})
        for pid in spec.get("plugins", []):
            if not pid_re.fullmatch(str(pid)):
                errors.append(
                    f"reviewer-plugins: {harness} plugin id {pid!r} is not name@marketplace"
                )
                continue
            mkt = str(pid).split("@", 1)[1]
            if mkt not in mps and mkt != "claude-plugins-official":
                errors.append(
                    f"reviewer-plugins: {harness} plugin {pid} has no declared marketplace {mkt}"
                )
    claude = manifest.get("claude", {})
    for mcp in claude.get("connected_mcps", []):
        plugin = str(mcp).split(":")[1] if str(mcp).startswith("plugin:") else ""
        if not any(str(p).split("@")[0] == plugin for p in claude.get("plugins", [])):
            errors.append(
                f"reviewer-plugins: connected MCP {mcp} has no matching Claude plugin"
            )
    oc_path = repo_root / "unix/home/.config/opencode/opencode.json"
    try:
        oc = json.loads(oc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        oc = {}
        errors.append(f"reviewer-plugins: cannot read opencode.json: {exc}")
    oc_spec = manifest.get("opencode", {})
    for plug in oc_spec.get("plugins", []):
        if plug not in (oc.get("plugins") or []):
            errors.append(f"reviewer-plugins: opencode.json plugins missing {plug}")
    servers = (
        ((oc.get("mcp") or {}).get("servers") or {})
        if isinstance(oc.get("mcp"), dict)
        else {}
    )
    for name in oc_spec.get("mcp_servers", []):
        if name not in servers:
            errors.append(f"reviewer-plugins: opencode.json mcp.servers missing {name}")

    if errors or _live_host_skipped():
        return errors, (
            _skip_reason(
                "reviewer-plugins manifest well-formed; live install/status not probed"
            )
            if not errors
            else ""
        )
    home = Path.home()
    # Installs + OAuth sign-ins are per host and need a browser; only the Mac host is required.
    if sys.platform != "darwin" or (home / ".config/dotfiles-v2/vm-env.sh").is_file():
        return [], "not the Mac host; live plugin status not required"
    live = [f"reviewer-plugins: {e}" for e in lib.check(manifest, home)]
    if live:
        live.append(
            "reviewer-plugins: fix with `mise run bootstrap:reviewer-plugins`; sign in via Claude /mcp -> <server> -> Authenticate"
        )
        return live, ""
    return (
        [],
        "declared reviewer plugins installed across harnesses and Claude plugin MCPs Connected",
    )


def _report(label: str, errors: list[str], ok_msg: str) -> int:
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        return 1
    sys.stdout.write(f"{label}: {ok_msg}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fast deterministic dotfiles-v2 gates")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("no-project-tools")
    sub.add_parser("mise-lock-integrity")
    p_host = sub.add_parser("host-option-a")
    p_host.add_argument("--strict-host", action="store_true")
    sub.add_parser("mise-doctor-clean")
    sub.add_parser("host-hygiene")
    sub.add_parser("reviewer-plugins")
    sub.add_parser("claude-ship-env")
    sub.add_parser("jouet-rules")
    sub.add_parser("usage-lint")
    sub.add_parser("pitchfork-config")
    sub.add_parser("dockerignore-secret-guard")
    sub.add_parser("git-index-locks")
    sub.add_parser("mise-version-parity")
    sub.add_parser("config-hygiene")
    p_audit = sub.add_parser("mise-audit-fixable")
    p_audit.add_argument("--json-file", type=Path, required=True)
    p_audit.add_argument("--rc", type=int, default=0)
    p_audit.add_argument("--err-file", type=Path, default=None)
    sub.add_parser("herdr-app-bundle")
    p_trust = sub.add_parser("codex-hook-trust")
    p_trust.add_argument("--strict-host", action="store_true")
    sub.add_parser("mise-binary-integrity")
    p_rc = sub.add_parser("agy-rc-health")
    p_rc.add_argument("--strict-host", action="store_true")
    sub.add_parser("skills-6harness-parity")
    sub.add_parser("post-commit-advisory")

    args = parser.parse_args(argv)
    repo_root = Path.cwd().resolve()

    if args.cmd == "no-project-tools":
        return _report(
            "no-project-tools",
            check_no_project_tools(repo_root),
            "root/project mise TOMLs and pyproject.toml clean",
        )
    if args.cmd == "mise-lock-integrity":
        return _report(
            "mise-lock-integrity",
            check_mise_lock_integrity(repo_root),
            "mise.lock and locks/ match config.toml across all platforms",
        )
    if args.cmd == "host-option-a":
        errs, msg = check_host_option_a(repo_root, strict_host=args.strict_host)
        return _report("host-option-a", errs, msg)
    if args.cmd == "mise-doctor-clean":
        errs, msg = check_mise_doctor_clean(repo_root)
        return _report("mise-doctor-clean", errs, msg)
    if args.cmd == "host-hygiene":
        errs, msg = check_host_hygiene(repo_root)
        return _report("host-hygiene", errs, msg)
    if args.cmd == "jouet-rules":
        return _report(
            "jouet-rules", check_jouet_rules(repo_root), "all jouet rules passed"
        )
    if args.cmd == "usage-lint":
        return _report(
            "usage-lint", check_usage_lint(repo_root), "all #USAGE tasks valid"
        )
    if args.cmd == "pitchfork-config":
        return _report(
            "pitchfork-config",
            check_pitchfork_config(repo_root),
            "pitchfork config valid",
        )
    if args.cmd == "git-index-locks":
        return _report(
            "git-index-locks",
            check_git_index_locks(repo_root),
            "hk read-only git calls skip optional locks; no stale index.lock",
        )
    if args.cmd == "dockerignore-secret-guard":
        return _report(
            "dockerignore-secret-guard",
            check_dockerignore_secret_guard(repo_root),
            ".dockerignore excludes local secrets and build residue",
        )
    if args.cmd == "mise-version-parity":
        return _report(
            "mise-version-parity",
            check_mise_version_parity(repo_root),
            "mise version parity verified",
        )
    if args.cmd == "config-hygiene":
        return _report(
            "config-hygiene",
            check_config_hygiene(repo_root),
            "configs and programs.json clean",
        )
    if args.cmd == "mise-audit-fixable":
        err_text = (
            args.err_file.read_text(encoding="utf-8").strip()
            if args.err_file is not None and args.err_file.is_file()
            else ""
        )
        try:
            data = json.loads(args.json_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:  # fmt: skip
            sys.stderr.write(f"FAIL  mise-audit: invalid JSON ({exc})\n")
            if err_text:
                sys.stderr.write(f"{err_text}\n")
            return 1
        fixable, advisories = evaluate_mise_audit_payload(data)
        for adv in advisories:
            sys.stdout.write(f"ADVISORY  mise-audit: {adv}\n")
        if fixable:
            for item in fixable:
                sys.stderr.write(f"FAIL  mise-audit: fixable {item}\n")
            if err_text:
                sys.stderr.write(f"{err_text}\n")
            return 1
        if args.rc != 0 and not (
            isinstance(data, dict) and data.get("vulnerabilities")
        ):
            sys.stderr.write(f"FAIL  mise-audit: exited {args.rc}\n")
            if err_text:
                sys.stderr.write(f"{err_text}\n")
            return 1
        sys.stdout.write("PASS  mise-audit (0 fixable advisories)\n")
        return 0
    if args.cmd == "herdr-app-bundle":
        errs, msg = check_herdr_app_bundle()
        return _report("herdr-app-bundle", errs, msg)
    if args.cmd == "codex-hook-trust":
        errs, msg = check_codex_hook_trust(repo_root, strict_host=args.strict_host)
        return _report("codex-hook-trust", errs, msg)
    if args.cmd == "mise-binary-integrity":
        errs, msg = check_mise_binary_integrity(repo_root)
        return _report("mise-binary-integrity", errs, msg)
    if args.cmd == "agy-rc-health":
        errs, msg = check_agy_rc_health(repo_root, strict_host=args.strict_host)
        return _report("agy-rc-health", errs, msg)
    if args.cmd == "reviewer-plugins":
        errs, msg = check_reviewer_plugins(repo_root)
        return _report("reviewer-plugins", errs, msg)
    if args.cmd == "claude-ship-env":
        errs, msg = check_claude_ship_env(repo_root)
        return _report("claude-ship-env", errs, msg)
    if args.cmd == "skills-6harness-parity":
        errs, msg = check_skills_6harness_parity(repo_root)
        return _report("skills-6harness-parity", errs, msg)
    if args.cmd == "post-commit-advisory":
        return post_commit_advisory(repo_root)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
