"""Zero-token quota pre-flight readers for the 6 smoke-tested AI harnesses.

Adapted (read-only logic, not imported) from the herdr-app session-tree meters.
Every reset/expiry is derived from provider payloads compared to the current
time; nothing here hardcodes a calendar date.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from time import monotonic
from typing import Any

from dotfiles_py.generated.env_smoke_schema import (
    EnvSmokeQuotaReading,
    EnvSmokeQuotaWindow,
)
from dotfiles_py.logging import get_logger
from env_inventory.collector import build_privacy_context, sanitize_text

logger = get_logger("env_smoke")

PROBE_TIMEOUT_S = 15.0
MAX_PROBE_BYTES = 4 * 1024 * 1024
RPC_MAX_LINE = 262_144
ZCODE_BALANCE_EVENT = "[coding-plan-availability] billing/balance "
ZCODE_BALANCE_URL = "https://zcode.z.ai/api/v1/zcode-plan/billing/balance"
ZCODE_TAIL_BYTES = 1024 * 1024
ZCODE_LOG_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.log$")

CLAUDE_SMOKE_MODEL = "haiku"
CODEX_SMOKE_MODEL = "gpt-6-luna"
AGY_SMOKE_MODEL = "gemini-3.8-flash-low"
AGY_FALLBACK_MODEL = "gpt-oss-120b-medium"
OPENCODE_SMOKE_MODEL = "opencode/deepseek-v4.1-flash-free"
ZCODE_SMOKE_MODEL = "account:zai-start-plan/GLM-5.3-Flash"
HERMES_FALLBACK_MODEL = "upstage/solar-mini4:free"

type Json = dict[str, Any]
type RpcFn = Callable[[Sequence[str], Sequence[Json], Callable[[Json], bool]], Json]
type RunFn = Callable[[Sequence[str], Mapping[str, str], float], bytes]


# --------------------------------------------------------------------------- utils


def now_utc() -> datetime:
    return datetime.now(UTC)


def _label(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 240 and value.isprintable():
        return value
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def iso_time(value: object) -> str | None:
    """Parse an epoch-seconds number or ISO string into an aware ISO-8601 string."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(value, UTC)
        elif isinstance(value, str) and value.strip():
            text = value.strip()
            if text.isdecimal():
                parsed = datetime.fromtimestamp(int(text), UTC)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            return None
    except AttributeError, OSError, OverflowError, TypeError, ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is not None else None


def _window(
    label: str, remaining: float, reset: str | None, group: str | None = None
) -> EnvSmokeQuotaWindow | None:
    if not 0 <= remaining <= 100:
        remaining = min(100.0, max(0.0, remaining))
    return EnvSmokeQuotaWindow(
        label=label, remaining_percent=float(remaining), reset=reset, group=group
    )


def _earliest(resets: Iterable[str | None]) -> str | None:
    parsed = []
    for reset in resets:
        if not reset:
            continue
        try:
            parsed.append(datetime.fromisoformat(reset))
        except ValueError:
            continue
    return min(parsed).isoformat() if parsed else None


@lru_cache(maxsize=1)
def _privacy_ctx() -> dict[str, Any]:
    return build_privacy_context(runner=lambda *_a, **_kw: (1, "", ""))


def sanitize_diagnostic(text: str) -> str:
    """Scrub private paths, usernames, hostnames, and tokens from diagnostics."""
    return sanitize_text(text, _privacy_ctx())


def _exhausted(
    windows: Iterable[EnvSmokeQuotaWindow], *, now: datetime | None = None
) -> list[EnvSmokeQuotaWindow]:
    out: list[EnvSmokeQuotaWindow] = []
    for w in windows:
        if w.remaining_percent > 0:
            continue
        if now is not None and w.reset:
            try:
                parsed = datetime.fromisoformat(w.reset)
                if parsed.tzinfo is not None and parsed <= now:
                    continue
            except ValueError:
                pass
        out.append(w)
    return out


def clean_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("__MISE_")}
    if extra:
        env.update(extra)
    return env


