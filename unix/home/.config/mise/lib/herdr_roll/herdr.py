"""Subprocess, Herdr (`hc` / `herdr-app`), process tree, and clean relaunch helpers."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from herdr_roll.models import HarnessKind

RAW_STRIP = frozenset({"HC_TERMINAL_ID", "HC_SCOPE"})
LAUNCHERS = frozenset({"fnox", "env"})
SHELLS = frozenset({"zsh", "bash", "sh", "fish"})

# Long-lived services a harness runs under itself; excluded when detecting active jobs.
SERVICE_RE = re.compile(
    r"\b(mcp|lsp|language-server|clangd|pyright|gopls|rust-analyzer|typescript-language-server|jdtls|sourcekit|"
    r"caffeinate|codebase-memory|openwiki|context7|exa|firecrawl|playwright|uv tool run|uv tool uvx|npx -y|npm exec|"
    r"bun x|modelcontextprotocol|node_repl|cua_node|codex-code-mode-host|hc (watch|tail)|fnox (daemon|mcp))\b|"
    r"\bnode\s+(--[\w-]+=\S*\s+|--(enable-source-maps|no-warnings|inspect|trace-warnings)\s+)*"
    r"(\S*/)?[\w.-]*server(\.[cm]?js)?(\s|$)|(^|/)(ty|ruff) server(\s|$)",
    re.IGNORECASE,
)

# macOS `ps` renders exiting subshells/statusline commands as `(bash)`, `(awk)`, `(git)`, etc.
TRANSIENT_WRAPPED_NAMES = frozenset(
    {
        "(bash)",
        "(sh)",
        "(zsh)",
        "(fish)",
        "(dash)",
        "(awk)",
        "(git)",
        "(sed)",
        "(tr)",
        "(cut)",
        "(jq)",
        "(grep)",
        "(rg)",
        "(python)",
        "(python3)",
        "(node)",
    }
)

ALLOWLISTED_ENV_KEYS = (
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "TERM",
    "COLORTERM",
    "TERM_PROGRAM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "HC_TERMINAL_ID",
    "HC_TERMINAL_SOCKET",
    "HC_SOCKET",
    "HC_SESSION",
    "DISPLAY",
    "SSH_AUTH_SOCK",
)

PS_LINE_RE = re.compile(
    r"\s*(\d+)\s+(\d+)\s+(\w{3}\s+\w{3}\s+\d+\s+[\d:]+\s+\d{4})\s+(.*)"
)


def raw_env() -> dict[str, str]:
    """Return environment without HC_TERMINAL_ID / HC_SCOPE so `hc send` types untagged input."""
    return {k: v for k, v in os.environ.items() if k not in RAW_STRIP}


def run_cmd(
    argv: Iterable[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 15.0,
    ok_codes: Iterable[int] = (0,),
) -> str | None:
    """Run *argv* non-interactively and return stdout on an allowed exit code, else None."""
    allowed = set(ok_codes)
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=dict(env) if env is not None else None,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return completed.stdout if completed.returncode in allowed else None


def run_json(
    argv: Iterable[str], *, env: Mapping[str, str] | None = None, timeout: float = 15.0
) -> dict[str, Any]:
    """Run *argv* and decode a JSON object stdout, returning {} on error."""
    out = run_cmd(argv, env=env, timeout=timeout)
    if not out:
        return {}
    try:
        decoded = json.loads(out)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def hc_ls() -> list[dict[str, Any]]:
    """Return live Herdr terminal rows from `hc ls --json`."""
    data = run_json(["hc", "ls", "--json"])
    terminals = data.get("terminals")
    if not isinstance(terminals, list):
        return []
    return [
        row for row in terminals if isinstance(row, dict) and row.get("state") == "live"
    ]


def hc_get(address: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Return `hc get <address> --json` object or {}."""
    return run_json(["hc", "get", address, "--json"], timeout=timeout)


