"""Build, guard, and run an isolated read-only `claude -p` review on API-key auth only.

Guards (each fails closed before or at the first stream event, so no request is billed to the
wrong pool):
- ANTHROPIC_API_KEY must be present (real key or a fnox proxy placeholder); subscription OAuth
  variables are never forwarded.
- The child gets an allowlisted environment, a scratch CLAUDE_CONFIG_DIR, `--bare`,
  `--restricted`, no slash commands, no setting sources, and no MCP servers.
- The stream-json `init` event must report apiKeySource == "ANTHROPIC_API_KEY", no MCP servers,
  and only the allowed tools; otherwise the child is killed immediately.
- The model is a closed allowlist (never fable, which can bill usage credits under `-p`).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import orjson

from dotfiles_py.generated.api_runner_schema import (
    ApiRunnerErrorCode,
    ApiRunnerErrorEntry,
    ApiRunnerRunResult,
    ApiRunnerSpec,
    ApiRunnerUsage,
)

EXPECTED_API_KEY_SOURCE = "ANTHROPIC_API_KEY"
REVIEW_TOOLS: tuple[str, ...] = ("Read",)
SUBSCRIPTION_AUTH_VARS: tuple[str, ...] = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
PASSTHROUGH_ENV: tuple[str, ...] = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "NODE_USE_ENV_PROXY",
)
FORCED_ENV: dict[str, str] = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


class RunnerError(Exception):
    """A fail-closed guard tripped; carries a typed error entry."""

    def __init__(
        self, code: ApiRunnerErrorCode, message: str, **detail: object
    ) -> None:
        super().__init__(message)
        self.entry = ApiRunnerErrorEntry(
            code=code, message=message, detail=detail or None
        )


# Bounds (turns, budget, timeout, model, effort) are enforced by the schema model.
RunSpec = ApiRunnerSpec


def resolve_claude_bin(home: Path | None = None) -> str | None:
    """Prefer the native installer's binary over any PATH shim."""
    native = (home or Path.home()) / ".local" / "bin" / "claude"
    if native.is_file() and os.access(native, os.X_OK):
        return str(native)
    return shutil.which("claude")


def validate_spec(spec: RunSpec) -> None:
    if not Path(spec.cwd).is_dir():
        raise RunnerError(
            ApiRunnerErrorCode.ERR_INVALID_INPUT,
            "cwd is not a directory",
            cwd=spec.cwd,
        )


def build_argv(spec: RunSpec, tools: Sequence[str] = REVIEW_TOOLS) -> list[str]:
    """Argv without the prompt; the prompt goes over stdin so it never appears in `ps`."""
    return [
        spec.claude_bin,
        "-p",
        "--bare",
        "--restricted",
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--model",
        spec.model.value,
        "--effort",
        spec.effort.value,
        "--tools",
        ",".join(tools),
        "--max-turns",
        str(spec.max_turns),
        "--max-budget-usd",
        f"{spec.max_budget_usd:.2f}",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def build_env(parent: Mapping[str, str], config_dir: Path) -> dict[str, str]:
    """Allowlisted child environment; refuses to run without an API key."""
    key = parent.get("ANTHROPIC_API_KEY", "")
    if not key.strip():
        raise RunnerError(
            ApiRunnerErrorCode.ERR_MISSING_CREDENTIAL,
            "ANTHROPIC_API_KEY is absent; run under the fnox `api-credit` profile",
        )
    if not key.startswith("fnox-placeholder"):
        raise RunnerError(
            ApiRunnerErrorCode.ERR_MISSING_CREDENTIAL,
            "a raw ANTHROPIC_API_KEY bypasses the strict-egress fnox proxy; use the api-credit profile",
        )
    if not (parent.get("HTTPS_PROXY") or parent.get("https_proxy")):
        raise RunnerError(
            ApiRunnerErrorCode.ERR_MISSING_CREDENTIAL,
            "placeholder key without HTTPS_PROXY; the fnox proxy is not active",
        )
    env = {name: parent[name] for name in PASSTHROUGH_ENV if name in parent}
    env.update(FORCED_ENV)
    env["ANTHROPIC_API_KEY"] = key
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    for name in SUBSCRIPTION_AUTH_VARS:
        env.pop(name, None)
    return env


def check_init(
    event: Mapping[str, Any], allowed_tools: Iterable[str]
) -> tuple[str, list[str]]:
    """Validate the stream-json init event before any model request is billed."""
    source = event.get("apiKeySource")
    if source != EXPECTED_API_KEY_SOURCE:
        raise RunnerError(
            ApiRunnerErrorCode.ERR_WRONG_AUTH_SOURCE,
            "claude did not select API-key auth; refusing to spend plan or usage credits",
            api_key_source=source,
        )
    tools = [str(t) for t in (event.get("tools") or [])]
    extra = sorted(set(tools) - set(allowed_tools))
    mcp = event.get("mcp_servers") or []
    if extra or mcp:
        raise RunnerError(
            ApiRunnerErrorCode.ERR_UNEXPECTED_TOOLS,
            "unexpected tools or MCP servers in isolated run",
            extra_tools=extra,
            mcp_servers=len(mcp),
        )
    return str(source), tools


def parse_result(
    event: Mapping[str, Any],
) -> tuple[ApiRunnerUsage, str | None, ApiRunnerErrorEntry | None]:
    raw = event.get("usage")
    usage_raw: dict[str, Any] = raw if isinstance(raw, dict) else {}
    usage = ApiRunnerUsage(
        total_cost_usd=float(event.get("total_cost_usd") or 0.0),
        num_turns=int(event.get("num_turns") or 0),
        duration_ms=int(event.get("duration_ms") or 0),
        input_tokens=_opt_int(usage_raw.get("input_tokens")),
        output_tokens=_opt_int(usage_raw.get("output_tokens")),
    )
    text = event.get("result")
    subtype = str(event.get("subtype") or "")
    error: ApiRunnerErrorEntry | None = None
    if "budget" in subtype:
        error = ApiRunnerErrorEntry(
            code=ApiRunnerErrorCode.ERR_BUDGET_EXCEEDED, message=subtype
        )
    elif event.get("is_error") or subtype != "success":
        error = ApiRunnerErrorEntry(
            code=ApiRunnerErrorCode.ERR_MODEL_ERROR,
            message=(str(text)[:300] if text else subtype) or "error",
        )
    return usage, (str(text) if text is not None else None), error


def _opt_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and value >= 0 else None


SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]+|fnox-placeholder-[A-Za-z0-9_-]+")