def killpg(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


# ----------------------------------------------------------------------- transports


def rpc(
    args: Sequence[str],
    messages: Sequence[Json],
    accept: Callable[[Json], bool],
    *,
    timeout: float = PROBE_TIMEOUT_S,
    env: Mapping[str, str] | None = None,
) -> Json:
    """Write NDJSON requests to a long-lived CLI and return the first accepted reply."""
    with (
        tempfile.TemporaryDirectory(prefix="env-smoke-rpc-") as scratch,
        subprocess.Popen(
            list(args),
            cwd=scratch,
            env=dict(env) if env is not None else clean_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process,
    ):
        assert process.stdin is not None
        assert process.stdout is not None
        stdout = process.stdout
        try:
            process.stdin.write(
                b"".join(json.dumps(item).encode() + b"\n" for item in messages)
            )
            process.stdin.flush()
            pending = b""
            deadline = monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(stdout, selectors.EVENT_READ)
                while (remaining := deadline - monotonic()) > 0:
                    if not selector.select(remaining):
                        break
                    chunk = (
                        stdout.read1(65536)
                        if isinstance(stdout, io.BufferedReader)
                        else os.read(stdout.fileno(), 65536)
                    )
                    if not chunk:
                        break
                    lines = (pending + chunk).split(b"\n")
                    pending = lines.pop()
                    if len(pending) > RPC_MAX_LINE:
                        raise ValueError("meter response too large")
                    for line in lines:
                        try:
                            message = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(message, dict) and accept(message):
                            return message
            raise ValueError("meter sent no supported result")
        finally:
            killpg(process)


def run_capture(args: Sequence[str], env: Mapping[str, str], timeout: float) -> bytes:
    """Run a quota-only command with bounded incremental stdout capture."""
    with (
        tempfile.TemporaryDirectory(prefix="env-smoke-probe-") as scratch,
        subprocess.Popen(
            list(args),
            cwd=scratch,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process,
    ):
        assert process.stdout is not None
        stdout_fd = process.stdout
        chunks: list[bytes] = []
        total = 0
        deadline = monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(stdout_fd, selectors.EVENT_READ)
                while (remaining := deadline - monotonic()) > 0:
                    if not selector.select(remaining):
                        raise subprocess.TimeoutExpired(list(args), timeout)
                    chunk = os.read(stdout_fd.fileno(), 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PROBE_BYTES:
                        raise ValueError(
                            f"{args[0]} quota output exceeded {MAX_PROBE_BYTES} bytes"
                        )
                    chunks.append(chunk)
                else:
                    raise subprocess.TimeoutExpired(list(args), timeout)
            rc = process.wait(timeout=max(0.1, deadline - monotonic()))
        finally:
            with contextlib.suppress(OSError):
                stdout_fd.close()
            killpg(process)
        if rc != 0:
            raise ValueError(f"{args[0]} quota command failed rc={rc}")
        return b"".join(chunks)


def _unavailable_probe(reason: str, now: datetime) -> EnvSmokeQuotaReading:
    return EnvSmokeQuotaReading(
        available=True,
        probe_ok=False,
        reason=sanitize_diagnostic(reason),
        checked_at=now.isoformat(),
    )


def _gate(
    windows: list[EnvSmokeQuotaWindow],
    relevant: list[EnvSmokeQuotaWindow],
    *,
    model: str,
    now: datetime,
    extra_ok: bool = False,
    blocked_reason: str | None = None,
) -> EnvSmokeQuotaReading:
    exhausted = _exhausted(relevant, now=now)
    stale_zero = any(w.remaining_percent <= 0 for w in relevant) and not exhausted
    if blocked_reason:
        return EnvSmokeQuotaReading(
            available=False,
            probe_ok=True,
            windows=windows,
            reset=_earliest(w.reset for w in exhausted or relevant),
            model=model,
            reason=blocked_reason,
            checked_at=now.isoformat(),
        )
    if exhausted and not extra_ok:
        labels = ", ".join(w.label for w in exhausted)
        return EnvSmokeQuotaReading(
            available=False,
            probe_ok=True,
            windows=windows,
            reset=_earliest(w.reset for w in exhausted),
            model=model,
            reason=f"quota exhausted: {labels}",
            checked_at=now.isoformat(),
        )
    if exhausted:
        reason = "windows exhausted but extra usage enabled"
    elif stale_zero:
        reason = "stale exhausted window reset in past; proceeding"
    elif windows:
        reason = "quota available"
    else:
        reason = "no quota windows reported"
    return EnvSmokeQuotaReading(
        available=True,
        probe_ok=True,
        windows=windows,
        model=model,
        reason=reason,
        checked_at=now.isoformat(),
    )


# ------------------------------------------------------------------------- claude

CLAUDE_USAGE_ARGS = (
    "-p",
    "--safe-mode",
    "--no-session-persistence",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
)
CLAUDE_REQUEST_ID = "env-smoke-quota"


def _matches_claude_target(
    scoped_model: str | None, surface: str | None, model: str
) -> bool:
    model_ok = (
        not scoped_model
        or model.lower() in scoped_model.lower()
        or "all" in scoped_model.lower()
    )
    surface_ok = not surface or any(
        tok in surface.lower() for tok in ("claude code", "cli", "all")
    )
    return model_ok and surface_ok


def parse_claude_usage(
    usage: Json, *, now: datetime, model: str = CLAUDE_SMOKE_MODEL
) -> EnvSmokeQuotaReading:
    """Gate on Claude `get_usage`: skip if any relevant window <= 0 and extraUsage != true."""
    if usage.get("rate_limits_available") is not True:
        return _gate([], [], model=model, now=now)
    limits = usage.get("rate_limits") or {}
    windows: list[EnvSmokeQuotaWindow] = []
    relevant: list[EnvSmokeQuotaWindow] = []
    seen: set[str] = set()
    for meter in limits.get("limits") or []:
        if not isinstance(meter, dict):
            continue
        group = meter.get("group")
        period = {"session": "5-hour", "five_hour": "5-hour", "weekly": "Weekly"}.get(
            str(group), _label(group)
        )
        scope = meter.get("scope") or {}
        scoped_model = _label((scope.get("model") or {}).get("display_name"))
        surface = _label((scope.get("surface") or {}).get("display_name"))
        used = _number(meter.get("percent"))
        if not period or used is None:
            continue
        name = " · ".join(filter(None, (period, scoped_model or surface)))
        scope_label = scoped_model or surface
        row = _window(name, 100 - used, iso_time(meter.get("resets_at")), scope_label)
        if row:
            windows.append(row)
            seen.add(name)
            if _matches_claude_target(scoped_model, surface, model):
                relevant.append(row)
    legacy = (
        ("five_hour", "5-hour", None),
        ("seven_day", "Weekly", None),
        ("seven_day_opus", "Weekly · Opus", "Opus"),
        ("seven_day_sonnet", "Weekly · Sonnet", "Sonnet"),
    )
    for key, name, scoped_model in legacy:
        meter = limits.get(key) or {}
        used = _number(meter.get("utilization")) if isinstance(meter, dict) else None
        if used is None or name in seen:
            continue
        row = _window(name, 100 - used, iso_time(meter.get("resets_at")), scoped_model)
        if row:
            windows.append(row)
            seen.add(name)
            if _matches_claude_target(scoped_model, None, model):
                relevant.append(row)
    extra = (limits.get("extra_usage") or {}).get("is_enabled")
    return _gate(windows, relevant, model=model, now=now, extra_ok=extra is True)


def probe_claude(
    binary: str, *, now: datetime | None = None, rpc_fn: RpcFn | None = None
) -> EnvSmokeQuotaReading:
    now = now or now_utc()
    call = rpc_fn or (lambda a, m, acc: rpc(a, m, acc))
    try:
        reply = call(
            [binary, *CLAUDE_USAGE_ARGS],
            [
                {
                    "type": "control_request",
                    "request_id": CLAUDE_REQUEST_ID,
                    "request": {"subtype": "get_usage", "skip_behaviors": True},
                }
            ],
            lambda item: (
                item.get("type") == "control_response"
                and (item.get("response") or {}).get("request_id") == CLAUDE_REQUEST_ID
            ),
        )
        usage = reply["response"]["response"]
        if not isinstance(usage, dict):
            raise TypeError("usage payload is not an object")
        return parse_claude_usage(usage, now=now)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        err = sanitize_diagnostic(str(exc))
        logger.warning("claude_quota_probe_failed", error=err)
        return _unavailable_probe(f"claude get_usage failed: {err}", now)


# -------------------------------------------------------------------------- codex


def _collect_codex_bucket(
    key: str,
    bucket: Mapping[str, Any],
    windows: list[EnvSmokeQuotaWindow],
    target: list[EnvSmokeQuotaWindow],
    *,
    is_relevant: bool,
) -> None:
    slug = _label(bucket.get("normalModelSlug"))
    name = _label(bucket.get("limitName")) or slug or str(key)
    for field in ("primary", "secondary"):
        meter = bucket.get(field) or {}
        used = _number(meter.get("usedPercent")) if isinstance(meter, dict) else None
        if used is None:
            continue
        mins = meter.get("windowDurationMins")
        period = {300: "5-hour", 10080: "Weekly"}.get(
            mins, f"{mins} min" if isinstance(mins, int) else field.title()
        )
        row = _window(
            f"{name} · {period}", 100 - used, iso_time(meter.get("resetsAt")), name
        )
        if row:
            windows.append(row)
            if is_relevant:
                target.append(row)


def parse_codex_rate_limits(
    result: Json, *, now: datetime, model: str = CODEX_SMOKE_MODEL
) -> EnvSmokeQuotaReading:
    """Gate on `account/rateLimits/read`: skip if ordinary usage disallowed or window <= 0."""
    by_id = result.get("rateLimitsByLimitId")
    buckets = by_id if isinstance(by_id, dict) and by_id else None
    windows: list[EnvSmokeQuotaWindow] = []
    relevant: list[EnvSmokeQuotaWindow] = []
    if buckets is not None:
        for key, bucket in buckets.items():
            if not isinstance(bucket, dict) or not re.fullmatch(
                r"[A-Za-z0-9_. -]{1,64}", str(key)
            ):
                continue
            slug = _label(bucket.get("normalModelSlug"))
            is_rel = key == "codex" or (slug is not None and slug == model)
            _collect_codex_bucket(
                str(key), bucket, windows, relevant, is_relevant=is_rel
            )
    if not relevant and isinstance(result.get("rateLimits"), dict):
        _collect_codex_bucket(
            "codex",
            result["rateLimits"],
            windows,
            relevant,
            is_relevant=True,
        )
    blocked = (
        "ordinaryUsageAllowed=false"
        if result.get("ordinaryUsageAllowed") is False
        else None
    )
    return _gate(windows, relevant, model=model, now=now, blocked_reason=blocked)


def _format_codex_rpc_error(err: Any) -> str:
    if isinstance(err, dict):
        code = err.get("code")
        msg = _label(err.get("message")) or ""
        parts = [
            f"code={code}" if code is not None else "",
            sanitize_diagnostic(msg)[:120],
        ]
        detail = " ".join(p for p in parts if p)
        return detail or "rpc error"
    return sanitize_diagnostic(str(err))[:160]


def probe_codex(
    binary: str, *, now: datetime | None = None, rpc_fn: RpcFn | None = None
) -> EnvSmokeQuotaReading:
    now = now or now_utc()
    call = rpc_fn or (lambda a, m, acc: rpc(a, m, acc))
    try:
        reply = call(
            [binary, "app-server"],
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "dotfiles-v2-env-smoke", "version": "1"}
                    },
                },
                {"jsonrpc": "2.0", "method": "initialized"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "account/rateLimits/read",
                    "params": {},
                },
            ],
            lambda item: item.get("id") == 2,
        )
        if "error" in reply:
            raise ValueError(
                f"rateLimits/read error: {_format_codex_rpc_error(reply['error'])}"
            )
        result = reply["result"]
        if not isinstance(result, dict):
            raise TypeError("rateLimits result is not an object")
        return parse_codex_rate_limits(result, now=now)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        err = sanitize_diagnostic(str(exc))
        logger.warning("codex_quota_probe_failed", error=err)
        return _unavailable_probe(f"codex rateLimits/read failed: {err}", now)