def hc_read(address: str, *, lines: int = 0, timeout: float = 15.0) -> str:
    """Return visible screen or screen+history from `hc read`."""
    return (
        run_cmd(["hc", "read", address, "--lines", str(lines)], timeout=timeout) or ""
    )


def hc_send(
    address: str,
    text: str,
    *,
    raw: bool = False,
    wait: bool = False,
    timeout: float = 15.0,
) -> bool:
    """Send *text* via `hc send`, optionally stripping HC_TERMINAL_ID and/or waiting."""
    cmd = ["hc", "send"]
    if wait:
        cmd.extend(["--wait", "--timeout", str(max(1, int(timeout)))])
    cmd.extend(["--", address, text])
    wait_timeout = (timeout + 15.0) if wait else timeout
    return (
        run_cmd(cmd, env=raw_env() if raw else None, timeout=wait_timeout) is not None
    )


def hc_keys(address: str, *keys: str, timeout: float = 15.0) -> bool:
    """Send key presses via `hc keys`."""
    return run_cmd(["hc", "keys", address, *keys], timeout=timeout) is not None


def hc_wait(
    address: str, *, output: str | None = None, timeout: float = 45.0
) -> dict[str, Any]:
    """Wait on *address* via `hc wait --json` and return parsed JSON result."""
    cmd = ["hc", "wait", "--json", "--timeout", str(max(1, int(timeout)))]
    if output is not None:
        cmd.extend(["--output", output])
    cmd.append(address)
    return run_json(cmd, timeout=timeout + 15.0)


def herdr_app_ls(*, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Return Herdr spaces list from `herdr-app ls`."""
    data = run_json(["herdr-app", "ls"], timeout=timeout)
    spaces = data.get("spaces")
    if not isinstance(spaces, list):
        return []
    return [row for row in spaces if isinstance(row, dict)]


def herdr_app_do(
    action: str, *args: str, timeout: float = 15.0
) -> dict[str, Any] | None:
    """Invoke `herdr-app do <action> ...`; return parsed response dict on success, else None."""
    out = run_cmd(["herdr-app", "do", action, *args], timeout=timeout)
    if out is None:
        return None
    if not out.strip():
        return {}
    try:
        decoded = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict) or "error" in decoded:
        return None
    return decoded


def processes() -> dict[int, tuple[int, str, str]]:
    """Return mapping `pid -> (ppid, lstart, args)` using `LC_ALL=C`."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    text = (
        run_cmd(["ps", "-axo", "pid=,ppid=,lstart=,args="], env=env, timeout=15.0) or ""
    )
    found: dict[int, tuple[int, str, str]] = {}
    for line in text.splitlines():
        match = PS_LINE_RE.match(line)
        if match:
            found[int(match[1])] = (int(match[2]), match[3], match[4])
    return found


def lsof_cwd(pid: int) -> str | None:
    """Return process working directory via `lsof`, or None if unavailable."""
    out = (
        run_cmd(["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"], timeout=10.0) or ""
    )
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def git_status_porcelain(folder: str | None) -> str | None:
    """Return normalized `git status --porcelain` without taking optional index locks, or None."""
    if not folder or not Path(folder).is_dir():
        return None
    if not run_cmd(
        ["git", "--no-optional-locks", "-C", folder, "rev-parse", "--git-dir"],
        timeout=10.0,
    ):
        return None
    out = run_cmd(
        ["git", "--no-optional-locks", "-C", folder, "status", "--porcelain"],
        timeout=15.0,
    )
    if out is None:
        return None
    return "\n".join(sorted(line.rstrip() for line in out.splitlines() if line.strip()))


