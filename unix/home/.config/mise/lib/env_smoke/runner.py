"""Quota-gated smoke and agentsview skill-check runner for the 6 AI harnesses."""

from __future__ import annotations

import contextlib
import json
import os
import re
import selectors
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any

from dotfiles_py.generated.env_smoke_schema import (
    EnvSmokeCommandResult,
    EnvSmokeErrorCode,
    EnvSmokeErrorEntry,
    EnvSmokeHarness,
    EnvSmokeHarnessOutcome,
    EnvSmokeMode,
    EnvSmokeQuotaReading,
    EnvSmokeResponse,
    EnvSmokeReturnCode,
    EnvSmokeReturnEntry,
    EnvSmokeSkillCheck,
    EnvSmokeSkillVerdict,
    EnvSmokeStatus,
    EnvSmokeSummary,
)
from dotfiles_py.logging import get_logger
from dotfiles_py.time_ns import now_ns_pair
from env_smoke import quota

logger = get_logger("env_smoke")

SMOKE_PROMPT = "Reply with exactly: OK"
SKILL_NAME = "agentsview-finding-history"
SKILL_PROMPT = (
    f"Activate the {SKILL_NAME} skill and use it to list exactly 1 recent "
    "session. Global MCP count is 0: skip MCP tool calls and run the skill's "
    "agentsview CLI commands through your shell tool. Reply with the session id only."
)
DEFAULT_TIMEOUT_S = 120.0
DRAIN_TIMEOUT_S = 1.5
MAX_STREAM_BYTES = 4 * 1024 * 1024
EXCERPT_CHARS = 600
ALL_HARNESSES: tuple[EnvSmokeHarness, ...] = tuple(EnvSmokeHarness)

type WhichFn = Callable[[str], str | None]
type ExecFn = Callable[[Sequence[str], Mapping[str, str], float], EnvSmokeCommandResult]
type ProbeFn = Callable[[EnvSmokeHarness, str, datetime], EnvSmokeQuotaReading]

_RATE_LIMIT_RE = re.compile(
    r"(?:HTTP(?:/[\d.]+)?\s+429\b|\bstatus(?:\s+code)?[:=\s]+429\b|\b429\s+too\s+many\s+requests\b"
    r"|\btoo\s+many\s+requests\b|\brate[ _-]?limit(?:ed| exceeded)\b(?!\s*[:=]\s*false\b))",
    re.IGNORECASE,
)
_ZCODE_QUOTA_RE = re.compile(
    r"(?:providerCode|err(?:or)?Code|\"code\"|code|\[)\s*[:='\"\s]*(?:1113|1005)\b"
    r"|\berror\s+(?:1113|1005)\b|exceed(?:ed)?\s+quota\s+limit",
    re.IGNORECASE,
)
_MCP_KINDS = frozenset({"mcp_tool_call", "mcp_tool_use", "mcptoolcall", "mcp_call"})
_MCP_TOOL_NAMES = frozenset({"call_mcp_tool"})
_TOOL_EVENT_KINDS = (
    frozenset({"tool_use", "tool_call", "function_call", "command_execution", "tool"})
    | _MCP_KINDS
)
_SHELL_TOOL_NAMES = frozenset(
    {
        "bash",
        "shell",
        "terminal",
        "sh",
        "zsh",
        "exec_command",
        "run_command",
        "command_execution",
    }
)
_CMD_KEYS = frozenset(
    {"command", "cmd", "CommandLine", "commandLine", "command_line", "script"}
)
_AGY_EXIT_RE = re.compile(r"Command exited with code (-?[0-9]{1,6})\.?")
_HERMES_CAP_THRESHOLD = 5000
_RESULT_PAYLOAD_KEYS = frozenset(
    {
        "tools",
        "tool_info",
        "output",
        "result",
        "stdout",
        "stderr",
        "arguments",
        "args",
        "input",
    }
)
_AGENTSVIEW_BIN_RE = re.compile(r"^(?:[\w./~$-]+/)?agentsview$")
_OK_REPLY_RE = re.compile(r"""^["'`\s]*OK[.!]?["'`\s]*$""")


# ------------------------------------------------------------------ subprocesses


def resolve_binary(name: str, which: WhichFn | None = None) -> str | None:
    found = (which or shutil.which)(name)
    if found:
        return found
    if which is not None:
        return None
    home = Path.home()
    for directory in (
        home / ".local" / "bin",
        home / ".opencode" / "bin",
        home / ".local" / "share" / "hermes-cli.noindex" / "bin",
    ):
        candidate = directory / name
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _drain_stream(
    selector: selectors.BaseSelector,
    stream: Any,
    buf: bytearray,
) -> None:
    try:
        chunk = os.read(stream.fileno(), 65536)
    except OSError:
        chunk = b""
    if not chunk:
        with contextlib.suppress(KeyError, ValueError):
            selector.unregister(stream)
        return
    if len(buf) < MAX_STREAM_BYTES:
        take = min(len(chunk), MAX_STREAM_BYTES - len(buf))
        buf.extend(chunk[:take])


