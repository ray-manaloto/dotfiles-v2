"""Pluggable 6-harness adapters for `herdr_roll`."""

from __future__ import annotations

import re
import shlex
import shutil
import sqlite3
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import quote

import orjson

from herdr_roll.herdr import (
    is_bare_shell,
    open_file_paths,
    owned_by_harness,
    program_name,
    run_cmd,
    unwrap_launchers,
)
from herdr_roll.models import HarnessKind, TerminalSnapshot

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
CODEX_ROLLOUT_RE = re.compile(
    rf"(/\S*rollout-\S*?-({UUID_RE.pattern})\.jsonl)", re.IGNORECASE
)
HERMES_BANNER_RE = re.compile(
    r"(?:Session|session_id)\s*[:=]\s*local-([0-9a-f]{8,})\b|\blocal-([0-9a-f]{8,})\b",
    re.IGNORECASE,
)
CODEX_FOOTER_MODEL_RE = re.compile(
    r"(GPT-[0-9A-Za-z._-]+)\s+(xhigh|high|medium|low)\s+·"
)

IDLE_STATES = frozenset({"idle", "done"})
WAITING_STATES = frozenset({"blocked", "permission", "error"})
HARNESS_BINARIES: dict[HarnessKind, tuple[str, ...]] = {
    HarnessKind.AGY: ("agy",),
    HarnessKind.CODEX: ("codex",),
    HarnessKind.CLAUDE: ("claude",),
    HarnessKind.OPENCODE: ("opencode",),
    HarnessKind.HERMES: ("hermes",),
    HarnessKind.ZCODE: ("zcode", "zcode-cli", "zcode.js", "zcode.mjs", "zcode.cjs"),
}


def _is_zcode_token(tok: str) -> bool:
    return Path(tok).name in {
        "zcode",
        "zcode.js",
        "zcode.mjs",
        "zcode.cjs",
        "zcode-cli",
    }


class AdapterSkipError(Exception):
    """Fail-closed skip reason produced by a harness adapter."""

    def __init__(self, code: str, *notes: str) -> None:
        super().__init__(code)
        self.code = code
        self.notes = notes


class HarnessAdapter(Protocol):
    """Contract implemented by each supported Herdr harness."""

    kind: HarnessKind

    def matches(
        self, status_app: str | None, fg_argv: list[str], term_argv: list[str]
    ) -> bool: ...

    def session_id(
        self,
        *,
        address: str,
        tree_pids: list[int],
        fg_argv: list[str],
        term_argv: list[str],
        cwd: str | None,
        title: str,
        screen_text: str,
    ) -> str | None: ...

    def is_idle(
        self,
        status_state: str | None,
        fg_pid: int | None,
        tree_pids: list[int],
        screen_text: str,
    ) -> bool: ...

    def blocking_dialog(
        self, screen_text: str, status_state: str | None
    ) -> str | None: ...

    def exit_command(self) -> str: ...

    def extra_env(self, term_argv: list[str]) -> dict[str, str]: ...

    def resume_argv(
        self,
        term: TerminalSnapshot,
        *,
        session_id: str,
        is_coordinator: bool,
        prompt: str | None,
        screen_text: str,
    ) -> list[str]: ...

    def can_in_place_restart(
        self,
        fg_pid: int | None,
        tree_pids: list[int],
        *,
        force_clean_env: bool,
        fresh_tab: bool,
    ) -> bool: ...

    def in_place_command(self) -> str | None: ...

    def verify_resumed(
        self,
        *,
        old_pid: int | None,
        new_pid: int | None,
        fg_argv: list[str],
        tree_pids: list[int],
        status_state: str | None,
        screen_text: str,
        session_id: str,
    ) -> tuple[bool, str | None]: ...