def adaptive_poll[T](
    probe: Callable[[], T | None],
    deadline_s: float,
    *,
    min_interval: float = 0.25,
    max_interval: float = 1.0,
) -> T | None:
    """Poll *probe* with smooth backoff from *min_interval* up to *max_interval*."""
    end = time.monotonic() + max(0.0, deadline_s)
    delay = max(0.05, min_interval)
    while True:
        found = probe()
        if found is not None or time.monotonic() >= end:
            return found
        time.sleep(min(delay, max(0.0, end - time.monotonic())))
        delay = min(max_interval, delay * 1.6)


ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def unwrap_launchers(argv: Iterable[str]) -> list[str]:
    """Strip outer Herdr shell wrappers, `fnox`, and `env` prefixes from *argv*."""
    words = list(argv)
    while words:
        cmd_name = Path(words[0]).name.lstrip("-")
        if (
            cmd_name in SHELLS
            and len(words) >= 4
            and words[1].startswith("-")
            and "c" in words[1]
            and ('"$@"' in words[2] or "exec" in words[2])
        ):
            words = words[4:]
            continue
        if cmd_name == "fnox":
            idx = 1
            while idx < len(words) and words[idx] != "--":
                tok = words[idx]
                if tok in {"--config", "-c", "--profile"}:
                    idx += 2
                else:
                    idx += 1
            if idx < len(words) and words[idx] == "--":
                words = words[idx + 1 :]
                continue
            break
        if cmd_name == "env":
            rest = words[1:]
            while rest and (rest[0].startswith("-") or "=" in rest[0]):
                if rest[0] in {"-u", "-C", "-S"} and len(rest) >= 2:
                    rest = rest[2:]
                else:
                    rest = rest[1:]
            words = rest
            continue
        break
    return words


def program_name(argv: Iterable[str]) -> str | None:
    """Return basename of the launched harness command after unwrapping wrappers."""
    words = unwrap_launchers(argv)
    return Path(words[0]).name if words else None


def is_bare_shell(argv: Iterable[str]) -> bool:
    """True when *argv* runs an interactive login/bare shell."""
    words = list(argv)
    return bool(words) and Path(words[0]).name.lstrip("-") in SHELLS


def is_transient_or_shell(args: str) -> bool:
    """True when *args* represents a bare shell, zombie, or exiting macOS `(proc)` ephemeral."""
    stripped = args.strip()
    if not stripped or stripped == "<defunct>":
        return True
    first = stripped.split()[0]
    if first in TRANSIENT_WRAPPED_NAMES or (
        first.startswith("(") and first.endswith(")")
    ):
        return True
    if first.startswith("-") and first.lstrip("-") in SHELLS:
        return True
    return Path(first).name.lstrip("-") in SHELLS and len(stripped.split()) == 1


def owned_by_harness(
    kind: HarnessKind,
    args: str,
    harness_binaries: Mapping[HarnessKind, tuple[str, ...]],
) -> bool:
    """True if *args* launches the harness executable itself (after unwrapping `fnox`/`env`/shell)."""
    names = harness_binaries.get(kind, ())
    if not names or not args.strip():
        return False
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    words = unwrap_launchers(tokens)
    if not words:
        return False
    first = Path(words[0]).name
    if any(first == name or first.startswith(f"{name}-") for name in names):
        return True
    # Script harnesses (e.g. Hermes/ZCode) may run under `python ... bin/hermes` or `node ... bin/zcode`.
    if (first.startswith("python") or first == "node") and len(words) > 1:
        for arg in words[1:]:
            if arg.startswith("-"):
                continue
            target = Path(arg).name
            return any(
                target == name or target.startswith(f"{name}-") for name in names
            )
    return False