def execute(
    args: Sequence[str], env: Mapping[str, str], timeout: float
) -> EnvSmokeCommandResult:
    """Run one prompt command with bounded stream reads, stdin=/dev/null, and group kill."""
    start = monotonic()
    timed_out = False
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    exit_code: int | None = None
    with tempfile.TemporaryDirectory(prefix="env-smoke-run-") as scratch:
        try:
            process = subprocess.Popen(
                list(args),
                cwd=scratch,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return EnvSmokeCommandResult(
                exit_code=None,
                stdout="",
                stderr=quota.sanitize_diagnostic(str(exc)),
                duration_ms=int((monotonic() - start) * 1000),
                timed_out=False,
            )
        assert process.stdout is not None
        assert process.stderr is not None
        deadline = start + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, data=stdout_buf)
                selector.register(process.stderr, selectors.EVENT_READ, data=stderr_buf)
                while selector.get_map():
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    events = selector.select(remaining)
                    if not events:
                        timed_out = True
                        break
                    for key, _ in events:
                        _drain_stream(selector, key.fileobj, key.data)
            if timed_out:
                quota.killpg(process)
                # Bounded post-kill drain avoids hanging if a detached descendant keeps a pipe FD.
                drain_end = monotonic() + DRAIN_TIMEOUT_S
                with selectors.DefaultSelector() as selector:
                    for stream, buf in (
                        (process.stdout, stdout_buf),
                        (process.stderr, stderr_buf),
                    ):
                        selector.register(stream, selectors.EVENT_READ, data=buf)
                    while selector.get_map() and (rem := drain_end - monotonic()) > 0:
                        events = selector.select(rem)
                        if not events:
                            break
                        for key, _ in events:
                            _drain_stream(selector, key.fileobj, key.data)
            else:
                exit_code = process.wait(timeout=max(0.5, deadline - monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            quota.killpg(process)
        finally:
            for stream in (process.stdout, process.stderr):
                with contextlib.suppress(OSError):
                    stream.close()
            if process.poll() is None:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    quota.killpg(process)
            exit_code = process.poll()
    return EnvSmokeCommandResult(
        exit_code=exit_code,
        stdout=bytes(stdout_buf).decode("utf-8", errors="replace"),
        stderr=bytes(stderr_buf).decode("utf-8", errors="replace"),
        duration_ms=int((monotonic() - start) * 1000),
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------- commands


def build_command(
    harness: EnvSmokeHarness,
    binary: str,
    model: str,
    *,
    skill_check: bool,
    timeout: float,
) -> list[str]:
    prompt = SKILL_PROMPT if skill_check else SMOKE_PROMPT
    match harness:
        case EnvSmokeHarness.CLAUDE:
            cmd = [binary, "-p", prompt, "--model", model, "--effort", "low"]
            cmd += ["--no-session-persistence"]
            if skill_check:
                return [
                    *cmd,
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--allowedTools",
                    "Bash(agentsview:*),Skill",
                ]
            return [*cmd, "--output-format", "text", "--strict-mcp-config"]
        case EnvSmokeHarness.CODEX:
            cmd = [binary, "exec"]
            if skill_check:
                cmd.append("--json")
            cmd += [
                "-m",
                model,
                "-c",
                'model_reasoning_effort="low"',
                "--ephemeral",
            ]
            if skill_check:
                cmd += [
                    "-s",
                    "workspace-write",
                    "-c",
                    "sandbox_workspace_write.network_access=true",
                ]
            else:
                cmd += ["-s", "read-only"]
            return [*cmd, "--skip-git-repo-check", prompt]
        case EnvSmokeHarness.AGY:
            cmd = [
                binary,
                "--model",
                model,
                "--effort",
                "low",
                "--output-format",
                "stream-json" if skill_check else "json",
                "--print-timeout",
                f"{max(1, int(timeout))}s" if skill_check else "60s",
                "--log-file",
                "/dev/null",
            ]
            if skill_check:
                cmd.append("--dangerously-skip-permissions")
            return [*cmd, f"--print={prompt}"]
        case EnvSmokeHarness.OPENCODE:
            cmd = [
                binary,
                "--standalone",
                "run",
                "--archived",
                "--format",
                "json",
            ]
            if skill_check:
                cmd.append("--auto")
            return [*cmd, "-m", model, prompt]
        case EnvSmokeHarness.ZCODE:
            return [
                binary,
                "--prompt",
                prompt,
                "--model",
                model,
                "--effort",
                "low",
                "--json",
            ]
        case EnvSmokeHarness.HERMES:
            cmd = [
                binary,
                "chat",
                "-q",
                prompt,
                "-m",
                model,
                "--provider",
                "nous",
                "--reasoning",
                "low" if skill_check else "none",
                "--max-turns",
                "16" if skill_check else "2",
                "--format",
                "stream-json",
                "--source",
                "tool",
                "--yolo",
            ]
            if skill_check:
                cmd.extend(["-t", "terminal,skills"])
            return cmd


# -------------------------------------------------------------------- analysis


def classify_failure(
    harness: EnvSmokeHarness, text: str
) -> tuple[EnvSmokeStatus, str] | None:
    """Map provider quota/rate-limit failures to SKIP_* statuses post-hoc."""
    if harness == EnvSmokeHarness.ZCODE and (m := _ZCODE_QUOTA_RE.search(text)):
        return EnvSmokeStatus.SKIP_QUOTA, f"zcode quota error ({m.group(0)})"
    if harness in {EnvSmokeHarness.OPENCODE, EnvSmokeHarness.HERMES} and (
        m := _RATE_LIMIT_RE.search(text)
    ):
        return EnvSmokeStatus.SKIP_RATE_LIMIT, f"rate limited ({m.group(0)})"
    return None


def _json_documents(text: str) -> list[Any]:
    docs: list[Any] = []
    stripped = text.strip()
    if stripped:
        try:
            docs.append(json.loads(stripped))
            return docs
        except ValueError:
            pass
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "[{":
            continue
        try:
            docs.append(json.loads(line))
        except ValueError:
            continue
    return docs


def _command_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return " ".join(value)
    return None


def _read_heredoc_delim(text: str, start: int) -> tuple[str | None, bool, int]:
    """Parse `<<DELIM` / `<<-DELIM` word starting at `<<`; return `(delim, strip_tabs, end_idx)`."""
    n = len(text)
    strip_tabs = text.startswith("<<-", start)
    j = start + (3 if strip_tabs else 2)
    while j < n and text[j] in " \t":
        j += 1
    delim_buf: list[str] = []
    dquote: str | None = None
    descaped = False
    saw_token = False
    while j < n:
        c = text[j]
        if descaped:
            delim_buf.append(c)
            descaped = False
            saw_token = True
            j += 1
            continue
        if dquote == "'":
            if c == "'":
                dquote = None
            else:
                delim_buf.append(c)
            saw_token = True
            j += 1
            continue
        if dquote == '"':
            if c == "\\":
                descaped = True
            elif c == '"':
                dquote = None
            else:
                delim_buf.append(c)
            saw_token = True
            j += 1
            continue
        if c == "\\":
            descaped = True
            saw_token = True
            j += 1
            continue
        if c in {"'", '"'}:
            dquote = c
            saw_token = True
            j += 1
            continue
        if c in " \t\n\r;|&<>()":
            break
        delim_buf.append(c)
        saw_token = True
        j += 1
    return ("".join(delim_buf) if saw_token else None), strip_tabs, j


def _consume_heredoc_bodies(
    text: str, start: int, heredocs: list[tuple[str, bool]]
) -> int:
    pos = start
    n = len(text)
    for delim, strip_tabs in heredocs:
        while pos < n:
            nl = text.find("\n", pos)
            line = text[pos:] if nl == -1 else text[pos:nl]
            if strip_tabs:
                line = line.lstrip("\t")
            pos = n if nl == -1 else nl + 1
            if line == delim:
                break
    heredocs.clear()
    return pos


def _scan_shell_segments(
    text: str,
    seps: tuple[str, ...],
    *,
    preserve_tokens: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    """Split shell text outside quotes/comments/heredocs, returning (segment, sep) pairs."""
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    word_boundary = True
    last_unquoted_op = ""
    arith_depth = 0
    pending_heredocs: list[tuple[str, bool]] = []
    ordered_seps = tuple(sorted(seps, key=len, reverse=True))
    ordered_preserve = tuple(sorted(preserve_tokens, key=len, reverse=True))
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if escaped:
            buf.append(ch)
            escaped = False
            word_boundary = False
            last_unquoted_op = ""
            i += 1
            continue
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
            word_boundary = False
            last_unquoted_op = ""
            i += 1
            continue
        if quote == '"':
            if ch == "\\":
                if i + 1 < n and text[i + 1] == "\n":
                    i += 2
                    continue
                escaped = True
            elif ch == '"':
                quote = None
            buf.append(ch)
            word_boundary = False
            last_unquoted_op = ""
            i += 1
            continue
        if ch == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            escaped = True
            buf.append(ch)
            word_boundary = False
            last_unquoted_op = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            word_boundary = False
            last_unquoted_op = ""
            i += 1
            continue
        if ch == "#" and word_boundary:
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("$((", i):
            arith_depth += 1
            buf.append("$((")
            word_boundary = False
            last_unquoted_op = ""
            i += 3
            continue
        if arith_depth > 0 and text.startswith("))", i):
            arith_depth -= 1
            buf.append("))")
            word_boundary = False
            last_unquoted_op = ""
            i += 2
            continue
        if (
            arith_depth == 0
            and text.startswith("<<", i)
            and not text.startswith("<<<", i)
        ):
            delim, strip_tabs, end_idx = _read_heredoc_delim(text, i)
            if delim is not None:
                pending_heredocs.append((delim, strip_tabs))
            buf.append(text[i:end_idx])
            word_boundary = False
            last_unquoted_op = ""
            i = end_idx
            continue
        preserved = next((t for t in ordered_preserve if text.startswith(t, i)), None)
        if preserved is not None:
            buf.append(preserved)
            word_boundary = preserved in {"&&", "||", "|&"}
            last_unquoted_op = preserved if preserved in {"&&", "||", "|&"} else ""
            i += len(preserved)
            continue
        matched = next((s for s in ordered_seps if text.startswith(s, i)), None)
        if matched is not None:
            if matched == "\n" and last_unquoted_op and not pending_heredocs:
                buf.append(" ")
                word_boundary = True
                i += 1
                continue
            parts.append(("".join(buf), matched))
            buf.clear()
            word_boundary = True
            last_unquoted_op = ""
            if matched == "\n" and pending_heredocs:
                i = _consume_heredoc_bodies(text, i + 1, pending_heredocs)
            else:
                i += len(matched)
            continue
        buf.append(ch)
        word_boundary = ch in " \t\n\r;|&()"
        if ch == "|":
            last_unquoted_op = "|"
        elif ch not in " \t":
            last_unquoted_op = ""
        i += 1
    parts.append(("".join(buf), ""))
    return parts


def _split_shell_outside_quotes(text: str, seps: tuple[str, ...]) -> list[str]:
    """Split shell text on unquoted separator tokens (longest match first)."""
    return [seg for seg, _ in _scan_shell_segments(text, seps)]


def _split_shell_statements(text: str) -> list[tuple[str, bool]]:
    """Return non-empty `(statement, backgrounded)` pairs ignoring redirections/logical ops."""
    return [
        (seg.strip(), sep == "&")
        for seg, sep in _scan_shell_segments(
            text,
            (";", "\n", "&"),
            preserve_tokens=("&&", "||", "|&", "<<<", "<&", ">&", "&>", ">|"),
        )
        if seg.strip()
    ]


def _consume_env_prefix(tokens: Sequence[str], idx: int) -> int:
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--":
            return idx + 1
        if tok in {"-u", "--unset", "-C", "--chdir", "-P", "-a", "--argv0"}:
            idx += 2
            continue
        if tok in {"-i", "--ignore-environment", "-0", "--null", "-v", "--debug"}:
            idx += 1
            continue
        if tok.startswith(("--unset=", "--chdir=", "--argv0=")):
            idx += 1
            continue
        if tok.startswith("-") and not tok.startswith("--"):
            pos = 1
            valid = True
            while pos < len(tok):
                ch = tok[pos]
                if ch in {"i", "0", "v"}:
                    pos += 1
                    continue
                if ch in {"u", "C", "P", "a"}:
                    if pos + 1 == len(tok):
                        idx += 1
                    break
                valid = False
                break
            if not valid or len(tok) == 1:
                return len(tokens)
            idx += 1
            continue
        if tok.startswith("-"):
            return len(tokens)
        if "=" in tok:
            idx += 1
            continue
        return idx
    return idx


_PIPE_FILTER_BINS = frozenset(
    {
        "head",
        "tail",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "jq",
        "sed",
        "awk",
        "gawk",
        "cat",
        "cut",
        "sort",
        "uniq",
        "wc",
        "tr",
        "tee",
        "column",
        "less",
        "more",
    }
)


def _stage_head_name(seg: str) -> str | None:
    stripped = seg.strip()
    if not stripped:
        return None
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    idx = 0
    while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
        idx += 1
    return Path(tokens[idx]).name if idx < len(tokens) else None


def _enables_pipefail(stmt: str) -> bool:
    tokens = stmt.split()
    if not tokens or Path(tokens[0]).name != "set":
        return False
    for idx, tok in enumerate(tokens[1:], start=1):
        if tok == "-o" and idx + 1 < len(tokens) and tokens[idx + 1] == "pipefail":
            return True
        if tok == "-opipefail":
            return True
    return False


def _single_segment_is_agentsview(seg: str) -> bool:
    stripped = seg.strip()
    if not stripped:
        return False
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    idx = 0
    while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
        idx += 1
    if idx < len(tokens) and Path(tokens[idx]).name == "env":
        idx = _consume_env_prefix(tokens, idx + 1)
    if idx >= len(tokens):
        return False
    head = tokens[idx]
    if _AGENTSVIEW_BIN_RE.match(head):
        return True
    if Path(head).name in {"sh", "bash", "zsh", "dash"}:
        j = idx + 1
        while j < len(tokens):
            tok = tokens[j]
            if tok == "--" or tok == "-" or not tok.startswith("-"):
                break
            if tok in {"-o", "+o"}:
                if j + 1 < len(tokens) and tokens[j + 1] == "noexec":
                    return False
                j += 2
                continue
            if tok in {"--noexec", "--help", "--version", "--dump-strings"}:
                return False
            if tok.startswith("--"):
                if tok in {
                    "--norc",
                    "--noprofile",
                    "--login",
                    "--posix",
                    "--restricted",
                }:
                    j += 1
                    continue
                return False
            if "n" in tok[1:]:
                return False
            if "c" in tok[1:]:
                return j + 1 < len(tokens) and _is_agentsview_exec(tokens[j + 1])
            j += 1
    return False


def _pipeline_runs_agentsview(pipeline: str, *, pipefail: bool = False) -> bool:
    stages = _split_shell_outside_quotes(pipeline, ("|&", "|"))
    av_indices = [
        i for i, stage in enumerate(stages) if _single_segment_is_agentsview(stage)
    ]
    if not av_indices:
        return False
    last_av = av_indices[-1]
    if pipefail or last_av == len(stages) - 1:
        return True
    return all(
        (_stage_head_name(st) or "") in _PIPE_FILTER_BINS
        for st in stages[last_av + 1 :]
    )


def _list_runs_agentsview(stmt: str, *, pipefail: bool = False) -> bool:
    """Evaluate POSIX left-to-right equal-precedence `&&`/`||` command list."""
    terms = _scan_shell_segments(stmt, ("&&", "||"))
    if not terms or any(not term.strip() for term, _ in terms):
        return False
    acc_with = _pipeline_runs_agentsview(terms[0][0], pipefail=pipefail)
    acc_without = not acc_with
    prev_op = terms[0][1]
    for term, next_op in terms[1:]:
        rhs_with = _pipeline_runs_agentsview(term, pipefail=pipefail)
        rhs_without = not rhs_with
        if prev_op == "&&":
            acc_with = acc_with or rhs_with
            acc_without = acc_without and rhs_without
        else:
            acc_with = acc_with or rhs_with
            acc_without = acc_without or rhs_without
        prev_op = next_op
    return acc_with and not acc_without


def _is_agentsview_exec(cmd_text: str) -> bool:
    """Return True only when executed foreground shell command runs `agentsview` without masking."""
    stmts = _split_shell_statements(cmd_text.strip())
    if not stmts:
        return False
    last_stmt, is_bg = stmts[-1]
    if is_bg:
        return False
    pipefail = any(_enables_pipefail(s) for s, bg in stmts[:-1] if not bg)
    return _list_runs_agentsview(last_stmt, pipefail=pipefail)


def _tool_node_name(node: Mapping[str, Any]) -> str:
    return str(
        node.get("name")
        or node.get("tool")
        or node.get("tool_name")
        or node.get("toolName")
        or ""
    ).lower()


def _explicit_failure_on_result(node: Mapping[str, Any]) -> bool:
    if node.get("is_error") is True:
        return True
    if "exit_code" in node:
        ec = node.get("exit_code")
        if not (isinstance(ec, int) and not isinstance(ec, bool) and ec == 0):
            return True
    if "status" in node and node.get("status") is not None:
        st = node.get("status")
        if not (
            isinstance(st, str) and st.lower() in {"success", "ok", "completed", "done"}
        ):
            return True
    err = node.get("error")
    if isinstance(err, str):
        if err.strip():
            return True
    elif err:
        return True
    state = node.get("state")
    if isinstance(state, str) and state.lower() not in {
        "success",
        "ok",
        "completed",
        "done",
    }:
        return True
    if isinstance(state, Mapping) and _explicit_failure_on_result(state):
        return True
    meta = node.get("metadata")
    if isinstance(meta, Mapping) and "exit" in meta:
        mex = meta.get("exit")
        if not (isinstance(mex, int) and not isinstance(mex, bool) and mex == 0):
            return True
    return False


def _tool_result_outcome(
    node: Mapping[str, Any], *, expected_name: str = ""
) -> tuple[str, Mapping[str, Any] | None]:
    if _explicit_failure_on_result(node):
        return "fail", None
    raw_out = node.get("output")
    rname = _tool_node_name(node) or expected_name.lower()
    expects_json = rname in {"terminal", "execute_code"} or (
        isinstance(raw_out, str) and raw_out.lstrip().startswith("{")
    )
    if not expects_json:
        return "pass", None
    if not isinstance(raw_out, str):
        return "indeterminate", None
    try:
        parsed = json.loads(raw_out)
    except ValueError:
        return "indeterminate", None
    if not isinstance(parsed, Mapping):
        return "indeterminate", None
    if _explicit_failure_on_result(parsed):
        return "fail", parsed
    st = parsed.get("status")
    if (rname == "execute_code" or st is not None) and (
        not isinstance(st, str) or st.lower() != "success"
    ):
        return "fail", parsed
    if rname in {"terminal", "execute_code"} and "exit_code" not in parsed:
        return "indeterminate", parsed
    return "pass", parsed


def _tool_node_id(node: Mapping[str, Any]) -> str | None:
    tid = (
        node.get("tool_use_id")
        or node.get("tool_call_id")
        or node.get("call_id")
        or node.get("id")
    )
    return tid if isinstance(tid, str) and tid else None


def _event_discriminator(node: Mapping[str, Any]) -> str:
    return str(
        node.get("type") or node.get("kind") or node.get("step_type") or ""
    ).lower()


def _analyze_tool_outcomes(
    docs: Sequence[Any],
) -> tuple[set[int], set[str], dict[int, Mapping[str, Any] | None]]:
    """Pair tool_use/tool_call/function_call nodes with tool_result events by ID or unambiguous name."""
    failed_obj_ids: set[int] = set()
    failed_str_ids: set[str] = set()
    passed_obj_ids: dict[int, Mapping[str, Any] | None] = {}
    pending_by_id: dict[str, list[dict[str, Any]]] = {}
    pending_by_name: dict[str, list[dict[str, Any]]] = {}

    def _drop_pending(target: dict[str, Any]) -> None:
        for bucket in list(pending_by_id.values()):
            if target in bucket:
                bucket.remove(target)
        for bucket in list(pending_by_name.values()):
            if target in bucket:
                bucket.remove(target)

    def _record(
        target: dict[str, Any], verdict: str, parsed: Mapping[str, Any] | None
    ) -> None:
        _drop_pending(target)
        if verdict == "fail":
            failed_obj_ids.add(id(target))
        elif verdict == "pass":
            passed_obj_ids[id(target)] = parsed

    def scan(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                scan(item)
            return
        if not isinstance(node, dict):
            return
        ntype = _event_discriminator(node)
        if ntype in {"tool_use", "tool_call", "function_call"}:
            tid = _tool_node_id(node)
            name = _tool_node_name(node)
            if tid:
                pending_by_id.setdefault(tid, []).append(node)
            if name:
                pending_by_name.setdefault(name, []).append(node)
        elif ntype == "tool_result":
            tid = _tool_node_id(node)
            rname = _tool_node_name(node)
            if tid and pending_by_id.get(tid):
                targets = list(pending_by_id[tid])
                if len(targets) == 1:
                    target = targets[0]
                    tname = _tool_node_name(target)
                    if rname and tname and rname != tname:
                        failed_str_ids.add(tid)
                        _record(target, "fail", None)
                    else:
                        verdict, parsed = _tool_result_outcome(
                            node, expected_name=tname
                        )
                        if verdict == "fail":
                            failed_str_ids.add(tid)
                        _record(target, verdict, parsed)
                else:
                    # Ambiguous duplicate correlation IDs: withhold credit from all pending.
                    failed_str_ids.add(tid)
                    for target in targets:
                        _record(target, "fail", None)
            elif not tid and rname and pending_by_name.get(rname):
                candidates = list(pending_by_name[rname])
                if len(candidates) == 1:
                    verdict, parsed = _tool_result_outcome(node, expected_name=rname)
                    _record(candidates[0], verdict, parsed)
                else:
                    # Ambiguous parallel calls without IDs: withhold credit from all pending.
                    for target in candidates:
                        _record(target, "fail", None)
            else:
                verdict, _ = _tool_result_outcome(node)
                if verdict == "fail" and tid:
                    failed_str_ids.add(tid)
            return
        for key, val in node.items():
            if key in _RESULT_PAYLOAD_KEYS:
                continue
            if isinstance(val, (dict, list)):
                scan(val)

    for d in docs:
        scan(d)
    return failed_obj_ids, failed_str_ids, passed_obj_ids


def _agy_tool_exit_code(tool_info: Any) -> int | None:
    if not isinstance(tool_info, Mapping):
        return None
    out = tool_info.get("output")
    if not isinstance(out, str):
        return None
    header = re.split(r"(?:^|\n+)Command output:", out, maxsplit=1)[0]
    exit_lines = [ln.strip() for ln in header.splitlines() if "Command exited" in ln]
    if not exit_lines:
        return None
    if len(exit_lines) == 1:
        match = _AGY_EXIT_RE.fullmatch(exit_lines[0])
        return int(match.group(1)) if match else -1
    for line in exit_lines:
        match = _AGY_EXIT_RE.fullmatch(line)
        if match is None:
            return -1
        code = int(match.group(1))
        if code != 0:
            return code
    return -1


def _invocation_failed(
    node: Mapping[str, Any],
    failed_obj_ids: set[int],
    failed_str_ids: set[str],
) -> bool:
    if id(node) in failed_obj_ids:
        return True
    tid = _tool_node_id(node)
    if tid and tid in failed_str_ids:
        return True
    if node.get("is_error") is True:
        return True
    ec = node.get("exit_code")
    if isinstance(ec, int) and not isinstance(ec, bool) and ec != 0:
        return True
    st = node.get("status")
    if isinstance(st, str) and st.lower() in {
        "error",
        "failed",
        "cancelled",
        "timeout",
    }:
        return True
    err = node.get("error")
    if isinstance(err, str):
        if err.strip():
            return True
    elif err:
        return True
    meta = node.get("metadata")
    if isinstance(meta, Mapping) and "exit" in meta:
        mex = meta.get("exit")
        if not (isinstance(mex, int) and not isinstance(mex, bool) and mex == 0):
            return True
    state = node.get("state")
    if isinstance(state, str) and state.lower() in {"error", "failed", "cancelled"}:
        return True
    tool_info = node.get("tool_info")
    if isinstance(tool_info, Mapping):
        agy_ec = _agy_tool_exit_code(tool_info)
        if agy_ec is not None and agy_ec != 0:
            return True
    return isinstance(state, Mapping) and _invocation_failed(
        state, failed_obj_ids, failed_str_ids
    )


def _mcp_name(node: Mapping[str, Any]) -> str | None:
    if isinstance(node.get("server"), str) and isinstance(node.get("tool"), str):
        return f"{node['server']}/{node['tool']}"
    for key in ("name", "tool", "tool_name", "toolName"):
        val = node.get(key)
        if isinstance(val, str):
            low = val.lower()
            if low.startswith(("mcp__", "mcp.", "mcp:")):
                return val
            if low in _MCP_TOOL_NAMES:
                ti = node.get("tool_info")
                params = ti.get("parameters") if isinstance(ti, Mapping) else None
                if isinstance(params, Mapping):
                    srv = params.get("ServerName") or params.get("server")
                    tn = params.get("ToolName") or params.get("tool")
                    if isinstance(srv, str) and isinstance(tn, str):
                        return f"{srv}/{tn}"
                return val
    return None


def _is_invocation_node(node: Mapping[str, Any]) -> bool:
    if _event_discriminator(node) in _TOOL_EVENT_KINDS:
        return True
    return isinstance(node.get("tool"), str) and isinstance(node.get("state"), Mapping)


def _walk_invocations(
    node: Any,
    cli: list[str],
    mcp: list[str],
    *,
    failed_obj_ids: set[int],
    failed_str_ids: set[str],
    passed_obj_ids: Mapping[int, Mapping[str, Any] | None],
    in_progress: bool = False,
) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_invocations(
                item,
                cli,
                mcp,
                failed_obj_ids=failed_obj_ids,
                failed_str_ids=failed_str_ids,
                passed_obj_ids=passed_obj_ids,
                in_progress=in_progress,
            )
        return
    if not isinstance(node, dict):
        return
    kind_val = _event_discriminator(node)
    if kind_val == "tool_result":
        return
    if _is_invocation_node(node):
        mcp_label = _mcp_name(node)
        if mcp_label or kind_val in _MCP_KINDS:
            label = mcp_label or str(
                node.get("name")
                or node.get("tool")
                or node.get("tool_name")
                or kind_val
            )
            if label not in mcp:
                mcp.append(label)
        else:
            step_type = str(node.get("step_type") or "").lower()
            step_state = (
                str(node.get("state") or "").upper()
                if isinstance(node.get("state"), str)
                else ""
            )
            # AGY step_update tool frames must reach DONE with exit code 0 before CLI credit.
            agy_incomplete = step_type == "tool" and (
                step_state != "DONE" or _agy_tool_exit_code(node.get("tool_info")) != 0
            )
            # Codex command_execution items must be completed with explicit exit_code == 0.
            codex_ec = node.get("exit_code")
            codex_incomplete = kind_val == "command_execution" and (
                in_progress
                or (
                    "status" in node
                    and str(node.get("status") or "").lower() != "completed"
                )
                or not (
                    isinstance(codex_ec, int)
                    and not isinstance(codex_ec, bool)
                    and codex_ec == 0
                )
            )
            name_val = str(
                node.get("name")
                or node.get("tool")
                or node.get("tool_name")
                or node.get("toolName")
                or kind_val
            ).lower()
            opencode_state = node.get("state")
            opencode_incomplete = (
                isinstance(node.get("tool"), str)
                and isinstance(opencode_state, Mapping)
                and (
                    str(opencode_state.get("status") or "").lower() != "completed"
                    or _explicit_failure_on_result(opencode_state)
                )
            )
            is_shell = name_val in _SHELL_TOOL_NAMES or kind_val == "command_execution"
            requires_paired = is_shell and kind_val in {
                "tool_use",
                "tool_call",
                "function_call",
            }
            missing_paired = requires_paired and id(node) not in passed_obj_ids
            if (
                not agy_incomplete
                and not codex_incomplete
                and not opencode_incomplete
                and not missing_paired
                and not _invocation_failed(node, failed_obj_ids, failed_str_ids)
                and is_shell
            ):
                tool_info = node.get("tool_info")
                ti_params = (
                    tool_info.get("parameters")
                    if isinstance(tool_info, Mapping)
                    else None
                )
                for src in (
                    node,
                    node.get("input"),
                    node.get("state"),
                    tool_info,
                    ti_params,
                ):
                    if not isinstance(src, Mapping):
                        continue
                    for ck in _CMD_KEYS:
                        text = _command_text(src.get(ck))
                        if text and _is_agentsview_exec(text):
                            clean = quota.sanitize_diagnostic(text)
                            if clean not in cli:
                                cli.append(clean)
                    state_in = src.get("input")
                    if isinstance(state_in, Mapping):
                        for ck in _CMD_KEYS:
                            text = _command_text(state_in.get(ck))
                            if text and _is_agentsview_exec(text):
                                clean = quota.sanitize_diagnostic(text)
                                if clean not in cli:
                                    cli.append(clean)
                for ak in ("arguments", "args"):
                    raw_args = node.get(ak)
                    if isinstance(raw_args, str):
                        with contextlib.suppress(ValueError):
                            parsed = json.loads(raw_args)
                            if isinstance(parsed, Mapping):
                                for ck in _CMD_KEYS:
                                    text = _command_text(parsed.get(ck))
                                    if text and _is_agentsview_exec(text):
                                        clean = quota.sanitize_diagnostic(text)
                                        if clean not in cli:
                                            cli.append(clean)
    parent_type = str(node.get("type") or "").lower()
    child_in_progress = in_progress or parent_type in {"item.started", "item.updated"}
    for key, value in node.items():
        if key in _RESULT_PAYLOAD_KEYS:
            continue
        if isinstance(value, (dict, list)):
            _walk_invocations(
                value,
                cli,
                mcp,
                failed_obj_ids=failed_obj_ids,
                failed_str_ids=failed_str_ids,
                passed_obj_ids=passed_obj_ids,
                in_progress=child_in_progress,
            )


def _utf8_len_or_none(text: str) -> int | None:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _is_hermes_capped_output(raw_out: Any) -> bool:
    if not isinstance(raw_out, str) or not raw_out.endswith("...") or len(raw_out) <= 3:
        return False
    prefix = raw_out[:-3]
    if len(prefix) == _HERMES_CAP_THRESHOLD:
        return True
    utf8_len = _utf8_len_or_none(prefix)
    return (
        utf8_len is not None
        and _HERMES_CAP_THRESHOLD - 3 <= utf8_len <= _HERMES_CAP_THRESHOLD
        and len(prefix) < _HERMES_CAP_THRESHOLD
    )


def _matches_hermes_cap_rule(content: str, prefix: str) -> bool:
    if not content.startswith(prefix) or len(content) <= len(prefix):
        return False
    if len(prefix) == _HERMES_CAP_THRESHOLD:
        return True
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        len(content_bytes) > _HERMES_CAP_THRESHOLD
        and content_bytes[:_HERMES_CAP_THRESHOLD].decode("utf-8", errors="ignore")
        == prefix
    )


def _rehydrate_hermes_capped_output(
    stdout: str, *, home: Path | None = None
) -> str | None:
    """Resolve Hermes capped StreamJsonEmitter terminal results from state.db."""
    docs = _json_documents(stdout)
    has_capped = any(
        isinstance(d, Mapping)
        and str(d.get("type") or "").lower() == "tool_result"
        and _tool_node_name(d) == "terminal"
        and _is_hermes_capped_output(d.get("output"))
        for d in docs
    )
    if not has_capped:
        return None
    session_id = ""
    for d in reversed(docs):
        if (
            isinstance(d, Mapping)
            and str(d.get("type") or "").lower() == "result"
            and isinstance(d.get("session_id"), str)
            and d["session_id"]
        ):
            session_id = d["session_id"]
            break
    if not session_id or _utf8_len_or_none(session_id) is None:
        return None
    db_path = (home or Path.home()) / ".hermes" / "state.db"
    if not db_path.is_file():
        return None
    rows: list[tuple[str | None, str | None]] = []
    with (
        contextlib.suppress(sqlite3.Error, OSError, UnicodeError),
        contextlib.closing(
            sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        ) as conn,
    ):
        rows = conn.execute(
            "SELECT tool_call_id, content FROM messages "
            "WHERE session_id = ? AND role = 'tool' AND tool_name = 'terminal' "
            "ORDER BY id",
            (session_id,),
        ).fetchall()
    if not rows:
        return None
    by_id: dict[str, list[str | None]] = {}
    ordered: list[str] = []
    for tcid, content in rows:
        if isinstance(content, str):
            ordered.append(content)
        if isinstance(tcid, str) and tcid:
            by_id.setdefault(tcid, []).append(
                content if isinstance(content, str) else None
            )
    updated = False
    out_docs: list[Any] = []
    for d in docs:
        if (
            isinstance(d, dict)
            and str(d.get("type") or "").lower() == "tool_result"
            and _tool_node_name(d) == "terminal"
        ):
            raw_out = d.get("output")
            if _is_hermes_capped_output(raw_out) and isinstance(raw_out, str):
                capped_prefix = raw_out[:-3]
                tcid = _tool_node_id(d)
                if tcid:
                    id_rows = by_id.get(tcid, ())
                    if (
                        len(id_rows) == 1
                        and isinstance(id_rows[0], str)
                        and _matches_hermes_cap_rule(id_rows[0], capped_prefix)
                    ):
                        d = {**d, "output": id_rows[0]}
                        updated = True
                else:
                    matches = [
                        c for c in ordered if _matches_hermes_cap_rule(c, capped_prefix)
                    ]
                    if len(matches) == 1:
                        d = {**d, "output": matches[0]}
                        updated = True
        out_docs.append(d)
    return "\n".join(json.dumps(item) for item in out_docs) if updated else None


def analyze_skill_output(text: str) -> EnvSmokeSkillCheck:
    """PASS only when a shell tool call ran the `agentsview` CLI and zero MCP calls occurred."""
    cli: list[str] = []
    mcp: list[str] = []
    docs = _json_documents(text)
    failed_obj_ids, failed_str_ids, passed_obj_ids = _analyze_tool_outcomes(docs)
    for doc in docs:
        _walk_invocations(
            doc,
            cli,
            mcp,
            failed_obj_ids=failed_obj_ids,
            failed_str_ids=failed_str_ids,
            passed_obj_ids=passed_obj_ids,
        )
    run_failed = _any_result_doc_failed(docs)
    if mcp:
        verdict = EnvSmokeSkillVerdict.FAIL_MCP_USED
    elif cli and not run_failed:
        verdict = EnvSmokeSkillVerdict.PASS
    else:
        verdict = EnvSmokeSkillVerdict.FAIL_NO_CLI
    return EnvSmokeSkillCheck(verdict=verdict, cli_calls=cli, mcp_calls=mcp)


def _excerpt(text: str) -> str | None:
    text = quota.sanitize_diagnostic(text.strip())
    if not text:
        return None
    return text if len(text) <= EXCERPT_CHARS else "…" + text[-EXCERPT_CHARS:]


def _result_doc_failed(doc: Mapping[str, Any]) -> bool:
    kind = str(doc.get("type") or doc.get("event") or "").lower()
    if kind in {"error", "turn.failed"}:
        return True
    if kind == "result":
        if doc.get("is_error") is True:
            return True
        sub = doc.get("subtype")
        if isinstance(sub, str) and sub.lower().startswith("error"):
            return True
        if _explicit_failure_on_result(doc):
            return True
        res = doc.get("result")
        if isinstance(res, Mapping) and _explicit_failure_on_result(res):
            return True
    return False


def _any_result_doc_failed(node: Any) -> bool:
    if isinstance(node, list):
        return any(_any_result_doc_failed(item) for item in node)
    return isinstance(node, Mapping) and _result_doc_failed(node)


def _extract_text_from_docs(docs: Sequence[Any]) -> str | None:
    texts: list[str] = []
    for doc in docs:
        if not isinstance(doc, Mapping):
            continue
        if str(doc.get("role") or doc.get("type") or "").lower() == "user":
            continue
        if _result_doc_failed(doc):
            continue
        for key in ("result", "response", "text", "content"):
            val = doc.get(key)
            if isinstance(val, str) and val.strip():
                texts.append(val.strip())
                break
            if key == "result" and isinstance(val, Mapping):
                if _explicit_failure_on_result(val):
                    continue
                for subk in ("response", "text", "content"):
                    sval = val.get(subk)
                    if isinstance(sval, str) and sval.strip():
                        texts.append(sval.strip())
                        break
        part = doc.get("part")
        if isinstance(part, Mapping) and part.get("type") == "text":
            pt = part.get("text")
            if isinstance(pt, str) and pt.strip():
                texts.append(pt.strip())
        msg = doc.get("message")
        if isinstance(msg, str) and msg.strip():
            texts.append(msg.strip())
        elif isinstance(msg, Mapping):
            mc = msg.get("content")
            if isinstance(mc, str) and mc.strip():
                texts.append(mc.strip())
            elif isinstance(mc, list):
                for item in mc:
                    if isinstance(item, Mapping) and item.get("type") == "text":
                        t = item.get("text")
                        if isinstance(t, str) and t.strip():
                            texts.append(t.strip())
    return texts[-1] if texts else None


def _smoke_ok(stdout: str) -> bool:
    docs = _json_documents(stdout)
    if docs:
        if _any_result_doc_failed(docs):
            return False
        extracted = _extract_text_from_docs(docs)
        if extracted is None:
            return False
        candidate = extracted
    else:
        lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        if not lines:
            return False
        if SMOKE_PROMPT in lines and len(lines) == 1:
            return False
        candidate = lines[-1] if SMOKE_PROMPT in lines else stdout.strip()
    if candidate.strip() == SMOKE_PROMPT:
        return False
    return _OK_REPLY_RE.fullmatch(candidate) is not None


# ----------------------------------------------------------------------- probes


def default_probe(
    harness: EnvSmokeHarness, binary: str, now: datetime
) -> EnvSmokeQuotaReading:
    match harness:
        case EnvSmokeHarness.CLAUDE:
            return quota.probe_claude(binary, now=now)
        case EnvSmokeHarness.CODEX:
            return quota.probe_codex(binary, now=now)
        case EnvSmokeHarness.AGY:
            return quota.probe_agy(binary, now=now)
        case EnvSmokeHarness.OPENCODE:
            return quota.probe_opencode(now=now)
        case EnvSmokeHarness.ZCODE:
            return quota.probe_zcode(now=now)
        case EnvSmokeHarness.HERMES:
            return quota.probe_hermes(now=now)


def _harness_env(harness: EnvSmokeHarness, scratch: Path) -> dict[str, str]:
    if harness == EnvSmokeHarness.AGY:
        return quota.agy_env(scratch)
    return quota.clean_env()


# ---------------------------------------------------------------------- runner


def _outcome(
    harness: EnvSmokeHarness,
    status: EnvSmokeStatus,
    *,
    errors: Iterable[EnvSmokeErrorEntry] = (),
    returns: Iterable[EnvSmokeReturnEntry] = (),
    **fields: Any,
) -> EnvSmokeHarnessOutcome:
    ts, ts_ns = now_ns_pair()
    return EnvSmokeHarnessOutcome(
        harness=harness,
        status=status,
        timestamp=ts,
        timestamp_unix_ns=ts_ns,
        errors=list(errors),
        returns=list(returns),
        **fields,
    )


def smoke_harness(
    harness: EnvSmokeHarness,
    *,
    mode: EnvSmokeMode,
    timeout: float = DEFAULT_TIMEOUT_S,
    which: WhichFn | None = None,
    probe: ProbeFn | None = None,
    exec_fn: ExecFn | None = None,
    now: datetime | None = None,
) -> EnvSmokeHarnessOutcome:
    binary = resolve_binary(harness.value, which)
    if binary is None:
        return _outcome(
            harness,
            EnvSmokeStatus.SKIP_MISSING,
            reason=f"`{harness.value}` binary not found on PATH",
            returns=[EnvSmokeReturnEntry(code=EnvSmokeReturnCode.RET_SKIPPED)],
        )
    reading = (probe or default_probe)(harness, binary, now or quota.now_utc())
    errors: list[EnvSmokeErrorEntry] = []
    returns: list[EnvSmokeReturnEntry] = []
    if not reading.probe_ok:
        errors.append(
            EnvSmokeErrorEntry(
                code=EnvSmokeErrorCode.ERR_QUOTA_PROBE_FAILED,
                message=reading.reason or "quota probe failed",
            )
        )
    model = reading.model
    if reading.model and reading.model == quota.AGY_FALLBACK_MODEL:
        returns.append(
            EnvSmokeReturnEntry(
                code=EnvSmokeReturnCode.RET_MODEL_FALLBACK, message=reading.reason
            )
        )
    if not reading.available:
        reset = f" (reset={reading.reset})" if reading.reset else ""
        return _outcome(
            harness,
            EnvSmokeStatus.SKIP_QUOTA,
            binary=binary,
            model=model,
            quota=reading,
            reason=f"{reading.reason or 'quota unavailable'}{reset}",
            errors=errors,
            returns=[
                *returns,
                EnvSmokeReturnEntry(code=EnvSmokeReturnCode.RET_SKIPPED),
            ],
        )
    if mode == EnvSmokeMode.DRY_RUN:
        status = EnvSmokeStatus.OK if reading.probe_ok else EnvSmokeStatus.FAIL
        return _outcome(
            harness,
            status,
            binary=binary,
            model=model,
            quota=reading,
            reason=reading.reason,
            errors=errors,
            returns=[
                *returns,
                EnvSmokeReturnEntry(code=EnvSmokeReturnCode.RET_DRY_RUN),
            ],
        )
    smoke_model = model or {
        EnvSmokeHarness.CLAUDE: quota.CLAUDE_SMOKE_MODEL,
        EnvSmokeHarness.CODEX: quota.CODEX_SMOKE_MODEL,
        EnvSmokeHarness.AGY: quota.AGY_SMOKE_MODEL,
        EnvSmokeHarness.OPENCODE: quota.OPENCODE_SMOKE_MODEL,
        EnvSmokeHarness.ZCODE: quota.ZCODE_SMOKE_MODEL,
        EnvSmokeHarness.HERMES: quota.HERMES_FALLBACK_MODEL,
    }.get(harness, "")
    skill_check = mode == EnvSmokeMode.SKILL_CHECK
    command = build_command(
        harness, binary, smoke_model, skill_check=skill_check, timeout=timeout
    )
    with tempfile.TemporaryDirectory(prefix="env-smoke-env-") as scratch:
        result = (exec_fn or execute)(
            command, _harness_env(harness, Path(scratch)), timeout
        )
    combined = f"{result.stdout}\n{result.stderr}"
    common: dict[str, Any] = {
        "binary": binary,
        "model": smoke_model,
        "command": command,
        "quota": reading,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "output_excerpt": _excerpt(result.stdout or result.stderr),
    }
    if result.timed_out:
        errors.append(
            EnvSmokeErrorEntry(
                code=EnvSmokeErrorCode.ERR_TIMEOUT,
                message=f"timed out after {timeout}s",
            )
        )
        return _outcome(
            harness,
            EnvSmokeStatus.FAIL,
            reason=f"timed out after {timeout}s",
            errors=errors,
            returns=returns,
            **common,
        )
    if result.exit_code != 0:
        mapped = classify_failure(harness, combined)
        if mapped is not None:
            status, why = mapped
            return _outcome(
                harness,
                status,
                reason=why,
                errors=errors,
                returns=[
                    *returns,
                    EnvSmokeReturnEntry(code=EnvSmokeReturnCode.RET_SKIPPED),
                ],
                **common,
            )
        errors.append(
            EnvSmokeErrorEntry(
                code=EnvSmokeErrorCode.ERR_COMMAND_FAILED,
                message=f"exit code {result.exit_code}",
                detail={"stderr": _excerpt(result.stderr)},
            )
        )
        return _outcome(
            harness,
            EnvSmokeStatus.FAIL,
            reason=f"exit code {result.exit_code}",
            errors=errors,
            returns=returns,
            **common,
        )
    if skill_check:
        verdict = analyze_skill_output(result.stdout)
        if (
            harness == EnvSmokeHarness.HERMES
            and verdict.verdict == EnvSmokeSkillVerdict.FAIL_NO_CLI
            and (hydrated := _rehydrate_hermes_capped_output(result.stdout))
        ):
            verdict = analyze_skill_output(hydrated)
        if verdict.verdict != EnvSmokeSkillVerdict.PASS:
            errors.append(
                EnvSmokeErrorEntry(
                    code=EnvSmokeErrorCode.ERR_SKILL_CHECK_FAILED,
                    message=verdict.verdict.value,
                    detail={"mcp_calls": verdict.mcp_calls},
                )
            )
        ok = verdict.verdict == EnvSmokeSkillVerdict.PASS
        return _outcome(
            harness,
            EnvSmokeStatus.OK if ok else EnvSmokeStatus.FAIL,
            reason=f"skill-check {verdict.verdict.value}",
            skill_check=verdict,
            errors=errors,
            returns=[*returns, EnvSmokeReturnEntry(code=EnvSmokeReturnCode.RET_OK)]
            if ok
            else returns,
            **common,
        )
    if not _smoke_ok(result.stdout):
        errors.append(
            EnvSmokeErrorEntry(
                code=EnvSmokeErrorCode.ERR_COMMAND_FAILED,
                message="reply did not contain OK",
            )
        )
        return _outcome(
            harness,
            EnvSmokeStatus.FAIL,
            reason="reply did not contain OK",
            errors=errors,
            returns=returns,
            **common,
        )
    return _outcome(
        harness,
        EnvSmokeStatus.OK,
        reason="replied OK",
        errors=errors,
        returns=[*returns, EnvSmokeReturnEntry(code=EnvSmokeReturnCode.RET_OK)],
        **common,
    )


def parse_harnesses(values: Iterable[str] | None) -> list[EnvSmokeHarness]:
    """Parse repeatable and/or comma/space separated harness names (default all 6)."""
    if values is None:
        return list(ALL_HARNESSES)
    raw = list(values)
    if not raw:
        return list(ALL_HARNESSES)
    names: list[str] = []
    for value in raw:
        names.extend(n for n in re.split(r"[\s,]+", value) if n)
    if not names:
        raise ValueError("empty --harness selector")
    selected: list[EnvSmokeHarness] = []
    for name in names:
        harness = EnvSmokeHarness(name.strip().lower())
        if harness not in selected:
            selected.append(harness)
    return selected


def run_smoke(
    harnesses: Sequence[EnvSmokeHarness],
    *,
    mode: EnvSmokeMode,
    timeout: float = DEFAULT_TIMEOUT_S,
    which: WhichFn | None = None,
    probe: ProbeFn | None = None,
    exec_fn: ExecFn | None = None,
    now: datetime | None = None,
) -> EnvSmokeResponse:
    def one(harness: EnvSmokeHarness) -> EnvSmokeHarnessOutcome:
        try:
            return smoke_harness(
                harness,
                mode=mode,
                timeout=timeout,
                which=which,
                probe=probe,
                exec_fn=exec_fn,
                now=now,
            )
        except Exception as exc:  # one harness must never sink the others
            logger.exception("env_smoke_harness_crashed", harness=harness.value)
            return _outcome(
                harness,
                EnvSmokeStatus.FAIL,
                reason=quota.sanitize_diagnostic(f"internal error: {exc}"),
                errors=[
                    EnvSmokeErrorEntry(
                        code=EnvSmokeErrorCode.ERR_UNKNOWN,
                        message=quota.sanitize_diagnostic(str(exc)) or "error",
                    )
                ],
            )

    with ThreadPoolExecutor(max_workers=max(1, len(harnesses))) as pool:
        outcomes = list(pool.map(one, harnesses))
    for outcome in outcomes:
        logger.info(
            "env_smoke_outcome",
            harness=outcome.harness.value,
            status=outcome.status.value,
            reason=outcome.reason,
        )
    counts = {status: 0 for status in EnvSmokeStatus}
    for outcome in outcomes:
        counts[outcome.status] += 1
    rc = 1 if counts[EnvSmokeStatus.FAIL] else 0
    errors = [e for o in outcomes for e in o.errors]
    if rc:
        ret = EnvSmokeReturnCode.RET_PARTIAL
    elif mode == EnvSmokeMode.DRY_RUN:
        ret = EnvSmokeReturnCode.RET_DRY_RUN
    else:
        ret = EnvSmokeReturnCode.RET_OK
    returns = [EnvSmokeReturnEntry(code=ret)]
    ts, ts_ns = now_ns_pair()
    summary = EnvSmokeSummary(
        total=len(outcomes),
        ok=counts[EnvSmokeStatus.OK],
        skip_quota=counts[EnvSmokeStatus.SKIP_QUOTA],
        skip_rate_limit=counts[EnvSmokeStatus.SKIP_RATE_LIMIT],
        skip_missing=counts[EnvSmokeStatus.SKIP_MISSING],
        fail=counts[EnvSmokeStatus.FAIL],
        timestamp=ts,
        timestamp_unix_ns=ts_ns,
        errors=errors,
        returns=returns,
    )
    return EnvSmokeResponse(
        command="env:smoke",
        mode=mode,
        rc=rc,
        summary=summary,
        outcomes=outcomes,
        timestamp=ts,
        timestamp_unix_ns=ts_ns,
        errors=errors,
        returns=returns,
    )
