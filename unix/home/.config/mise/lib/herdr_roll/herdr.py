"""Subprocess, Herdr (`hc` / `herdr-app`), `psutil` process tree, `tenacity` polling, and relaunch helpers."""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import orjson
import psutil
from tenacity import Retrying, retry_if_result, stop_after_delay, wait_exponential

from dotfiles_py.time_ns import format_ns_timestamp
from herdr_roll.models import AgyLogProof, HarnessKind

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

# macOS renders exiting subshells/statusline commands as `(bash)`, `(awk)`, `(git)`, etc.
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

ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    except OSError, subprocess.TimeoutExpired, ValueError:
        return None
    return completed.stdout if completed.returncode in allowed else None


def run_json(
    argv: Iterable[str], *, env: Mapping[str, str] | None = None, timeout: float = 15.0
) -> dict[str, Any]:
    """Run *argv* and decode a JSON object stdout via `orjson`, returning {} on error."""
    out = run_cmd(argv, env=env, timeout=timeout)
    if not out:
        return {}
    try:
        decoded = orjson.loads(out)
    except orjson.JSONDecodeError:
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
        decoded = orjson.loads(out)
    except orjson.JSONDecodeError:
        return None
    if not isinstance(decoded, dict) or "error" in decoded:
        return None
    return decoded


def processes() -> dict[int, tuple[int, str, str]]:
    """Return mapping `pid -> (ppid, lstart_str, args)` using C-backed `psutil`."""
    found: dict[int, tuple[int, str, str]] = {}
    for proc in psutil.process_iter(["pid", "ppid", "create_time", "cmdline", "name"]):
        try:
            info = proc.info
            pid = int(info["pid"])
            ppid = int(info.get("ppid") or 0)
            ctime = float(info.get("create_time") or 0.0)
            lstart = (
                dt.datetime.fromtimestamp(ctime, tz=dt.UTC)
                .astimezone()
                .strftime("%a %b %e %H:%M:%S %Y")
            )
            cmdline = info.get("cmdline") or []
            args = (
                shlex.join([str(part) for part in cmdline])
                if cmdline
                else str(info.get("name") or "")
            )
            found[pid] = (ppid, lstart, args)
        except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess:
            continue
    return found


def lsof_cwd(pid: int) -> str | None:
    """Return process working directory via C-backed `psutil`, or None if unavailable."""
    try:
        return psutil.Process(pid).cwd()
    except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError:
        return None


def open_file_paths(tree_pids: Sequence[int]) -> list[str]:
    """Return open file paths held by *tree_pids* using C-backed `psutil`."""
    paths: list[str] = []
    seen: set[str] = set()
    for pid in tree_pids:
        try:
            for item in psutil.Process(pid).open_files():
                if item.path and item.path not in seen:
                    seen.add(item.path)
                    paths.append(item.path)
        except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError:
            continue
    return paths