def allowlisted_env_dict(
    *,
    terminal_address: str,
    extra_env: Mapping[str, str] | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build sanitized, allowlisted environment dictionary with *extra_env* precedence."""
    source = dict(os.environ if base_env is None else base_env)
    resolved: dict[str, str] = {}
    for key in ALLOWLISTED_ENV_KEYS:
        if key == "HC_TERMINAL_ID":
            resolved["HC_TERMINAL_ID"] = terminal_address
        elif key == "TERM":
            term_val = source.get("TERM") or ""
            resolved["TERM"] = (
                term_val if term_val and term_val != "dumb" else "xterm-256color"
            )
        elif key == "COLORTERM":
            resolved["COLORTERM"] = source.get("COLORTERM") or "truecolor"
        elif key == "TERM_PROGRAM":
            resolved["TERM_PROGRAM"] = source.get("TERM_PROGRAM") or "ghostty"
        elif val := source.get(key):
            resolved[key] = val

    resolved["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    if extra_env:
        for key, val in extra_env.items():
            if ENV_KEY_RE.fullmatch(key):
                resolved[key] = val
    return resolved


def clean_relaunch_command(
    cwd: str,
    argv: list[str],
    *,
    terminal_address: str,
    extra_env: Mapping[str, str] | None = None,
    base_env: Mapping[str, str] | None = None,
) -> str:
    """Build an allowlisted `env -i ... /bin/zsh -lc 'exec "$@"' /bin/zsh ...` shell line."""
    resolved = allowlisted_env_dict(
        terminal_address=terminal_address,
        extra_env=extra_env,
        base_env=base_env,
    )
    env_pairs = [f"{k}={shlex.quote(v)}" for k, v in resolved.items()]
    quoted_cmd = " ".join(shlex.quote(token) for token in argv)
    return f"cd {shlex.quote(cwd)} && env -i {' '.join(env_pairs)} /bin/zsh -lc 'exec \"$@\"' /bin/zsh {quoted_cmd}"


class LiveClient:
    """Thread-safe parallel Herdr and process-table reader."""

    def __init__(self, *, max_workers: int = 8) -> None:
        self.max_workers = max_workers
        self._lock = threading.RLock()
        self.terminals: dict[str, dict[str, Any]] = {}
        self.info: dict[str, dict[str, Any]] = {}
        self.spaces: list[dict[str, Any]] = []
        self.procs: dict[int, tuple[int, str, str]] = {}
        self.listed = False
        self.me = os.environ.get("HC_TERMINAL_ID")
        self.refresh()

    def refresh(self) -> None:
        """Refresh `herdr-app ls`, `hc ls --json`, parallel `hc get --json`, and then `ps`."""
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_spaces = pool.submit(herdr_app_ls)
            f_ls = pool.submit(run_json, ["hc", "ls", "--json"])
            spaces = f_spaces.result()
            ls_payload = f_ls.result()

        raw_terminals = ls_payload.get("terminals")
        is_listed = isinstance(raw_terminals, list)
        listed = (
            [
                r
                for r in raw_terminals
                if isinstance(r, dict) and r.get("state") == "live"
            ]
            if isinstance(raw_terminals, list)
            else []
        )
        terminals = {str(row["address"]): row for row in listed if row.get("address")}

        if terminals:
            with ThreadPoolExecutor(
                max_workers=min(self.max_workers, len(terminals))
            ) as pool:
                results = list(
                    pool.map(lambda addr: (addr, hc_get(addr)), sorted(terminals))
                )
            info = {addr: row for addr, row in results}
        else:
            info = {}

        # Read `ps` after `hc get` so newly spawned foreground PIDs are present in `procs`.
        procs = processes()
        with self._lock:
            self.spaces = spaces
            self.listed = is_listed
            self.terminals = terminals
            self.info = info
            self.procs = procs

    def refresh_terminal(self, address: str) -> None:
        """Refresh `hc get <address>`, `hc ls --json`, and `ps` atomically under lock."""
        addr_info = hc_get(address)
        ls_payload = run_json(["hc", "ls", "--json"])
        procs = processes()
        raw_terminals = ls_payload.get("terminals")
        with self._lock:
            self.info[address] = addr_info
            if isinstance(raw_terminals, list):
                self.listed = True
                self.terminals = {
                    str(row["address"]): row
                    for row in raw_terminals
                    if isinstance(row, dict)
                    and row.get("state") == "live"
                    and row.get("address")
                }
            else:
                self.listed = False
                self.terminals = {}
            self.procs = procs

    def foreground(self, address: str) -> tuple[int | None, list[str]]:
        """Return `(foreground_pid, foreground_argv)` for *address*."""
        with self._lock:
            fg = (self.info.get(address) or {}).get("foreground")
        if not isinstance(fg, dict):
            return None, []
        pid = fg.get("pid")
        argv = fg.get("argv")
        fg_pid = int(pid) if isinstance(pid, int) else None
        fg_argv = [str(x) for x in argv] if isinstance(argv, list) else []
        return fg_pid, fg_argv

    def descendants(self, pid: int) -> list[int]:
        """Return *pid* and all descendant PIDs in BFS order."""
        with self._lock:
            procs_copy = dict(self.procs)
        children_by_parent: dict[int, list[int]] = {}
        for child_pid, (ppid, _, _) in procs_copy.items():
            children_by_parent.setdefault(ppid, []).append(child_pid)
        found: list[int] = []
        queue = [pid]
        seen = {pid}
        while queue:
            cur = queue.pop(0)
            found.append(cur)
            for child in children_by_parent.get(cur, ()):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        return found

    def tree(self, address: str) -> list[int]:
        """Return PIDs in the terminal's foreground tree followed by its root PID tree."""
        with self._lock:
            fg_pid, _ = self.foreground(address)
            root_pid = (self.info.get(address) or {}).get("pid")
        seen: set[int] = set()
        ordered: list[int] = []
        for pid in (fg_pid, root_pid if isinstance(root_pid, int) else None):
            if pid is None:
                continue
            for member in self.descendants(pid):
                if member not in seen:
                    seen.add(member)
                    ordered.append(member)
        return ordered

    def _sample_jobs(
        self,
        address: str,
        kind: HarnessKind,
        harness_binaries: Mapping[HarnessKind, tuple[str, ...]],
    ) -> dict[str, str]:
        with self._lock:
            fg_pid, _ = self.foreground(address)
            procs_snap = dict(self.procs)
            terms_snap = dict(self.terminals)
            info_snap = dict(self.info)

        found: dict[str, str] = {}
        if fg_pid is not None:
            for child in self.descendants(fg_pid)[1:]:
                row = procs_snap.get(child)
                if not row:
                    continue
                args = row[2].strip()
                if (
                    SERVICE_RE.search(args)
                    or is_transient_or_shell(args)
                    or owned_by_harness(kind, args, harness_binaries)
                ):
                    continue
                found[f"pid:{child}"] = (
                    args.split(" && ", 1)[-1][:120]
                    if "shell-snapshots" in args
                    else args[:120]
                )

        valid_parents = {address} | ({str(fg_pid)} if fg_pid is not None else set())
        for other_addr, row in terms_snap.items():
            if other_addr == address:
                continue
            other_info = info_snap.get(other_addr) or {}
            parent = row.get("parent")
            if parent is None:
                parent = other_info.get("parent")
            die_with_parent = row.get("die_with_parent")
            if die_with_parent is None:
                die_with_parent = other_info.get("die_with_parent")
            if (
                parent is not None
                and str(parent) in valid_parents
                and die_with_parent is not False
            ):
                found[f"hc:{other_addr}"] = (
                    f"hc child terminal {other_addr} dies with this session"
                )
        return found

    def active_jobs(
        self,
        address: str,
        kind: HarnessKind,
        harness_binaries: Mapping[HarnessKind, tuple[str, ...]],
        *,
        confirm_delay: float = 1.5,
    ) -> list[str]:
        """Return persistent background jobs (2-sample confirmation drops transient statusline children)."""
        first = self._sample_jobs(address, kind, harness_binaries)
        if not first or confirm_delay <= 0:
            return list(first.values())
        time.sleep(confirm_delay)
        self.refresh_terminal(address)
        second = self._sample_jobs(address, kind, harness_binaries)
        persistent_keys = sorted(set(first) & set(second))
        return [second[k] for k in persistent_keys]