def _connect_ro(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file():
        return None
    try:
        uri = f"file:{quote(str(db_path.expanduser().resolve()), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except OSError, sqlite3.Error:
        return None


def _dir_candidates(cwd: str | None) -> tuple[str, ...]:
    if not cwd:
        return ()
    raw = str(Path(cwd).expanduser())
    try:
        resolved = str(Path(cwd).expanduser().resolve())
    except OSError:
        resolved = raw
    return (raw,) if raw == resolved else (resolved, raw)


def _extract_flag_value(argv: list[str], names: Collection[str]) -> str | None:
    for idx, word in enumerate(argv):
        for flag in names:
            if word == flag and idx + 1 < len(argv):
                return argv[idx + 1]
            if word.startswith(f"{flag}="):
                return word.split("=", 1)[1]
    return None


def _universal_blocking_dialog(
    screen_text: str, status_state: str | None
) -> str | None:
    if status_state in WAITING_STATES:
        return f"status is {status_state}"
    lower = screen_text.lower()
    if "hooks need review" in lower or "continue without trusting" in lower:
        return "Hooks need review dialog requires owner decision"
    if re.search(r"trust (this|the) folder|do you trust", screen_text, re.IGNORECASE):
        return "Trust folder prompt requires owner decision"
    if "bypass permissions mode" in lower and "yes, i accept" in lower:
        return "Bypass permissions confirmation requires owner decision"
    if (
        "sign in to" in lower
        or "sign in with chatgpt" in lower
        or "login required" in lower
        or ("oauth" in lower and "approve" in lower)
    ):
        return "Authentication prompt requires owner decision"
    return None


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_by_table(
    words: list[str],
    keep: dict[str, int | str],
    drop: dict[str, int | str],
) -> list[str]:
    """Filter CLI flags strictly using KEEP/DROP arity tables; fail closed on unknown/dangling flags."""
    kept: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        name, equals, _ = word.partition("=")
        i += 1
        if word == "--":
            break
        if not word.startswith("-"):
            continue
        arity = keep.get(name, drop.get(name))
        if arity is None:
            raise AdapterSkipError(
                "skipped-unknown-flag", f"{name} in {shlex.join(words)}"
            )
        taken = [word]
        if not equals and arity == 1:
            if i >= len(words):
                raise AdapterSkipError(
                    "skipped-dangling-flag",
                    f"{name} missing value in {shlex.join(words)}",
                )
            taken.append(words[i])
            i += 1
        elif not equals and arity in {"?", "*"}:
            while i < len(words) and not words[i].startswith("-"):
                taken.append(words[i])
                i += 1
                if arity == "?":
                    break
        if name in keep:
            kept.extend(taken)
    return kept


def _parse_leading_json_object(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object via `orjson`, tolerating trailing non-JSON bytes."""
    stripped = raw.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        decoded = orjson.loads(stripped)
        return decoded if isinstance(decoded, dict) else None
    except orjson.JSONDecodeError:
        pass
    depth = 0
    in_string = False
    escape = False
    for idx, ch in enumerate(stripped):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    decoded = orjson.loads(stripped[: idx + 1])
                    return decoded if isinstance(decoded, dict) else None
                except orjson.JSONDecodeError:
                    return None
    return None


class BaseAdapter:
    """Shared base helper methods for adapters."""

    kind: HarnessKind

    def is_idle(
        self,
        status_state: str | None,
        fg_pid: int | None,
        tree_pids: list[int],
        screen_text: str,
    ) -> bool:
        del fg_pid, tree_pids, screen_text
        return status_state in IDLE_STATES

    def blocking_dialog(self, screen_text: str, status_state: str | None) -> str | None:
        return _universal_blocking_dialog(screen_text, status_state)

    def exit_command(self) -> str:
        return "/exit"

    def extra_env(self, term_argv: list[str]) -> dict[str, str]:
        del term_argv
        return {}

    def can_in_place_restart(
        self,
        fg_pid: int | None,
        tree_pids: list[int],
        *,
        force_clean_env: bool,
        fresh_tab: bool,
    ) -> bool:
        del fg_pid, tree_pids, force_clean_env, fresh_tab
        return False

    def in_place_command(self) -> str | None:
        return None


class AgyAdapter(BaseAdapter):
    """Antigravity CLI (`agy` / `agy-hc`) adapter."""

    kind = HarnessKind.AGY

    SESSION_FLAGS: ClassVar[frozenset[str]] = frozenset(
        {"--conversation", "--resume", "-r"}
    )

    KEEP: ClassVar[dict[str, int | str]] = {
        "--add-dir": 1,
        "--agent": 1,
        "--dangerously-skip-permissions": 0,
        "--effort": 1,
        "--log-file": 1,
        "--mode": 1,
        "--model": 1,
        "--project": 1,
        "--remote-agent": 0,
        "--remote-control": 0,
        "--sandbox": 0,
        "--settings": 1,
    }
    DROP: ClassVar[dict[str, int | str]] = {
        "-c": 0,
        "--continue": 0,
        "--conversation": 1,
        "-r": 1,
        "--resume": 1,
        "--disable-slash-commands": 0,
        "-i": 1,
        "--prompt-interactive": 1,
        "-p": 1,
        "--print": 1,
        "--prompt": 1,
        "--print-timeout": 1,
        "--new-project": 0,
        "--input-format": 1,
        "--output-format": 1,
        "--json-schema": 1,
    }

    def matches(
        self, status_app: str | None, fg_argv: list[str], term_argv: list[str]
    ) -> bool:
        return (
            status_app == "antigravity"
            or program_name(fg_argv) == "agy"
            or program_name(term_argv) == "agy-hc"
        )

    def session_id(
        self,
        *,
        address: str,
        tree_pids: list[int],
        fg_argv: list[str],
        term_argv: list[str],
        cwd: str | None,
        title: str,
        screen_text: str,
    ) -> str | None:
        del address, cwd, title, screen_text
        for source in (unwrap_launchers(fg_argv), unwrap_launchers(term_argv)):
            conv = _extract_flag_value(source, self.SESSION_FLAGS)
            if conv and UUID_RE.fullmatch(conv):
                return conv
        if tree_pids:
            brain_ids = {
                m[1]
                for path_str in open_file_paths(tree_pids)
                if (
                    m := re.search(
                        rf"/antigravity-cli/brain/({UUID_RE.pattern})/", path_str
                    )
                )
            }
            if len(brain_ids) == 1:
                return next(iter(brain_ids))
        return None

    def resume_argv(
        self,
        term: TerminalSnapshot,
        *,
        session_id: str,
        is_coordinator: bool,
        prompt: str | None,
        screen_text: str,
    ) -> list[str]:
        del is_coordinator, screen_text
        words = unwrap_launchers(term.fg_argv) or unwrap_launchers(term.term_argv)
        kept = _filter_by_table(words[1:], self.KEEP, self.DROP) if words else []
        cmd = ["agy-hc", "--conversation", session_id, *kept]
        if prompt:
            cmd.extend(["-i", prompt])
        return cmd

    def verify_resumed(
        self,
        *,
        old_pid: int | None,
        new_pid: int | None,
        fg_argv: list[str],
        tree_pids: list[int],
        status_state: str | None,
        screen_text: str,
        session_id: str,
    ) -> tuple[bool, str | None]:
        del tree_pids, screen_text
        if new_pid is None or new_pid == old_pid:
            return False, "process PID did not advance"
        if program_name(fg_argv) not in {"agy", "agy-hc"}:
            return False, f"unexpected foreground {program_name(fg_argv)!r}"
        sid_in_argv = _extract_flag_value(unwrap_launchers(fg_argv), self.SESSION_FLAGS)
        if not sid_in_argv or sid_in_argv.lower() != session_id.lower():
            return False, f"--conversation {session_id} missing from argv"
        if status_state not in IDLE_STATES:
            return False, f"status is {status_state!r}"
        return True, None


class CodexAdapter(BaseAdapter):
    """Codex CLI (`codex` / `codex-hc`) adapter."""

    kind = HarnessKind.CODEX

    PAIR_FLAGS = frozenset(
        {
            "-a",
            "--ask-for-approval",
            "--add-dir",
            "-C",
            "--cd",
            "-c",
            "--config",
            "--disable",
            "--enable",
            "-i",
            "--image",
            "--local-provider",
            "-m",
            "--model",
            "-p",
            "--profile",
            "--remote",
            "--remote-auth-token-env",
            "-s",
            "--sandbox",
        }
    )
    BOOL_FLAGS = frozenset(
        {
            "--approve-for-me",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--no-alt-screen",
            "--no-daemon",
            "--not-so-yolo",
            "--oss",
            "--search",
            "--strict-config",
            "--worktree",
            "--yolo",
        }
    )
    DROP_BOOL_FLAGS = frozenset({"--all", "--include-non-interactive", "--last"})

    def matches(
        self, status_app: str | None, fg_argv: list[str], term_argv: list[str]
    ) -> bool:
        return (
            status_app == "codex"
            or program_name(fg_argv) == "codex"
            or program_name(term_argv) == "codex-hc"
        )

    def session_id(
        self,
        *,
        address: str,
        tree_pids: list[int],
        fg_argv: list[str],
        term_argv: list[str],
        cwd: str | None,
        title: str,
        screen_text: str,
    ) -> str | None:
        del address, fg_argv, term_argv, cwd, title, screen_text
        if not tree_pids:
            return None
        held_paths = open_file_paths(tree_pids)
        matches = [
            match
            for path_str in held_paths
            if (match := CODEX_ROLLOUT_RE.search(path_str)) is not None
        ]
        if not matches:
            return None
        resolved: set[str] = set()
        for match in matches:
            rollout_path = Path(match[1])
            root_id: str | None = None
            try:
                with rollout_path.open("rb") as handle:
                    first_line = handle.readline()
                meta = orjson.loads(first_line)
                payload = meta.get("payload") if isinstance(meta, dict) else None
                cand = payload.get("session_id") if isinstance(payload, dict) else None
                if isinstance(cand, str) and UUID_RE.fullmatch(cand):
                    root_id = cand
            except OSError, ValueError:
                pass
            resolved.add(root_id or match[2])
        return next(iter(resolved)) if len(resolved) == 1 else None

    def codex_flags(self, raw_argv: Sequence[str], screen_text: str) -> list[str]:
        """Extract clean, deduplicated Codex flags, stripping wrapper `shell_environment_policy.*` duplicates."""
        unwrapped = unwrap_launchers(raw_argv)
        if not unwrapped:
            return []
        tail = (
            unwrapped[1:]
            if Path(unwrapped[0]).name in {"codex", "codex-hc"}
            else unwrapped
        )
        kept_model: str | None = None
        kept_effort: str | None = None
        other: list[str] = []

        i = 0
        seen_subcommand = False
        while i < len(tail):
            token = tail[i]
            flag, eq, inline_val = token.partition("=")
            if token == "--":
                break
            if token in self.BOOL_FLAGS:
                if token not in other:
                    other.append(token)
                i += 1
                continue
            if token in self.DROP_BOOL_FLAGS:
                i += 1
                continue
            if flag in self.PAIR_FLAGS:
                val = inline_val if eq else (tail[i + 1] if i + 1 < len(tail) else None)
                i += 1 if eq else 2
                if val is None:
                    raise AdapterSkipError(
                        "skipped-dangling-flag",
                        f"{flag} missing value in {shlex.join(unwrapped)}",
                    )
                if flag == "--disable" and val == "daemon_auto_start":
                    continue
                if flag in {"-c", "--config"} and val.startswith(
                    "shell_environment_policy."
                ):
                    continue
                if flag in {"-m", "--model"}:
                    kept_model = val
                    continue
                if flag in {"-c", "--config"} and val.startswith(
                    "model_reasoning_effort="
                ):
                    kept_effort = val.split("=", 1)[1].strip().strip('"').strip("'")
                    continue
                other.extend([flag, val])
                continue
            if not token.startswith("-"):
                if not seen_subcommand and token == "resume":
                    seen_subcommand = True
                    i += (
                        2 if i + 1 < len(tail) and UUID_RE.fullmatch(tail[i + 1]) else 1
                    )
                    continue
                break
            raise AdapterSkipError(
                "skipped-unknown-flag", f"{token} in {shlex.join(unwrapped)}"
            )

        # Prefer effective model/effort from the bottom-most visible Codex footer line.
        footer_matches = list(
            CODEX_FOOTER_MODEL_RE.finditer("\n".join(screen_text.splitlines()[-10:]))
        )
        if footer_matches:
            kept_model = footer_matches[-1][1].lower()
            kept_effort = footer_matches[-1][2].lower()

        if kept_model:
            other.extend(["--model", kept_model])
        if kept_effort:
            other.extend(["-c", f'model_reasoning_effort="{kept_effort}"'])
        return other

    def resume_argv(
        self,
        term: TerminalSnapshot,
        *,
        session_id: str,
        is_coordinator: bool,
        prompt: str | None,
        screen_text: str,
    ) -> list[str]:
        del is_coordinator
        flags = self.codex_flags(term.fg_argv or term.term_argv, screen_text)
        cmd = ["codex-hc", "resume", session_id, *flags]
        if prompt:
            cmd.append(prompt)
        return cmd

    def verify_resumed(
        self,
        *,
        old_pid: int | None,
        new_pid: int | None,
        fg_argv: list[str],
        tree_pids: list[int],
        status_state: str | None,
        screen_text: str,
        session_id: str,
    ) -> tuple[bool, str | None]:
        del screen_text
        if new_pid is None or new_pid == old_pid:
            return False, "process PID did not advance"
        if program_name(fg_argv) not in {"codex", "codex-hc"}:
            return False, f"unexpected foreground {program_name(fg_argv)!r}"
        if status_state not in IDLE_STATES:
            return False, f"status is {status_state!r}"
        live_thread = self.session_id(
            address="",
            tree_pids=tree_pids,
            fg_argv=fg_argv,
            term_argv=[],
            cwd=None,
            title="",
            screen_text="",
        )
        if live_thread != session_id:
            return False, f"expected thread {session_id}, got {live_thread}"
        return True, None


def claude_record(pid: int | None) -> dict[str, Any] | None:
    """Read `~/.claude/sessions/<pid>.json` safely (supports trailing non-JSON data)."""
    if not pid:
        return None
    record = Path.home() / f".claude/sessions/{pid}.json"
    try:
        raw_text = record.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_leading_json_object(raw_text)


def claude_record_for_tree(
    fg_pid: int | None, tree_pids: list[int]
) -> dict[str, Any] | None:
    """Return the first valid `~/.claude/sessions/<pid>.json` across *tree_pids* or *fg_pid*."""
    ordered = list(tree_pids)
    if fg_pid is not None and fg_pid not in ordered:
        ordered.insert(0, fg_pid)
    for pid in ordered:
        rec = claude_record(pid)
        if rec is not None:
            return rec
    return None


class ClaudeAdapter(BaseAdapter):
    """Claude Code CLI (`claude`) adapter."""

    kind = HarnessKind.CLAUDE

    KEEP: ClassVar[dict[str, int | str]] = {
        "--add-dir": "*",
        "--agent": 1,
        "--agents": 1,
        "--allow-dangerously-skip-permissions": 0,
        "--allowedTools": "*",
        "--allowed-tools": "*",
        "--append-system-prompt": 1,
        "--autocompact": 1,
        "--ax-screen-reader": 0,
        "--bare": 0,
        "--betas": "*",
        "--brief": 0,
        "--chrome": 0,
        "--dangerously-skip-permissions": 0,
        "-d": "?",
        "--debug": "?",
        "--debug-file": 1,
        "--disable-slash-commands": 0,
        "--disallowedTools": "*",
        "--disallowed-tools": "*",
        "--effort": 1,
        "--exclude-dynamic-system-prompt-sections": 0,
        "--fallback-model": 1,
        "--ide": 0,
        "--max-budget-usd": 1,
        "--mcp-config": "*",
        "--model": 1,
        "-n": 1,
        "--name": 1,
        "--no-chrome": 0,
        "--permission-mode": 1,
        "--permission-prompts": 1,
        "--plugin-dir": 1,
        "--plugin-url": 1,
        "--prompt-suggestions": "?",
        "--remote-control": "?",
        "--remote-control-session-name-prefix": 1,
        "--restricted": 0,
        "--safe-mode": 0,
        "--setting-sources": 1,
        "--settings": 1,
        "--strict-mcp-config": 0,
        "--system-prompt": 1,
        "--system-prompt-snapshot": 1,
        "--tools": "*",
        "--verbose": 0,
    }
    DROP: ClassVar[dict[str, int | str]] = {
        "-r": "?",
        "--resume": "?",
        "-c": 0,
        "--continue": 0,
        "--session-id": 1,
        "--fork-session": 0,
        "--from-pr": "?",
        "--teleport": "?",
        "--cloud": "?",
        "--environment": 1,
        "--desktop": 0,
        "--bg": 0,
        "--background": 0,
        "--tmux": 0,
        "-w": "?",
        "--worktree": "?",
        "--file": "*",
        "-p": 0,
        "--print": 0,
        "--output-format": 1,
        "--input-format": 1,
        "--json-schema": 1,
        "--include-hook-events": 0,
        "--include-partial-messages": 0,
        "--replay-user-messages": 0,
        "--forward-subagent-text": 0,
        "--no-session-persistence": 0,
    }

    def matches(
        self, status_app: str | None, fg_argv: list[str], term_argv: list[str]
    ) -> bool:
        return (
            status_app == "claude-code"
            or program_name(fg_argv) == "claude"
            or program_name(term_argv) == "claude"
        )

    def session_id(
        self,
        *,
        address: str,
        tree_pids: list[int],
        fg_argv: list[str],
        term_argv: list[str],
        cwd: str | None,
        title: str,
        screen_text: str,
    ) -> str | None:
        del address, fg_argv, term_argv, cwd, title, screen_text
        for pid in tree_pids:
            rec = claude_record(pid)
            sid = rec.get("sessionId") if rec else None
            if isinstance(sid, str) and sid:
                return sid
        return None

    def is_idle(
        self,
        status_state: str | None,
        fg_pid: int | None,
        tree_pids: list[int],
        screen_text: str,
    ) -> bool:
        del screen_text
        if status_state not in IDLE_STATES:
            return False
        rec = claude_record_for_tree(fg_pid, tree_pids)
        if not rec:
            return True
        return rec.get("status", "idle") == "idle" and not rec.get("waitingFor")

    def can_in_place_restart(
        self,
        fg_pid: int | None,
        tree_pids: list[int],
        *,
        force_clean_env: bool,
        fresh_tab: bool,
    ) -> bool:
        if force_clean_env or fresh_tab or (fg_pid is None and not tree_pids):
            return False
        rec = claude_record_for_tree(fg_pid, tree_pids)
        return (
            isinstance(rec, dict)
            and not rec.get("bridgeSessionId")
            and rec.get("kind") != "bg"
            and rec.get("nameSource") != "user"
            and isinstance(rec.get("startedAt"), int)
        )

    def in_place_command(self) -> str | None:
        return "/restart"

    def resume_argv(
        self,
        term: TerminalSnapshot,
        *,
        session_id: str,
        is_coordinator: bool,
        prompt: str | None,
        screen_text: str,
    ) -> list[str]:
        del screen_text
        words = unwrap_launchers(term.fg_argv) or unwrap_launchers(term.term_argv)
        kept = _filter_by_table(words[1:], self.KEEP, self.DROP) if words else []
        resume_target = "chief-of-staff" if is_coordinator else session_id
        cmd = ["claude", *kept, "--resume", resume_target]
        if prompt:
            cmd.append(prompt)
        return cmd

    def verify_resumed(
        self,
        *,
        old_pid: int | None,
        new_pid: int | None,
        fg_argv: list[str],
        tree_pids: list[int],
        status_state: str | None,
        screen_text: str,
        session_id: str,
    ) -> tuple[bool, str | None]:
        del screen_text
        if new_pid is None:
            return False, "no live Claude PID found"
        if program_name(fg_argv) != "claude":
            return False, f"unexpected foreground {program_name(fg_argv)!r}"
        if status_state not in IDLE_STATES:
            return False, f"status is {status_state!r}"
        live_sid = self.session_id(
            address="",
            tree_pids=tree_pids,
            fg_argv=fg_argv,
            term_argv=[],
            cwd=None,
            title="",
            screen_text="",
        )
        if live_sid != session_id:
            return False, f"expected session {session_id}, got {live_sid}"
        if old_pid is not None and new_pid == old_pid:
            return False, "process PID did not advance"
        return True, None


class OpenCodeAdapter(BaseAdapter):
    """OpenCode CLI (`opencode`) adapter with SQLite fallback for fresh sessions."""

    kind = HarnessKind.OPENCODE

    _FALLBACK_TITLE_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?:New session|Child session) - \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
    )

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        return self._db_path or (Path.home() / ".local/share/opencode/opencode.db")

    def matches(
        self, status_app: str | None, fg_argv: list[str], term_argv: list[str]
    ) -> bool:
        return (
            status_app == "opencode"
            or program_name(fg_argv) == "opencode"
            or program_name(term_argv) == "opencode"
        )

    @classmethod
    def _is_fallback_title(cls, value: Any) -> bool:
        if not isinstance(value, str):
            return True
        stripped = value.strip()
        return not stripped or cls._FALLBACK_TITLE_RE.fullmatch(stripped) is not None

    @classmethod
    def _matches_oc_title(cls, db_title: str, clean_title: str) -> bool:
        if cls._is_fallback_title(db_title):
            return False
        if db_title == clean_title:
            return True
        utf16 = db_title.encode("utf-16-le")
        if len(utf16) > 80:
            trunc37 = utf16[:74].decode("utf-16-le", errors="ignore")
            return clean_title in (f"{trunc37}…", f"{trunc37}...")
        return False

    def _lookup_sqlite(self, cwd: str | None, title: str) -> str | None:
        dirs = _dir_candidates(cwd)
        if not dirs:
            return None
        conn = _connect_ro(self.db_path)
        if conn is None:
            return None
        try:
            tables = {
                str(r["name"])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name IN ('session_v2', 'session')"
                ).fetchall()
            }
            table = (
                "session_v2"
                if "session_v2" in tables
                else ("session" if "session" in tables else None)
            )
            if table is None:
                return None

            cols = {
                str(r["name"])
                for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not {"id", "directory", "title"}.issubset(cols):
                return None

            placeholders = ", ".join("?" for _ in dirs)
            clauses = [f"directory IN ({placeholders})"]
            if "parent_id" in cols:
                clauses.append("parent_id IS NULL")
            if "time_archived" in cols:
                clauses.append("time_archived IS NULL")
            if "time_deleted" in cols:
                clauses.append("time_deleted IS NULL")
            order_col = "time_updated" if "time_updated" in cols else "rowid"
            rows = conn.execute(
                f"SELECT id, title FROM {table} WHERE {' AND '.join(clauses)} "
                f"ORDER BY {order_col} DESC",
                dirs,
            ).fetchall()
        except sqlite3.Error:
            return None
        finally:
            conn.close()

        if not rows:
            return None

        raw_title = title.strip()
        if raw_title == "OpenCode":
            if len(rows) == 1 and self._is_fallback_title(rows[0]["title"]):
                return str(rows[0]["id"])
            return None

        match = re.match(r"^OC\s*\|\s*(.+)$", raw_title)
        if match is None:
            return None
        clean_title = match.group(1).strip()
        if not clean_title:
            return None

        matches = [
            str(r["id"])
            for r in rows
            if r["title"] and self._matches_oc_title(str(r["title"]), clean_title)
        ]
        return matches[0] if len(matches) == 1 else None

    def session_id(
        self,
        *,
        address: str,
        tree_pids: list[int],
        fg_argv: list[str],
        term_argv: list[str],
        cwd: str | None,
        title: str,
        screen_text: str,
    ) -> str | None:
        del address, tree_pids, screen_text
        for source in (unwrap_launchers(fg_argv), unwrap_launchers(term_argv)):
            sid = _extract_flag_value(source, {"-s", "--session"})
            if sid and sid.startswith("ses_"):
                return sid
        return self._lookup_sqlite(cwd, title)

    def resume_argv(
        self,
        term: TerminalSnapshot,
        *,
        session_id: str,
        is_coordinator: bool,
        prompt: str | None,
        screen_text: str,
    ) -> list[str]:
        del is_coordinator, screen_text
        raw = unwrap_launchers(term.fg_argv) or unwrap_launchers(term.term_argv)
        tail = raw[1:] if raw and Path(raw[0]).name == "opencode" else raw
        kept: list[str] = []
        has_server = False
        i = 0
        while i < len(tail):
            tok = tail[i]
            flag, eq, inline_val = tok.partition("=")
            if tok == "--auto":
                if tok not in kept:
                    kept.append(tok)
                i += 1
            elif flag == "--server":
                val = inline_val if eq else (tail[i + 1] if i + 1 < len(tail) else None)
                i += 1 if eq else 2
                if val is None:
                    raise AdapterSkipError(
                        "skipped-dangling-flag",
                        f"--server missing value in {shlex.join(raw)}",
                    )
                kept.extend(["--server", val])
                has_server = True
            elif flag == "--standalone":
                i += 1
            elif flag in {"-m", "--model"}:
                val = inline_val if eq else (tail[i + 1] if i + 1 < len(tail) else None)
                i += 1 if eq else 2
                if val is None:
                    raise AdapterSkipError(
                        "skipped-dangling-flag",
                        f"{flag} missing value in {shlex.join(raw)}",
                    )
                kept.extend([flag, val])
            elif flag in {"-s", "--session", "--prompt"}:
                i += 1 if eq else 2
            elif flag in {"-c", "--continue"}:
                i += 1
            else:
                i += 1

        cmd = [
            "opencode",
            *(["--standalone"] if not has_server else []),
            *kept,
            "-s",
            session_id,
        ]
        if prompt:
            cmd.extend(["--prompt", prompt])
        return cmd

    def verify_resumed(
        self,
        *,
        old_pid: int | None,
        new_pid: int | None,
        fg_argv: list[str],
        tree_pids: list[int],
        status_state: str | None,
        screen_text: str,
        session_id: str,
    ) -> tuple[bool, str | None]:
        del tree_pids, screen_text
        if new_pid is None or new_pid == old_pid:
            return False, "process PID did not advance"
        if program_name(fg_argv) != "opencode":
            return False, f"unexpected foreground {program_name(fg_argv)!r}"
        if status_state not in IDLE_STATES:
            return False, f"status is {status_state!r}"
        sid_in_argv = _extract_flag_value(
            unwrap_launchers(fg_argv), {"-s", "--session"}
        )
        if old_pid is not None and sid_in_argv != session_id:
            return False, f"-s {session_id} missing from argv"
        return True, None


class HermesAdapter(BaseAdapter):
    """Hermes Agent TUI (`hermes chat --tui`) adapter."""

    kind = HarnessKind.HERMES

    KEEP: ClassVar[dict[str, int | str]] = {
        "--accept-hooks": 0,
        "--base-url": 1,
        "--checkpoints": 0,
        "--cli": 0,
        "--dev": 0,
        "--ignore-rules": 0,
        "--ignore-user-config": 0,
        "--in": 1,
        "-m": 1,
        "--max-turns": 1,
        "--model": 1,
        "--native": 0,
        "--no-restore-cwd": 0,
        "--pass-session-id": 0,
        "--provider": 1,
        "--reasoning": 1,
        "--run-budget": 1,
        "--safe-mode": 0,
        "-s": 1,
        "--skills": 1,
        "--source": 1,
        "-t": 1,
        "--toolsets": 1,
        "--tui": 0,
        "--tui-native": 0,
        "-v": 0,
        "--verbose": 0,
        "-w": 0,
        "--worktree": 0,
        "--yolo": 0,
    }
    DROP: ClassVar[dict[str, int | str]] = {
        "-c": "?",
        "--continue": "?",
        "--create-if-missing": 0,
        "-q": 1,
        "--query": 1,
        "--query-file": 1,
        "--oneshot": 0,
        "-Q": 0,
        "-r": 1,
        "--resume": 1,
    }

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        return self._db_path or (Path.home() / ".hermes/state.db")

    def matches(
        self, status_app: str | None, fg_argv: list[str], term_argv: list[str]
    ) -> bool:
        del status_app
        return any(Path(tok).name == "hermes" for tok in (*fg_argv, *term_argv))

    def _lookup_sqlite(self, prefix: str | None, *, title: str = "") -> str | None:
        conn = _connect_ro(self.db_path)
        if conn is None:
            return None
        try:
            if prefix:
                rows = conn.execute(
                    r"SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\' ORDER BY started_at DESC LIMIT 2",
                    (f"{_like_escape(prefix)}%",),
                ).fetchall()
                if len(rows) == 1:
                    return str(rows[0]["id"])
            cleaned = (
                re.sub(r"^[✓✗•●▸]\s*", "", title).split(" · ", 1)[0].rstrip("…").strip()
            )
            if len(cleaned) >= 12:
                rows = conn.execute(
                    r"SELECT id FROM sessions WHERE title LIKE ? ESCAPE '\' ORDER BY started_at DESC LIMIT 2",
                    (f"{_like_escape(cleaned)}%",),
                ).fetchall()
                if len(rows) == 1:
                    return str(rows[0]["id"])
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        return None

    def session_id(
        self,
        *,
        address: str,
        tree_pids: list[int],
        fg_argv: list[str],
        term_argv: list[str],
        cwd: str | None,
        title: str,
        screen_text: str,
    ) -> str | None:
        del address, tree_pids, cwd
        for source in (fg_argv, term_argv):
            sid = _extract_flag_value(source, {"--resume", "-r"})
            if sid and sid.lower() != "latest" and len(sid) >= 12:
                return sid
        prefixes = {
            f"local-{m[1] or m[2]}" for m in HERMES_BANNER_RE.finditer(screen_text)
        }
        prefix = next(iter(prefixes)) if len(prefixes) == 1 else None
        return self._lookup_sqlite(prefix, title=title)

    def exit_command(self) -> str:
        return "/quit"

    def extra_env(self, term_argv: list[str]) -> dict[str, str]:
        extracted: dict[str, str] = {}
        for tok in term_argv:
            if "=" in tok and tok.split("=", 1)[0] in {"HERMES_TUI_DIR", "HERMES_NODE"}:
                k, v = tok.split("=", 1)
                extracted[k] = v
        if "HERMES_TUI_DIR" not in extracted:
            tui = Path.home() / ".local/share/hermes-cli.noindex/tui"
            if tui.is_dir():
                extracted["HERMES_TUI_DIR"] = str(tui)
        if "HERMES_NODE" not in extracted:
            node = run_cmd(["mise", "which", "node"], timeout=5.0)
            if node and node.strip():
                extracted["HERMES_NODE"] = node.strip()
        return extracted

    def is_idle(
        self,
        status_state: str | None,
        fg_pid: int | None,
        tree_pids: list[int],
        screen_text: str,
    ) -> bool:
        del fg_pid, tree_pids
        if status_state is not None and status_state not in IDLE_STATES:
            return False
        nonblank = [line for line in screen_text.splitlines() if line.strip()]
        tail_lines = nonblank[-8:]
        tail_text = "\n".join(tail_lines)
        has_prompt = any(line.lstrip().startswith("❯") for line in tail_lines)
        has_spinner = any(
            spin in tail_text
            for spin in ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
        )
        return has_prompt and not has_spinner

    def resume_argv(
        self,
        term: TerminalSnapshot,
        *,
        session_id: str,
        is_coordinator: bool,
        prompt: str | None,
        screen_text: str,
    ) -> list[str]:
        del is_coordinator, screen_text
        raw = list(term.fg_argv) or list(term.term_argv)
        hermes_bin = next((tok for tok in raw if Path(tok).name == "hermes"), None)
        if not hermes_bin:
            hermes_bin = shutil.which("hermes") or str(
                Path.home() / ".local/share/hermes-cli.noindex/bin/hermes"
            )
        idx = next((i for i, tok in enumerate(raw) if Path(tok).name == "hermes"), -1)
        tail = raw[idx + 1 :] if idx >= 0 else []
        if tail and tail[0] == "chat":
            tail = tail[1:]
        kept = _filter_by_table(tail, self.KEEP, self.DROP) if tail else ["--tui"]
        cmd = [hermes_bin, "chat", *kept, "--resume", session_id]
        if prompt:
            cmd.extend(["-q", prompt])
        return cmd

    def verify_resumed(
        self,
        *,
        old_pid: int | None,
        new_pid: int | None,
        fg_argv: list[str],
        tree_pids: list[int],
        status_state: str | None,
        screen_text: str,
        session_id: str,
    ) -> tuple[bool, str | None]:
        del tree_pids
        if new_pid is None or new_pid == old_pid:
            return False, "process PID did not advance"
        if not any(Path(tok).name == "hermes" for tok in fg_argv):
            return False, f"unexpected foreground {fg_argv[:3]!r}"
        if not self.is_idle(status_state, new_pid, [], screen_text):
            return False, "Hermes input prompt ❯ not yet ready"
        sid_in_argv = _extract_flag_value(fg_argv, {"--resume", "-r"})
        if old_pid is not None and sid_in_argv != session_id:
            return False, f"--resume {session_id} missing from argv"
        return True, None


class ZCodeAdapter(BaseAdapter):
    """ZCode CLI (`zcode`) adapter with SQLite fallback."""

    kind = HarnessKind.ZCODE

    KEEP: ClassVar[dict[str, int | str]] = {
        "--disallowed-tools": "*",
        "--disallowedTools": "*",
        "--effort": 1,
        "--force-mcs": 0,
        "--json": 0,
        "--locale": 1,
        "-m": 1,
        "--mode": 1,
        "--model": 1,
        "--no-browser": 0,
        "--no-color": 0,
        "--toolsearch-mode": 1,
        "-v": 0,
        "--verbose": 0,
        "--workflow-mode": 1,
    }
    DROP: ClassVar[dict[str, int | str]] = {
        "--attach": 1,
        "-c": 0,
        "--continue": 0,
        "--cwd": 1,
        "-p": 1,
        "--prompt": 1,
        "--resume": 1,
        "--target": 1,
        "--target-replace": 0,
    }

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        return self._db_path or (Path.home() / ".zcode/cli/db/db.sqlite")

    def matches(
        self, status_app: str | None, fg_argv: list[str], term_argv: list[str]
    ) -> bool:
        del status_app
        return (
            program_name(fg_argv) == "zcode"
            or program_name(term_argv) == "zcode"
            or (
                bool(fg_argv)
                and owned_by_harness(self.kind, shlex.join(fg_argv), HARNESS_BINARIES)
            )
            or (
                bool(term_argv)
                and owned_by_harness(self.kind, shlex.join(term_argv), HARNESS_BINARIES)
            )
        )

    def _lookup_sqlite(self, cwd: str | None, title: str) -> str | None:
        dirs = _dir_candidates(cwd)
        if not dirs:
            return None
        conn = _connect_ro(self.db_path)
        if conn is None:
            return None
        try:
            placeholders = ", ".join("?" for _ in dirs)
            cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(session)").fetchall()
            }
            clauses = [f"directory IN ({placeholders})"]
            if "parent_id" in cols:
                clauses.append("parent_id IS NULL")
            if "time_archived" in cols:
                clauses.append("time_archived IS NULL")
            if "time_deleted" in cols:
                clauses.append("time_deleted IS NULL")
            rows = conn.execute(
                f"SELECT id, title FROM session WHERE {' AND '.join(clauses)} ORDER BY time_updated DESC LIMIT 10",
                dirs,
            ).fetchall()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if not rows:
            return None
        if title:
            matches = [
                str(r["id"]) for r in rows if r["title"] and str(r["title"]) in title
            ]
            return matches[0] if len(matches) == 1 else None
        return str(rows[0]["id"]) if len(rows) == 1 else None

    def session_id(
        self,
        *,
        address: str,
        tree_pids: list[int],
        fg_argv: list[str],
        term_argv: list[str],
        cwd: str | None,
        title: str,
        screen_text: str,
    ) -> str | None:
        del address, tree_pids, screen_text
        for source in (unwrap_launchers(fg_argv), unwrap_launchers(term_argv)):
            sid = _extract_flag_value(source, {"--resume"})
            if sid and sid.startswith("sess_"):
                return sid
        return self._lookup_sqlite(cwd, title)

    def is_idle(
        self,
        status_state: str | None,
        fg_pid: int | None,
        tree_pids: list[int],
        screen_text: str,
    ) -> bool:
        del fg_pid, tree_pids
        if status_state is not None and status_state not in IDLE_STATES:
            return False
        nonblank = [line for line in screen_text.splitlines() if line.strip()]
        if not nonblank:
            return False
        lower_tail = "\n".join(nonblank[-8:]).lower()
        return not any(
            busy in lower_tail
            for busy in (
                "esc to cancel",
                "thinking…",
                "running tool",
                "ctrl+c to cancel",
            )
        )

    def resume_argv(
        self,
        term: TerminalSnapshot,
        *,
        session_id: str,
        is_coordinator: bool,
        prompt: str | None,
        screen_text: str,
    ) -> list[str]:
        del is_coordinator, screen_text
        words = unwrap_launchers(term.fg_argv) or unwrap_launchers(term.term_argv)
        idx = next((i for i, tok in enumerate(words) if _is_zcode_token(tok)), -1)
        tail = words[idx + 1 :] if idx >= 0 else words[1:]
        if tail and tail[0] == "tui":
            tail = tail[1:]
        kept = _filter_by_table(tail, self.KEEP, self.DROP) if tail else []
        cmd = ["zcode", *kept, "--resume", session_id]
        if prompt:
            cmd.extend(["--prompt", prompt])
        return cmd

    def verify_resumed(
        self,
        *,
        old_pid: int | None,
        new_pid: int | None,
        fg_argv: list[str],
        tree_pids: list[int],
        status_state: str | None,
        screen_text: str,
        session_id: str,
    ) -> tuple[bool, str | None]:
        del tree_pids
        if new_pid is None or new_pid == old_pid:
            return False, "process PID did not advance"
        if program_name(fg_argv) != "zcode" and not (
            bool(fg_argv)
            and owned_by_harness(self.kind, shlex.join(fg_argv), HARNESS_BINARIES)
        ):
            return False, f"unexpected foreground {fg_argv[:3]!r}"
        if _universal_blocking_dialog(screen_text, status_state):
            return False, "blocking dialog visible"
        if not self.is_idle(status_state, new_pid, [], screen_text):
            return False, "ZCode UI not yet ready"
        sid_in_argv = _extract_flag_value(unwrap_launchers(fg_argv), {"--resume"})
        if old_pid is not None and sid_in_argv != session_id:
            return False, f"--resume {session_id} missing from argv"
        return True, None


ADAPTER_ORDER: tuple[HarnessAdapter, ...] = (
    AgyAdapter(),
    CodexAdapter(),
    ClaudeAdapter(),
    OpenCodeAdapter(),
    HermesAdapter(),
    ZCodeAdapter(),
)
ADAPTERS: dict[HarnessKind, HarnessAdapter] = {
    adapter.kind: adapter for adapter in ADAPTER_ORDER
}


def detect_kind(
    status_app: str | None, fg_argv: list[str], term_argv: list[str]
) -> HarnessKind:
    """Identify the running harness kind or fall back to `HarnessKind.SHELL`."""
    for adapter in ADAPTER_ORDER:
        if adapter.matches(status_app, fg_argv, term_argv):
            return adapter.kind
    return HarnessKind.SHELL


def harness_running(
    kind: HarnessKind, fg_argv: list[str], status_app: str | None = None
) -> bool:
    """True when a terminal's foreground still runs *kind* (and not a bare shell)."""
    if is_bare_shell(fg_argv):
        return False
    adapter = ADAPTERS.get(kind)
    if adapter is None:
        return False
    if adapter.matches(status_app, fg_argv, []):
        return True
    return (
        owned_by_harness(kind, shlex.join(fg_argv[:4]), HARNESS_BINARIES)
        if fg_argv
        else False
    )