AGY_CLI_LOG_PID_RE = re.compile(r"Starting language server process with pid (\d+)")
AGY_CLI_LOG_HOOKS_RE = re.compile(
    r"loaded (\d+) named hooks from \d+ hooks\.json file\(s\)"
)
AGY_CLI_LOG_PRIMARY_CONV_RE = re.compile(
    r"(?:Resuming conversation "
    r"|GetConversationDetail: found conversation |Forwarding user message to conversation "
    r"|Sending user message to conversation )"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
AGY_CLI_LOG_STREAM_CONV_RE = re.compile(
    r"(?:Starting conversation update stream for |cascade_id:)"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _read_bounded_log(
    path: Path, *, head_bytes: int = 262_144, tail_bytes: int = 65_536
) -> str:
    size = path.stat().st_size
    if size <= head_bytes + tail_bytes:
        return path.read_text(encoding="utf-8", errors="replace")
    with path.open("rb") as handle:
        head = handle.read(head_bytes)
        handle.seek(max(0, size - tail_bytes))
        tail = handle.read(tail_bytes)
    return (
        head.decode("utf-8", errors="replace")
        + "\n"
        + tail.decode("utf-8", errors="replace")
    )


def process_start_info(pid: int | None) -> tuple[str | None, str | None]:
    """Return `(started_at_ns, lstart)` for *pid* via `psutil`, or `(None, None)`."""
    if pid is None or isinstance(pid, bool) or pid <= 0:
        return None, None
    try:
        ctime = float(psutil.Process(pid).create_time())
    except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError:
        return None, None
    if not math.isfinite(ctime) or ctime <= 0:
        return None, None
    started_ns = round(ctime * 1_000_000) * 1_000
    started_at = format_ns_timestamp(started_ns)
    lstart = (
        dt.datetime.fromtimestamp(started_ns // 1_000_000_000, tz=dt.UTC)
        .astimezone()
        .strftime("%a %b %e %H:%M:%S %Y")
    )
    return started_at, lstart


def _expected_agy_hook_count(hooks_json_path: Path | None = None) -> int | None:
    target = (
        hooks_json_path
        if hooks_json_path is not None
        else (Path.home() / ".gemini" / "config" / "hooks.json")
    )
    if not target.is_file():
        return None
    try:
        raw = orjson.loads(target.read_bytes())
    except OSError, orjson.JSONDecodeError:
        return None
    return len(raw) if isinstance(raw, dict) else None


def _log_file_arg(fg_argv: Sequence[str]) -> str | None:
    words = list(fg_argv)
    for idx, tok in enumerate(words):
        if tok == "--log-file" and idx + 1 < len(words):
            return words[idx + 1]
        if tok.startswith("--log-file="):
            return tok.split("=", 1)[1]
    return None


def _eval_agy_log_candidate(
    log_path: Path,
    tree_set: set[int],
    session_id: str | None,
    expected_hooks: int | None,
) -> AgyLogProof | None:
    try:
        text = _read_bounded_log(log_path)
    except OSError:
        return None

    pid_matches = AGY_CLI_LOG_PID_RE.findall(text)
    log_pid = int(pid_matches[-1]) if pid_matches else None
    pid_matched = log_pid is not None and log_pid in tree_set

    hook_matches = AGY_CLI_LOG_HOOKS_RE.findall(text)
    loaded_hooks = int(hook_matches[-1]) if hook_matches else None

    primary_convs = AGY_CLI_LOG_PRIMARY_CONV_RE.findall(text)
    stream_convs = AGY_CLI_LOG_STREAM_CONV_RE.findall(text)
    if primary_convs:
        if session_id:
            session_low = session_id.lower()
            matched_primary = next(
                (c for c in reversed(primary_convs) if c.lower() == session_low), None
            )
            conv_id: str | None = matched_primary or primary_convs[-1]
        else:
            conv_id = primary_convs[-1]
    elif stream_convs:
        conv_id = stream_convs[0]
    else:
        conv_id = None

    conv_matched = bool(
        session_id and conv_id and conv_id.lower() == session_id.lower()
    )
    hooks_matched = (
        loaded_hooks is not None
        and expected_hooks is not None
        and loaded_hooks == expected_hooks
    )
    conv_ok = conv_matched if session_id else conv_id is not None
    hooks_ok = hooks_matched if expected_hooks is not None else True
    verified = bool(pid_matched and conv_ok and hooks_ok)

    return AgyLogProof(
        log_path=str(log_path),
        pid=log_pid,
        pid_matched=pid_matched,
        conversation_id=conv_id,
        conversation_matched=conv_matched,
        loaded_hook_count=loaded_hooks,
        expected_hook_count=expected_hooks,
        hooks_matched=hooks_matched,
        verified=verified,
    )


def inspect_agy_cli_log(
    tree_pids: Sequence[int],
    session_id: str | None,
    *,
    fg_argv: Sequence[str] = (),
    hooks_json_path: Path | None = None,
) -> AgyLogProof:
    """Confirm active Antigravity CLI log PID, conversation ID, and loaded hook count."""
    expected_hooks = _expected_agy_hook_count(hooks_json_path)
    candidates: list[Path] = []
    seen: set[str] = set()
    explicit_log = _log_file_arg(fg_argv)
    if explicit_log:
        exp_path = Path(explicit_log).expanduser()
        candidates.append(exp_path)
        seen.add(str(exp_path))
    for raw_path in open_file_paths(tree_pids):
        norm = raw_path.replace("\\", "/")
        if (
            "/antigravity-cli/log/cli-" in norm
            and norm.endswith(".log")
            and raw_path not in seen
        ):
            seen.add(raw_path)
            candidates.append(Path(raw_path))

    existing = [p for p in candidates if p.is_file()]
    if not existing:
        return AgyLogProof(
            log_path=None,
            pid=None,
            pid_matched=False,
            conversation_id=None,
            conversation_matched=False,
            loaded_hook_count=None,
            expected_hook_count=expected_hooks,
            hooks_matched=False,
            verified=False,
        )

    tree_set = set(tree_pids)
    first_readable: AgyLogProof | None = None
    for cand in existing:
        proof = _eval_agy_log_candidate(cand, tree_set, session_id, expected_hooks)
        if proof is None:
            continue
        if first_readable is None:
            first_readable = proof
        if proof.pid_matched:
            return proof

    if first_readable is not None:
        return first_readable
    return AgyLogProof(
        log_path=str(existing[0]),
        pid=None,
        pid_matched=False,
        conversation_id=None,
        conversation_matched=False,
        loaded_hook_count=None,
        expected_hook_count=expected_hooks,
        hooks_matched=False,
        verified=False,
    )


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
    """Poll *probe* with exponential backoff backed by `tenacity` until non-None or *deadline_s*."""
    low = max(0.05, float(min_interval))
    high = max(low, float(max_interval))
    budget = max(0.0, float(deadline_s))

    def _sleep(seconds: float) -> None:
        time.sleep(seconds)

    retrying = Retrying(
        stop=stop_after_delay(budget),
        wait=wait_exponential(multiplier=low, exp_base=1.6, min=low, max=high),
        retry=retry_if_result(lambda value: value is None),
        retry_error_callback=lambda state: (
            state.outcome.result() if state.outcome is not None else None
        ),
        sleep=_sleep,
    )
    return retrying(probe)


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
                rest = (
                    rest[2:]
                    if rest[0] in {"-u", "-C", "-S"} and len(rest) >= 2
                    else rest[1:]
                )
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
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    if not tokens:
        return True
    first = tokens[0]
    if first in TRANSIENT_WRAPPED_NAMES or (
        first.startswith("(") and first.endswith(")")
    ):
        return True
    if first.startswith("-") and first.lstrip("-") in SHELLS:
        return True
    return Path(first).name.lstrip("-") in SHELLS and len(tokens) == 1


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
        """Refresh `herdr-app ls`, `hc ls --json`, parallel `hc get --json`, and then `psutil`."""
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

        # Read `psutil` after `hc get` so newly spawned foreground PIDs are present in `procs`.
        procs = processes()
        with self._lock:
            self.spaces = spaces
            self.listed = is_listed
            self.terminals = terminals
            self.info = info
            self.procs = procs

    def refresh_terminal(self, address: str) -> None:
        """Refresh `hc get <address>`, `hc ls --json`, and `psutil` atomically under lock."""
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