# ---------------------------------------------------------------------------- agy

AGY_USAGE_ARGS = (
    "--print",
    "/usage",
    "--output-format",
    "json",
    "--print-timeout",
    "10s",
    "--log-file",
    "/dev/null",
)


def agy_env(scratch: Path) -> dict[str, str]:
    """Env with no-op `open`/`xdg-open` stubs first on PATH so no browser ever opens."""
    for command in ("open", "xdg-open"):
        stub = scratch / command
        if not stub.exists():
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stub.chmod(0o700)
    base = clean_env()
    return clean_env(
        {
            "PATH": os.pathsep.join([str(scratch), base.get("PATH", os.defpath)]),
            "BROWSER": str(scratch / "open"),
            "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
        }
    )


def _is_agy_gpt_pool(name: str) -> bool:
    lowered = name.lower()
    return any(tok in lowered for tok in ("gpt", "oss", "openai"))


def parse_agy_usage(reply: Json, *, now: datetime) -> EnvSmokeQuotaReading:
    """Gate on Gemini pool, falling back to GPT pool's model when Gemini is unavailable."""
    report = reply.get("command") or {}
    usage = reply.get("usage") or {}
    if (
        reply.get("status") != "SUCCESS"
        or report.get("name") != "usage"
        or reply.get("num_turns") not in (0, None)
        or usage.get("total_tokens") not in (0, None)
    ):
        raise ValueError("agy did not return a zero-token quota-only /usage report")
    windows: list[EnvSmokeQuotaWindow] = []
    pools: dict[str, list[EnvSmokeQuotaWindow]] = {}
    for group in (report.get("data") or {}).get("groups") or []:
        if not isinstance(group, dict):
            continue
        name = _label(group.get("name"))
        if not name:
            continue
        pool = pools.setdefault(name, [])
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or bucket.get("enabled") is False:
                continue
            period = {"weekly": "Weekly", "5h": "5-hour"}.get(
                str(bucket.get("window"))
            ) or _label(bucket.get("name"))
            fraction = _number(bucket.get("remaining_fraction"))
            if not period or fraction is None:
                continue
            row = _window(
                f"{name} · {period}",
                100 * fraction,
                iso_time(bucket.get("reset_time")),
                name,
            )
            if row:
                windows.append(row)
                pool.append(row)
    gemini = [
        w for name, rows in pools.items() if "gemini" in name.lower() for w in rows
    ]
    if gemini and not _exhausted(gemini, now=now):
        return _gate(windows, gemini, model=AGY_SMOKE_MODEL, now=now)
    if not pools:
        return _gate([], [], model=AGY_SMOKE_MODEL, now=now)
    gpt_fallback_pools = [
        rows
        for name, rows in pools.items()
        if _is_agy_gpt_pool(name) and rows and not _exhausted(rows, now=now)
    ]
    if gpt_fallback_pools:
        return EnvSmokeQuotaReading(
            available=True,
            probe_ok=True,
            windows=windows,
            reset=_earliest(w.reset for w in _exhausted(gemini, now=now)),
            model=AGY_FALLBACK_MODEL,
            reason=f"Gemini pool unavailable; falling back to {AGY_FALLBACK_MODEL}",
            checked_at=now.isoformat(),
        )
    if not gemini:
        return EnvSmokeQuotaReading(
            available=False,
            probe_ok=True,
            windows=windows,
            reset=_earliest(w.reset for w in windows),
            model=AGY_SMOKE_MODEL,
            reason="no available Gemini or GPT fallback pool reported",
            checked_at=now.isoformat(),
        )
    return _gate(windows, gemini, model=AGY_SMOKE_MODEL, now=now)