def stderr_tail(path: Path, limit: int = 800) -> str | None:
    """Bounded, secret-scrubbed tail of the child's stderr for diagnostics."""
    try:
        data = path.read_bytes()[-limit:].decode(errors="replace").strip()
    except OSError:
        return None
    return SECRET_RE.sub("<redacted>", data) or None


def run(
    spec: RunSpec,
    prompt: str,
    parent_env: Mapping[str, str],
    *,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> ApiRunnerRunResult:
    """Run claude, enforcing guards on the live event stream; never raises."""
    api_key_source: str | None = None
    tools: list[str] = []
    usage: ApiRunnerUsage | None = None
    result_text: str | None = None
    errors: list[ApiRunnerErrorEntry] = []

    def outcome() -> ApiRunnerRunResult:
        return ApiRunnerRunResult(
            api_key_source=api_key_source,
            tools=tools,
            usage=usage,
            result_text=result_text,
            guard_errors=errors,
        )

    try:
        validate_spec(spec)
        config_dir = Path(spec.config_dir)
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_dir.chmod(0o700)
        env = build_env(parent_env, config_dir)
    except RunnerError as exc:
        errors.append(exc.entry)
        return outcome()
    except OSError as exc:
        errors.append(
            ApiRunnerErrorEntry(
                code=ApiRunnerErrorCode.ERR_INVALID_INPUT, message=str(exc)
            )
        )
        return outcome()

    stderr_path = config_dir / "claude-stderr.log"
    timed_out = threading.Event()
    saw_init = False
    with stderr_path.open("wb") as stderr_fh:
        proc = popen(
            build_argv(spec),
            cwd=spec.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_fh,
        )

        def _expire() -> None:
            timed_out.set()
            proc.kill()

        watchdog = threading.Timer(spec.timeout_sec, _expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(prompt.encode())
            proc.stdin.close()
            for line in proc.stdout:
                try:
                    event = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init":
                    api_key_source, tools = check_init(event, REVIEW_TOOLS)
                    saw_init = True
                elif event.get("type") == "result":
                    if not saw_init:
                        raise RunnerError(
                            ApiRunnerErrorCode.ERR_WRONG_AUTH_SOURCE,
                            "result before init event",
                        )
                    usage, result_text, err = parse_result(event)
                    if err:
                        errors.append(err)
                elif not saw_init and event.get("type") != "system":
                    raise RunnerError(
                        ApiRunnerErrorCode.ERR_WRONG_AUTH_SOURCE,
                        "model event before init event",
                    )
        except RunnerError as exc:
            errors.append(exc.entry)
        except (OSError, ValueError) as exc:
            errors.append(
                ApiRunnerErrorEntry(
                    code=ApiRunnerErrorCode.ERR_COMMAND_FAILED,
                    message=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                proc.kill()
            code = proc.wait()

    if timed_out.is_set():
        errors.append(
            ApiRunnerErrorEntry(
                code=ApiRunnerErrorCode.ERR_TIMEOUT,
                message=f"exceeded {spec.timeout_sec}s",
            )
        )
    elif usage is None and not errors:
        tail = stderr_tail(stderr_path)
        errors.append(
            ApiRunnerErrorEntry(
                code=ApiRunnerErrorCode.ERR_COMMAND_FAILED,
                message=f"claude exited {code} without a result event",
                detail={"stderr_tail": tail} if tail else None,
            )
        )
    return outcome()
