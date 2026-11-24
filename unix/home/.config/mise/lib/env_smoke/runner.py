"""Quota-gated smoke and agentsview skill-check runner for the 6 AI harnesses."""

from __future__ import annotations

import contextlib
import json
import os
import re
import selectors
import shlex
import shutil
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
_AGENTSVIEW_BIN_RE = re.compile(r"^(?:[\w./~$-]+/)?agentsview$")
_OK_REPLY_RE = re.compile(r"""^["'`\s]*OK[.!]?["'`\s]*$""")
_SHELL_SEP_RE = re.compile(r"\s*(?:&&|\|\||[;\n|&])\s*")


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
            return [
                *cmd,
                "-m",
                model,
                "-c",
                'model_reasoning_effort="low"',
                "--ephemeral",
                "-s",
                "read-only",
                "--skip-git-repo-check",
                prompt,
            ]
        case EnvSmokeHarness.AGY:
            cmd = [
                binary,
                "--print",
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
            return [*cmd, prompt]
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
            return [
                binary,
                "chat",
                "-q",
                prompt,
                "-m",
                model,
                "--provider",
                "nous",
                "--reasoning",
                "none",
                "--max-turns",
                "4" if skill_check else "2",
                "--format",
                "stream-json",
                "--source",
                "tool",
                "--yolo",
            ]


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


def _is_agentsview_exec(cmd_text: str) -> bool:
    """Return True only if an executed command/subshell invokes `agentsview` directly."""
    for seg in _SHELL_SEP_RE.split(cmd_text.strip()):
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        idx = 0
        while (
            idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-")
        ):
            idx += 1
        if idx < len(tokens) and Path(tokens[idx]).name == "env":
            idx += 1
            while idx < len(tokens) and (
                tokens[idx].startswith("-") or "=" in tokens[idx]
            ):
                idx += 1
        if idx >= len(tokens):
            continue
        head = tokens[idx]
        if _AGENTSVIEW_BIN_RE.match(head):
            return True
        if Path(head).name in {"sh", "bash", "zsh", "dash"}:
            for j in range(idx + 1, len(tokens) - 1):
                if tokens[j] in {"-c", "-lc", "-ic"} and _is_agentsview_exec(
                    tokens[j + 1]
                ):
                    return True
    return False


def _collect_failed_tool_ids(docs: Sequence[Any]) -> set[str]:
    failed: set[str] = set()

    def scan(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                scan(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "tool_result" and node.get("is_error") is True:
            tid = node.get("tool_use_id") or node.get("id")
            if isinstance(tid, str) and tid:
                failed.add(tid)
        for val in node.values():
            if isinstance(val, (dict, list)):
                scan(val)

    for d in docs:
        scan(d)
    return failed


def _invocation_failed(node: Mapping[str, Any], failed_ids: set[str]) -> bool:
    tid = node.get("id") or node.get("tool_use_id") or node.get("call_id")
    if isinstance(tid, str) and tid in failed_ids:
        return True
    if node.get("is_error") is True:
        return True
    ec = node.get("exit_code")
    if isinstance(ec, int) and not isinstance(ec, bool) and ec != 0:
        return True
    st = node.get("status")
    if isinstance(st, str) and st.lower() in {"error", "failed", "cancelled"}:
        return True
    state = node.get("state")
    return isinstance(state, Mapping) and _invocation_failed(state, failed_ids)


def _mcp_name(node: Mapping[str, Any]) -> str | None:
    if isinstance(node.get("server"), str) and isinstance(node.get("tool"), str):
        return f"{node['server']}/{node['tool']}"
    for key in ("name", "tool", "tool_name", "toolName"):
        val = node.get(key)
        if isinstance(val, str):
            low = val.lower()
            if low.startswith(("mcp__", "mcp.", "mcp:")):
                return val
    return None


def _is_invocation_node(node: Mapping[str, Any]) -> bool:
    for key in ("type", "kind"):
        val = node.get(key)
        if isinstance(val, str) and val.lower() in _TOOL_EVENT_KINDS:
            return True
    return isinstance(node.get("tool"), str) and isinstance(node.get("state"), Mapping)


def _walk_invocations(
    node: Any,
    cli: list[str],
    mcp: list[str],
    *,
    failed_ids: set[str],
) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_invocations(item, cli, mcp, failed_ids=failed_ids)
        return
    if not isinstance(node, dict):
        return
    if _is_invocation_node(node):
        mcp_label = _mcp_name(node)
        kind_val = str(node.get("type") or node.get("kind") or "").lower()
        if mcp_label or kind_val in _MCP_KINDS:
            label = mcp_label or str(node.get("name") or node.get("tool") or kind_val)
            if label not in mcp:
                mcp.append(label)
        elif not _invocation_failed(node, failed_ids):
            name_val = str(
                node.get("name")
                or node.get("tool")
                or node.get("tool_name")
                or node.get("toolName")
                or kind_val
            ).lower()
            if name_val in _SHELL_TOOL_NAMES or kind_val == "command_execution":
                for src in (node, node.get("input"), node.get("state")):
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
    for key, value in node.items():
        if key == "tools":
            continue
        if isinstance(value, (dict, list)):
            _walk_invocations(value, cli, mcp, failed_ids=failed_ids)


def analyze_skill_output(text: str) -> EnvSmokeSkillCheck:
    """PASS only when a shell tool call ran the `agentsview` CLI and zero MCP calls occurred."""
    cli: list[str] = []
    mcp: list[str] = []
    docs = _json_documents(text)
    failed_ids = _collect_failed_tool_ids(docs)
    for doc in docs:
        _walk_invocations(doc, cli, mcp, failed_ids=failed_ids)
    if mcp:
        verdict = EnvSmokeSkillVerdict.FAIL_MCP_USED
    elif cli:
        verdict = EnvSmokeSkillVerdict.PASS
    else:
        verdict = EnvSmokeSkillVerdict.FAIL_NO_CLI
    return EnvSmokeSkillCheck(verdict=verdict, cli_calls=cli, mcp_calls=mcp)


def _excerpt(text: str) -> str | None:
    text = quota.sanitize_diagnostic(text.strip())
    if not text:
        return None
    return text if len(text) <= EXCERPT_CHARS else "…" + text[-EXCERPT_CHARS:]


def _extract_text_from_docs(docs: Sequence[Any]) -> str | None:
    texts: list[str] = []
    for doc in docs:
        if not isinstance(doc, Mapping):
            continue
        if str(doc.get("role") or doc.get("type") or "").lower() == "user":
            continue
        for key in ("result", "response", "text", "content"):
            val = doc.get(key)
            if isinstance(val, str) and val.strip():
                texts.append(val.strip())
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