def probe_agy(
    binary: str, *, now: datetime | None = None, run_fn: RunFn | None = None
) -> EnvSmokeQuotaReading:
    now = now or now_utc()
    run = run_fn or run_capture
    try:
        with tempfile.TemporaryDirectory(prefix="env-smoke-agy-") as scratch:
            out = run([binary, *AGY_USAGE_ARGS], agy_env(Path(scratch)), 15.0)
        reply = json.loads(out)
        if not isinstance(reply, dict):
            raise TypeError("agy /usage reply is not an object")
        return parse_agy_usage(reply, now=now)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        err = sanitize_diagnostic(str(exc))
        logger.warning("agy_quota_probe_failed", error=err)
        return _unavailable_probe(f"agy /usage failed: {err}", now)


# ----------------------------------------------------------------------- opencode


def probe_opencode(*, now: datetime | None = None) -> EnvSmokeQuotaReading:
    now = now or now_utc()
    return EnvSmokeQuotaReading(
        available=True,
        probe_ok=True,
        model=OPENCODE_SMOKE_MODEL,
        reason="$0 free tier; HTTP 429 is mapped post-hoc to SKIP_RATE_LIMIT",
        checked_at=now.isoformat(),
    )


# -------------------------------------------------------------------------- zcode


def _amount(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and len(value) <= 20 and value.isdecimal():
        return int(value)
    return None


def _epoch(value: object) -> int | None:
    seconds = _amount(value)
    return seconds if seconds else None


def zcode_balance_event(log_dir: Path) -> Json | None:
    """Return the newest `billing/balance` log event from the 2 newest daily logs."""
    if not log_dir.is_dir():
        return None
    logs = sorted(
        (p for p in log_dir.iterdir() if ZCODE_LOG_RE.match(p.name)), reverse=True
    )
    for path in logs[:2]:
        try:
            with path.open("rb") as handle:
                offset = max(0, handle.seek(0, os.SEEK_END) - ZCODE_TAIL_BYTES)
                handle.seek(offset)
                lines = (
                    handle.read(ZCODE_TAIL_BYTES)
                    .decode("utf-8", errors="replace")
                    .splitlines()
                )
        except OSError:
            continue
        for line in reversed(lines[bool(offset) :]):
            if ZCODE_BALANCE_EVENT not in line:
                continue
            _, _, payload = line.partition(ZCODE_BALANCE_EVENT)[2].partition("{")
            if not payload:
                continue
            try:
                event = json.loads("{" + payload)
            except ValueError:
                continue
            url = event.get("url") if isinstance(event, dict) else None
            if isinstance(url, str) and url.split("?", 1)[0] == ZCODE_BALANCE_URL:
                return event
    return None


def parse_zcode_balance(log_dir: Path, *, now: datetime) -> EnvSmokeQuotaReading:
    """Gate on newest balance snapshot: need an active, non-expired, non-empty row."""
    event = zcode_balance_event(log_dir)
    checked = now.isoformat()
    if event is None:
        return EnvSmokeQuotaReading(
            available=False,
            probe_ok=False,
            model=ZCODE_SMOKE_MODEL,
            reason=f"no billing/balance rows in {log_dir}",
            checked_at=checked,
        )
    payload = event.get("payload")
    data = payload.get("data") if isinstance(payload, dict) else None
    if event.get("success") is not True or not isinstance(data, dict):
        return EnvSmokeQuotaReading(
            available=False,
            probe_ok=False,
            model=ZCODE_SMOKE_MODEL,
            reason="newest billing/balance response was unsuccessful",
            checked_at=checked,
        )
    plans = [p for p in data.get("plans") or [] if isinstance(p, dict)]
    now_s = now.timestamp()
    windows: list[EnvSmokeQuotaWindow] = []
    active = 0
    current_count = 0
    current_resets: list[str | None] = []
    expired_at: list[str | None] = []
    for balance in data.get("balances") or []:
        if not isinstance(balance, dict):
            continue
        plan = next(
            (
                p
                for p in plans
                if balance.get("user_plan_id")
                and p.get("user_plan_id") == balance.get("user_plan_id")
            ),
            {},
        )
        name = (
            _label(balance.get("show_name")) or _label(plan.get("name")) or "Allowance"
        )
        status = str(plan.get("status") or "active").lower()
        total = _amount(balance.get("total_units"))
        left = _amount(balance.get("available_units"))
        if left is None:
            left = _amount(balance.get("remaining_units"))
        bal_expires = _epoch(balance.get("expires_at"))
        plan_ends = _epoch(plan.get("ends_at"))
        period_end = _epoch(balance.get("period_end"))
        remaining_pct = (
            100.0 * left / total
            if total and left is not None
            else (100.0 if left else 0.0)
        )
        effective_expiry = (
            min(t for t in (bal_expires, plan_ends) if t is not None)
            if (bal_expires is not None or plan_ends is not None)
            else None
        )
        reset = iso_time(period_end) if period_end else iso_time(effective_expiry)
        row = _window(name, remaining_pct, reset, _label(plan.get("name")))
        if row:
            windows.append(row)
        is_expired = (bal_expires is not None and bal_expires <= now_s) or (
            plan_ends is not None and plan_ends <= now_s
        )
        if is_expired:
            expired_at.append(iso_time(effective_expiry))
        else:
            current_count += 1
            if reset:
                current_resets.append(reset)
        if status == "active" and not is_expired and (left or 0) > 0:
            active += 1
    if active:
        return EnvSmokeQuotaReading(
            available=True,
            probe_ok=True,
            windows=windows,
            model=ZCODE_SMOKE_MODEL,
            reason=f"{active} active non-expired allowance(s)",
            checked_at=checked,
        )
    if current_count:
        reason = "no active allowance with remaining units"
    elif expired_at:
        reason = "all allowances expired"
    elif windows:
        reason = "no active allowance with remaining units"
    else:
        reason = "newest billing/balance snapshot lists no allowances"
    return EnvSmokeQuotaReading(
        available=False,
        probe_ok=True,
        windows=windows,
        reset=_earliest(current_resets) if current_resets else None,
        model=ZCODE_SMOKE_MODEL,
        reason=reason,
        checked_at=checked,
    )


def probe_zcode(
    *, home: Path | None = None, now: datetime | None = None
) -> EnvSmokeQuotaReading:
    now = now or now_utc()
    base = (home or Path.home()) / ".zcode" / "v2" / "logs"
    return parse_zcode_balance(base, now=now)


# ------------------------------------------------------------------------- hermes


def hermes_free_model(home: Path | None = None) -> tuple[str, str]:
    """Return `(model, source)` from the Nous recommended-models cache, else fallback."""
    cache = (home or Path.home()) / ".hermes" / "cache" / "nous_recommended_cache.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return HERMES_FALLBACK_MODEL, "fallback (cache unreadable)"
    candidates: list[Any] = [data]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, dict):
                candidates.extend([value, value.get("data")])
    for node in candidates:
        if not isinstance(node, dict):
            continue
        models = node.get("freeRecommendedModels")
        if isinstance(models, list) and models:
            first = models[0]
            name = first.get("modelName") if isinstance(first, dict) else first
            if _label(name):
                return str(name), "nous_recommended_cache freeRecommendedModels[0]"
    return HERMES_FALLBACK_MODEL, "fallback (no freeRecommendedModels)"


def probe_hermes(
    *, home: Path | None = None, now: datetime | None = None
) -> EnvSmokeQuotaReading:
    now = now or now_utc()
    model, source = hermes_free_model(home)
    return EnvSmokeQuotaReading(
        available=True,
        probe_ok=True,
        model=model,
        reason=f"free Nous model from {source}",
        checked_at=now.isoformat(),
    )
