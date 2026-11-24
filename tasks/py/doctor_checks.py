#!/usr/bin/env python3
"""Fast deterministic project & Option A host checks for `mise doctor project` and `hk.pkl`."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
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

DOCUMENTED_BACKEND_OPTOUTS = frozenset({"aqua:oxc-project/oxc/oxfmt"})
ALLOWED_DOTFILES_MERGE_RELATIVE = frozenset(
    {"../../.claude.mcp-jdx.json", "../../.codex/mcp-jdx.toml"}
)
EXPECTED_PROGRAMS_HC = {
    "antigravity": "agy-hc",
    "codex": "codex-hc",
    "opencode": "opencode-hc",
}


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

    resolved_root = repo_root.resolve(strict=False)
    mise_cfg_root = resolved_root / "unix" / "home" / ".config" / "mise"
    referenced_aube_dirs: set[Path] = set()
    npm_lock_unresolved = False
    for tool_name, spec in tools_cfg.items():
        want_ver = _tool_version(spec)
        entries = tools_lock.get(tool_name)
        if not isinstance(entries, list) or not entries:
            errors.append(f"mise.lock missing entry for [tools].{tool_name!r}")
            if tool_name.startswith("npm:"):
                npm_lock_unresolved = True
            continue
        entry_dicts = [e for e in entries if isinstance(e, dict)]
        if not entry_dicts:
            errors.append(f"mise.lock invalid entries for [tools].{tool_name!r}")
            if tool_name.startswith("npm:"):
                npm_lock_unresolved = True
            continue
        if want_ver:
            matching = [e for e in entry_dicts if str(e.get("version", "")) == want_ver]
            if not matching:
                got_vers = ", ".join(str(e.get("version", "")) for e in entry_dicts)
                errors.append(
                    f"mise.lock version mismatch for {tool_name}: config={want_ver}, lock=[{got_vers}]"
                )
                if tool_name.startswith("npm:"):
                    npm_lock_unresolved = True
                continue
        else:
            matching = entry_dicts
        if tool_name.startswith("npm:"):
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
    if os.environ.get("CI"):
        return [], "CI: skipped live host Option A checks"

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

    # no-legacy-symlinks + orphan check (bounded depth across managed trees)
    scan_roots = (
        home / ".config" / "mise",
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
        home / ".gemini" / "GEMINI.md",
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


def check_mise_doctor_clean() -> tuple[list[str], str]:
    """Verify `mise -C ~ doctor --json` reports 0 errors and 0 warnings."""
    if os.environ.get("CI"):
        return [], "CI: skipped live host mise-doctor-clean"
    home = Path.home()
    env = dict(os.environ)
    if "PATH" in env:
        env["PATH"] = _strip_git_core_from_path(
            env["PATH"], env.get("GIT_EXEC_PATH", "")
        )
    try:
        proc = subprocess.run(
            ["mise", "-C", str(home), "doctor", "--json"],
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
    if os.environ.get("CI"):
        return [], "CI: skipped live host-hygiene"

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

    # 7. Unmanaged ~/.config/opencode/opencode.jsonc
    oc_jsonc = home / ".config" / "opencode" / "opencode.jsonc"
    if oc_jsonc.exists() or oc_jsonc.is_symlink():
        errors.append(f"unmanaged-opencode-jsonc: {oc_jsonc}")

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
        "host hygiene, shims, wrappers, plugins, daemons, vb, local bins, and skills parity verified",
    )


def check_cli_wrappers_smoke() -> tuple[list[str], str]:
    """Smoke-test active CLI wrappers for opencode, codex, and agy without proxy warnings."""
    home = Path.home()
    if (
        os.environ.get("CI")
        or os.environ.get("container") == "docker"  # noqa: SIM112
        or (home / ".config" / "dotfiles-v2" / "vm-env.sh").is_file()
    ):
        return [], "CI/container/VM: skipped live cli-wrappers-smoke"
    env = dict(os.environ)
    env.pop("__MISE_ACTIVE_WRAPPERS", None)
    wrappers_bin = (
        Path(env.get("MISE_DATA_DIR", str(home / ".local" / "share" / "mise")))
        / "command-wrappers"
        / "bin"
    )
    if wrappers_bin.is_dir():
        env["PATH"] = f"{wrappers_bin}:{env.get('PATH', os.defpath)}"

    smoke_cmds: list[list[str]] = [
        ["opencode", "update", "--help"],
        ["opencode", "upgrade", "--help"],
        ["opencode", "mcp", "--help"],
        ["opencode", "service", "--help"],
        ["opencode", "run", "--help"],
        ["codex", "exec", "--help"],
        ["agy", "--help"],
    ]
    errors: list[str] = []
    for cmd in smoke_cmds:
        if not shutil.which(cmd[0], path=env.get("PATH")):
            errors.append(f"cli-wrappers-smoke: `{cmd[0]}` not found on PATH")
            continue
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"cli-wrappers-smoke: `{' '.join(cmd)}` failed: {exc}")
            continue
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode != 0:
            errors.append(
                f"cli-wrappers-smoke: `{' '.join(cmd)}` exited {proc.returncode}: {proc.stderr.strip()}"
            )
        if "Unrecognized flag" in combined:
            errors.append(
                f"cli-wrappers-smoke: `{' '.join(cmd)}` rejected flag: {combined.strip()}"
            )
        if "WARN fnox::commands::proxy" in (proc.stderr or ""):
            errors.append(
                f"cli-wrappers-smoke: `{' '.join(cmd)}` leaked `WARN fnox::commands::proxy` on stderr"
            )
    return errors, "CLI wrappers smoke-tested cleanly across opencode, codex, and agy"


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


def check_mise_version_parity(repo_root: Path) -> list[str]:
    """Verify pinned MISE_VER and min_version match across Dockerfile, vm:up, env:doctor, configs, and docs."""
    errors: list[str] = []
    observed: dict[str, str] = {}

    dockerfile = repo_root / "docker" / "ubuntu" / "Dockerfile"
    if dockerfile.is_file():
        m = re.search(
            r"FROM\s+ghcr\.io/jdx/mise:([0-9]+\.[0-9]+\.[0-9]+)@",
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

    agents_md = repo_root / "AGENTS.md"
    if agents_md.is_file():
        atext = agents_md.read_text(encoding="utf-8")
        avers = re.findall(
            r"(?:ghcr\.io/jdx/mise:|copies `mise |installs `mise )([0-9]{4}\.[0-9]+\.[0-9]+)",
            atext,
        )
        if not avers:
            errors.append("mise-version-parity: missing mise version in AGENTS.md")
        elif len(set(avers)) != 1:
            errors.append(
                f"mise-version-parity: inconsistent mise versions in AGENTS.md: {sorted(set(avers))}"
            )
        else:
            observed["AGENTS.md"] = avers[0]

    skill_md = (
        repo_root
        / "unix"
        / "home"
        / ".agents"
        / "skills"
        / "mise-bootstrap-ops"
        / "SKILL.md"
    )
    if skill_md.is_file():
        stext = skill_md.read_text(encoding="utf-8")
        m = re.search(r"ghcr\.io/jdx/mise`\s*\(`([0-9]{4}\.[0-9]+\.[0-9]+)`\)", stext)
        if m:
            observed["unix/home/.agents/skills/mise-bootstrap-ops/SKILL.md"] = m.group(
                1
            )
        else:
            errors.append(
                "mise-version-parity: missing mise version in mise-bootstrap-ops/SKILL.md"
            )

    unique_vers = set(observed.values())
    if len(unique_vers) > 1:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(observed.items()))
        errors.append(f"mise-version-parity: version skew detected: {summary}")
    return errors


def _load_mise_registry_map() -> dict[str, tuple[str, list[str]]]:
    out = subprocess.check_output(["mise", "registry"], text=True, timeout=15)
    by_backend: dict[str, tuple[str, list[str]]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        short = parts[0]
        backends = parts[1:]
        for backend in backends:
            by_backend.setdefault(backend, (short, backends))
    return by_backend


def check_config_hygiene(repo_root: Path) -> list[str]:
    """Validate registry short names, hyphenated npm prerelease, wrapper flags, programs.json, and [dotfiles] paths."""
    errors: list[str] = []
    mise_dir = repo_root / "unix" / "home" / ".config" / "mise"
    main_cfg_path = mise_dir / "config.toml"
    if not main_cfg_path.is_file():
        return ["config-hygiene: missing unix/home/.config/mise/config.toml"]

    main_text = main_cfg_path.read_text(encoding="utf-8")
    main_lines = main_text.splitlines()
    main_cfg = tomllib.loads(main_text)

    try:
        reg_map = _load_mise_registry_map()
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"config-hygiene: failed to load `mise registry`: {exc}"]

    # 1. Registry canonical short names (allowing documented explicit backend opt-outs)
    for tool_key in main_cfg.get("tools", {}):
        if ":" not in tool_key or tool_key not in reg_map:
            continue
        short, backends = reg_map[tool_key]
        if backends[0] == tool_key:
            errors.append(
                f"config-hygiene: use canonical registry short name `{short}` instead of `{tool_key}`"
            )
            continue
        # Non-primary backend: only allow documented explicit opt-outs (e.g. aqua:oxc-project/oxc/oxfmt)
        has_comment = False
        for idx, raw_line in enumerate(main_lines):
            if f'"{tool_key}"' in raw_line or f"'{tool_key}'" in raw_line:
                prev_line = main_lines[idx - 1].strip() if idx > 0 else ""
                if "#" in raw_line or prev_line.startswith("#"):
                    has_comment = True
                break
        if tool_key not in DOCUMENTED_BACKEND_OPTOUTS or not has_comment:
            errors.append(
                f"config-hygiene: `{tool_key}` overrides canonical short name `{short}` without documented opt-out"
            )

    # 2. Hyphenated npm: tools require prerelease = true
    for tool_key, spec in main_cfg.get("tools", {}).items():
        if not str(tool_key).startswith("npm:"):
            continue
        ver = _tool_version(spec) or ""
        if "-" in ver and not (
            isinstance(spec, dict) and spec.get("prerelease") is True
        ):
            errors.append(
                f"config-hygiene: hyphenated npm tool `{tool_key}` ({ver}) must set `prerelease = true`"
            )

    # 3 & 4. Wrapper checks across config*.toml
    for cfg_file in sorted(mise_dir.glob("config*.toml")):
        cfg_data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
        rel = cfg_file.relative_to(repo_root).as_posix()
        wrappers = cfg_data.get("wrappers")
        if isinstance(wrappers, dict):
            oc = wrappers.get("opencode")
            if isinstance(oc, dict):
                oc_args = [str(a) for a in oc.get("args") or []]
                if "--standalone" in oc_args:
                    errors.append(
                        f"config-hygiene: {rel} [wrappers.opencode].args must not hardcode `--standalone`"
                    )
            for wname, wspec in wrappers.items():
                if not isinstance(wspec, dict):
                    continue
                cmd = str(wspec.get("command") or "")
                args = [str(a) for a in wspec.get("args") or []]
                uses_fnox_proxy = (
                    cmd == "fnox" and "proxy" in args and "run" in args
                ) or "fnox proxy run" in cmd
                if uses_fnox_proxy:
                    env_tbl = wspec.get("env")
                    rust_log = (
                        str(env_tbl.get("RUST_LOG") or "")
                        if isinstance(env_tbl, dict)
                        else ""
                    )
                    if "fnox::commands::proxy=error" not in rust_log:
                        errors.append(
                            f"config-hygiene: {rel} [wrappers.{wname}] invokes `fnox proxy run` without `fnox::commands::proxy=error` in RUST_LOG"
                        )

        # 6. No raw ../ or ../../ [dotfiles] traversals
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

    # 5. programs.json uses *-hc
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
            for pname, expected_bin in EXPECTED_PROGRAMS_HC.items():
                entry = programs.get(pname)
                run_argv = entry.get("run") if isinstance(entry, dict) else None
                if not isinstance(run_argv, list) or not run_argv:
                    errors.append(
                        f"config-hygiene: programs.json `{pname}` missing `run` argv"
                    )
                elif str(run_argv[0]) != expected_bin:
                    errors.append(
                        f"config-hygiene: programs.json `{pname}` must invoke `{expected_bin}` (got {run_argv[0]!r})"
                    )
    return errors


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
    sub.add_parser("cli-wrappers-smoke")
    sub.add_parser("jouet-rules")
    sub.add_parser("usage-lint")
    sub.add_parser("pitchfork-config")
    sub.add_parser("dockerignore-secret-guard")
    sub.add_parser("mise-version-parity")
    sub.add_parser("config-hygiene")
    p_audit = sub.add_parser("mise-audit-fixable")
    p_audit.add_argument("--json-file", type=Path, required=True)
    p_audit.add_argument("--rc", type=int, default=0)
    p_audit.add_argument("--err-file", type=Path, default=None)
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
        errs, msg = check_mise_doctor_clean()
        return _report("mise-doctor-clean", errs, msg)
    if args.cmd == "host-hygiene":
        errs, msg = check_host_hygiene(repo_root)
        return _report("host-hygiene", errs, msg)
    if args.cmd == "cli-wrappers-smoke":
        errs, msg = check_cli_wrappers_smoke()
        return _report("cli-wrappers-smoke", errs, msg)
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
    if args.cmd == "post-commit-advisory":
        return post_commit_advisory(repo_root)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
