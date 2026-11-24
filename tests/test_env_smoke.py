"""Tests for schema-first quota-gated 6-harness `env:smoke` (zero live model calls)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dotfiles_py.generated.env_smoke_schema import (
    EnvSmokeCommandResult,
    EnvSmokeErrorCode,
    EnvSmokeHarness,
    EnvSmokeMode,
    EnvSmokeQuotaReading,
    EnvSmokeResponse,
    EnvSmokeReturnCode,
    EnvSmokeSkillVerdict,
    EnvSmokeStatus,
)
from env_smoke import cli, quota, runner

REPO_ROOT = Path(__file__).resolve().parents[1]
TASK = REPO_ROOT / "tasks" / "env" / "smoke"
NOW = datetime.now(UTC)
TS_RE = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{9}(?:Z|[+-][0-9]{4})")


def _iso(delta_s: float) -> str:
    return (NOW + timedelta(seconds=delta_s)).isoformat()


def _epoch(delta_s: float) -> int:
    return int(NOW.timestamp() + delta_s)


def _result(
    stdout: str = "",
    *,
    exit_code: int | None = 0,
    stderr: str = "",
    timed_out: bool = False,
) -> EnvSmokeCommandResult:
    return EnvSmokeCommandResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=7,
        timed_out=timed_out,
    )


def _reading(**kw: Any) -> EnvSmokeQuotaReading:
    base: dict[str, Any] = {
        "available": True,
        "probe_ok": True,
        "checked_at": NOW.isoformat(),
    }
    base.update(kw)
    return EnvSmokeQuotaReading(**base)


# ------------------------------------------------------------------- claude


def _claude_usage(
    weekly_used: float, *, extra: bool | None = None, opus_used: float = 10
) -> dict[str, Any]:
    limits: dict[str, Any] = {
        "limits": [
            {"group": "session", "percent": 20, "resets_at": _iso(3600)},
            {"group": "weekly", "percent": weekly_used, "resets_at": _epoch(86400)},
            {
                "group": "weekly",
                "percent": opus_used,
                "resets_at": _iso(7200),
                "scope": {"model": {"display_name": "Opus"}},
            },
        ],
        "seven_day": {"utilization": 99, "resets_at": _iso(5)},
    }
    if extra is not None:
        limits["extra_usage"] = {"is_enabled": extra}
    return {"rate_limits_available": True, "rate_limits": limits}


def test_claude_parser_available_and_ignores_other_model_scope() -> None:
    reading = quota.parse_claude_usage(_claude_usage(40, opus_used=100), now=NOW)
    assert reading.available is True
    assert reading.model == "haiku"
    labels = {w.label: w.remaining_percent for w in reading.windows}
    assert labels == {"5-hour": 80.0, "Weekly": 60.0, "Weekly · Opus": 0.0}


def test_claude_parser_skips_with_reset_from_payload() -> None:
    reading = quota.parse_claude_usage(_claude_usage(100), now=NOW)
    assert reading.available is False
    assert reading.reset == datetime.fromtimestamp(_epoch(86400), UTC).isoformat()
    assert "Weekly" in (reading.reason or "")


def test_claude_parser_extra_usage_overrides_exhaustion() -> None:
    reading = quota.parse_claude_usage(_claude_usage(100, extra=True), now=NOW)
    assert reading.available is True
    assert "extra usage" in (reading.reason or "")
    assert (
        quota.parse_claude_usage(_claude_usage(100, extra=False), now=NOW).available
        is False
    )


def test_claude_parser_legacy_and_unavailable_limits() -> None:
    legacy = {
        "rate_limits_available": True,
        "rate_limits": {
            "five_hour": {"utilization": 100, "resets_at": _iso(60)},
            "seven_day_opus": {"utilization": 100, "resets_at": _iso(90)},
        },
    }
    reading = quota.parse_claude_usage(legacy, now=NOW)
    assert reading.available is False
    assert reading.reset == _iso(60)
    none = quota.parse_claude_usage({"rate_limits_available": False}, now=NOW)
    assert none.available is True
    assert none.windows == []


def test_probe_claude_rpc_shape_and_failure() -> None:
    seen: dict[str, Any] = {}

    def fake_rpc(args, messages, accept):
        seen["args"] = list(args)
        seen["messages"] = messages
        reply = {
            "type": "control_response",
            "response": {
                "request_id": quota.CLAUDE_REQUEST_ID,
                "response": _claude_usage(10),
            },
        }
        assert accept(reply)
        assert not accept({"type": "assistant"})
        return reply

    reading = quota.probe_claude("/x/claude", now=NOW, rpc_fn=fake_rpc)
    assert reading.available is True
    assert seen["args"][:3] == ["/x/claude", "-p", "--safe-mode"]
    assert "--bare" not in seen["args"]
    assert seen["messages"][0]["request"]["subtype"] == "get_usage"

    def boom(*_a):
        raise ValueError("meter sent no supported result")

    failed = quota.probe_claude("/x/claude", now=NOW, rpc_fn=boom)
    assert failed.probe_ok is False
    assert failed.available is True


# -------------------------------------------------------------------- codex


def _codex_result(
    primary: float = 10, secondary: float = 20, *, allowed: bool | None = True
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {
                    "usedPercent": primary,
                    "windowDurationMins": 300,
                    "resetsAt": _epoch(1800),
                },
                "secondary": {
                    "usedPercent": secondary,
                    "windowDurationMins": 10080,
                    "resetsAt": _epoch(600000),
                },
            },
            "other_model": {
                "normalModelSlug": "gpt-6-pro",
                "primary": {"usedPercent": 100, "windowDurationMins": 300},
            },
        }
    }
    if allowed is not None:
        result["ordinaryUsageAllowed"] = allowed
    return result


def test_codex_parser_available_and_ignores_unrelated_bucket() -> None:
    reading = quota.parse_codex_rate_limits(_codex_result(), now=NOW)
    assert reading.available is True
    assert reading.model == "gpt-6-luna"
    assert {w.label for w in reading.windows} == {
        "codex · 5-hour",
        "codex · Weekly",
        "gpt-6-pro · 5-hour",
    }


def test_codex_parser_skips_on_exhausted_window_and_disallowed() -> None:
    exhausted = quota.parse_codex_rate_limits(_codex_result(primary=100), now=NOW)
    assert exhausted.available is False
    assert exhausted.reset == datetime.fromtimestamp(_epoch(1800), UTC).isoformat()
    disallowed = quota.parse_codex_rate_limits(_codex_result(allowed=False), now=NOW)
    assert disallowed.available is False
    assert disallowed.reason == "ordinaryUsageAllowed=false"
    legacy = quota.parse_codex_rate_limits(
        {"rateLimits": {"primary": {"usedPercent": 5}}}, now=NOW
    )
    assert legacy.available is True


def test_probe_codex_rpc_and_error_reply() -> None:
    def fake_rpc(args, messages, accept):
        assert list(args) == ["/x/codex", "app-server"]
        assert [m.get("method") for m in messages] == [
            "initialize",
            "initialized",
            "account/rateLimits/read",
        ]
        assert accept({"id": 2})
        return {"id": 2, "result": _codex_result(secondary=100)}

    assert quota.probe_codex("/x/codex", now=NOW, rpc_fn=fake_rpc).available is False
    err = quota.probe_codex(
        "/x/codex", now=NOW, rpc_fn=lambda *_a: {"id": 2, "error": {"code": -1}}
    )
    assert err.probe_ok is False


# ---------------------------------------------------------------------- agy


def _agy_reply(gemini: float, other: float, **overrides: Any) -> dict[str, Any]:
    def group(name: str, frac: float) -> dict[str, Any]:
        return {
            "name": name,
            "buckets": [
                {
                    "window": "weekly",
                    "remaining_fraction": frac,
                    "reset_time": _iso(9000),
                },
                {"window": "5h", "remaining_fraction": 0.5, "reset_time": _iso(900)},
                {"window": "5h", "enabled": False, "remaining_fraction": 0.0},
            ],
        }

    reply: dict[str, Any] = {
        "status": "SUCCESS",
        "num_turns": 0,
        "usage": {"total_tokens": 0},
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    group("Gemini Models", gemini),
                    group("Claude and GPT models", other),
                ]
            },
        },
    }
    reply.update(overrides)
    return reply


def test_agy_parser_gemini_available() -> None:
    reading = quota.parse_agy_usage(_agy_reply(0.4, 0.4), now=NOW)
    assert reading.available is True
    assert reading.model == quota.AGY_SMOKE_MODEL
    assert len(reading.windows) == 4


def test_agy_parser_fallback_and_skip() -> None:
    fallback = quota.parse_agy_usage(_agy_reply(0.0, 0.3), now=NOW)
    assert fallback.available is True
    assert fallback.model == quota.AGY_FALLBACK_MODEL
    assert fallback.reset == _iso(9000)
    skipped = quota.parse_agy_usage(_agy_reply(0.0, 0.0), now=NOW)
    assert skipped.available is False
    assert skipped.reset == _iso(9000)


def test_agy_parser_rejects_token_spending_reply() -> None:
    with pytest.raises(ValueError):
        quota.parse_agy_usage(_agy_reply(1, 1, num_turns=1), now=NOW)


def test_probe_agy_stubs_browser_and_disables_update(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_run(args: Sequence[str], env: Mapping[str, str], timeout: float) -> bytes:
        seen["args"] = list(args)
        seen["env"] = dict(env)
        stub_dir = Path(env["BROWSER"]).parent
        seen["stubs"] = sorted(p.name for p in stub_dir.iterdir())
        seen["path_head"] = env["PATH"].split(os.pathsep)[0]
        return json.dumps(_agy_reply(0.5, 0.5)).encode()

    reading = quota.probe_agy("/x/agy", now=NOW, run_fn=fake_run)
    assert reading.available is True
    assert seen["args"][1:3] == ["--print", "/usage"]
    assert "--log-file" in seen["args"]
    assert seen["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert seen["stubs"] == ["open", "xdg-open"]
    assert seen["path_head"] == str(Path(seen["env"]["BROWSER"]).parent)
    bad = quota.probe_agy("/x/agy", now=NOW, run_fn=lambda *_a: b"not json")
    assert bad.probe_ok is False


# -------------------------------------------------------------------- zcode


def _zcode_line(balances: list[dict[str, Any]], plans: list[dict[str, Any]]) -> str:
    event = {
        "success": True,
        "url": quota.ZCODE_BALANCE_URL + "?app_version=4.0.0",
        "payload": {
            "code": 0,
            "data": {"server_time": _epoch(0), "plans": plans, "balances": balances},
        },
    }
    return (
        f"[{(NOW - timedelta(days=1)).strftime('%Y-%m-%d')} 00:00:00.000] [info] [main] "
        f"{quota.ZCODE_BALANCE_EVENT}请求完成 {json.dumps(event)}\n"
    )


def _zcode_home(tmp_path: Path, *lines: str, name: str | None = None) -> Path:
    logs = tmp_path / ".zcode" / "v2" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_name = name or f"{(NOW - timedelta(days=1)).strftime('%Y-%m-%d')}.log"
    (logs / log_name).write_text("noise line\n" + "".join(lines), encoding="utf-8")
    return tmp_path


def _zbal(remaining: int, expires: int, plan_id: str = "upl_1") -> dict[str, Any]:
    return {
        "user_plan_id": plan_id,
        "show_name": "GLM-5.3-Flash",
        "total_units": 1000,
        "used_units": 1000 - remaining,
        "remaining_units": remaining,
        "available_units": remaining,
        "period_end": expires,
        "expires_at": expires,
    }


def test_zcode_parser_active_non_expired(tmp_path: Path) -> None:
    home = _zcode_home(
        tmp_path,
        _zcode_line(
            [_zbal(400, _epoch(3600))],
            [{"user_plan_id": "upl_1", "name": "Plan", "status": "active"}],
        ),
    )
    reading = quota.probe_zcode(home=home, now=NOW)
    assert reading.available is True
    assert reading.windows[0].remaining_percent == 40.0
    assert reading.model == quota.ZCODE_SMOKE_MODEL


def test_zcode_parser_expired_empty_and_missing(tmp_path: Path) -> None:
    plans = [{"user_plan_id": "upl_1", "name": "Plan", "status": "active"}]
    expired = quota.probe_zcode(
        home=_zcode_home(tmp_path / "a", _zcode_line([_zbal(400, _epoch(-60))], plans)),
        now=NOW,
    )
    assert expired.available is False
    assert expired.reason == "all allowances expired"
    # Newest line wins even when an older line in the same file had quota.
    newest_empty = quota.probe_zcode(
        home=_zcode_home(
            tmp_path / "b",
            _zcode_line([_zbal(400, _epoch(3600))], plans),
            _zcode_line([], []),
        ),
        now=NOW,
    )
    assert newest_empty.available is False
    assert "no allowances" in (newest_empty.reason or "")
    drained = quota.probe_zcode(
        home=_zcode_home(tmp_path / "c", _zcode_line([_zbal(0, _epoch(3600))], plans)),
        now=NOW,
    )
    assert drained.available is False
    assert drained.reset == datetime.fromtimestamp(_epoch(3600), UTC).isoformat()
    missing = quota.probe_zcode(home=tmp_path / "none", now=NOW)
    assert missing.available is False
    assert missing.probe_ok is False


def test_zcode_reads_newest_daily_log_first(tmp_path: Path) -> None:
    plans = [{"user_plan_id": "upl_1", "status": "active"}]
    older = f"{(NOW - timedelta(days=2)).strftime('%Y-%m-%d')}.log"
    newer = f"{NOW.strftime('%Y-%m-%d')}.log"
    home = _zcode_home(
        tmp_path, _zcode_line([_zbal(10, _epoch(-5))], plans), name=older
    )
    _zcode_home(home, _zcode_line([_zbal(10, _epoch(500))], plans), name=newer)
    assert quota.probe_zcode(home=home, now=NOW).available is True


def test_quota_stage1_edge_cases(tmp_path: Path) -> None:
    # Claude Code CLI surface exhaustion skips even without a model field.
    cli_surf = {
        "rate_limits_available": True,
        "rate_limits": {
            "limits": [
                {
                    "group": "session",
                    "percent": 100,
                    "resets_at": _iso(120),
                    "scope": {"surface": {"display_name": "Claude Code"}},
                }
            ]
        },
    }
    assert quota.parse_claude_usage(cli_surf, now=NOW).available is False

    # Codex unrelated bucket falls back to ordinary `rateLimits`, never promotes unrelated bucket.
    unrelated = {
        "rateLimits": {"primary": {"usedPercent": 10, "resetsAt": _epoch(300)}},
        "rateLimitsByLimitId": {
            "other": {
                "normalModelSlug": "other-model",
                "primary": {"usedPercent": 100, "resetsAt": _epoch(300)},
            }
        },
    }
    assert quota.parse_codex_rate_limits(unrelated, now=NOW).available is True

    # Past reset times are treated as stale and do not block execution.
    stale_codex = {
        "rateLimits": {
            "primary": {"usedPercent": 100, "resetsAt": _epoch(-120)},
        }
    }
    assert quota.parse_codex_rate_limits(stale_codex, now=NOW).available is True

    # AGY does not authorize Gemini or GPT fallback when only an unrelated Claude pool is present.
    claude_only_agy = _agy_reply(0.0, 0.5)
    claude_only_agy["command"]["data"]["groups"] = [
        {
            "name": "Claude Only",
            "buckets": [
                {"window": "5h", "remaining_fraction": 0.9, "reset_time": _iso(600)}
            ],
        }
    ]
    assert quota.parse_agy_usage(claude_only_agy, now=NOW).available is False

    # ZCode plan.ends_at expiry blocks even when balance.expires_at is future; mixed expired+drained retains reset.
    ended_home = _zcode_home(
        tmp_path / "ended",
        _zcode_line(
            [_zbal(400, _epoch(3600))],
            [{"user_plan_id": "upl_1", "status": "active", "ends_at": _epoch(-60)}],
        ),
    )
    assert quota.probe_zcode(home=ended_home, now=NOW).available is False

    mixed_home = _zcode_home(
        tmp_path / "mixed",
        _zcode_line(
            [_zbal(50, _epoch(-60), "upl_old"), _zbal(0, _epoch(7200), "upl_cur")],
            [
                {"user_plan_id": "upl_old", "status": "active"},
                {"user_plan_id": "upl_cur", "status": "active"},
            ],
        ),
    )
    mixed_reading = quota.probe_zcode(home=mixed_home, now=NOW)
    assert mixed_reading.available is False
    assert mixed_reading.reason == "no active allowance with remaining units"
    assert mixed_reading.reset == datetime.fromtimestamp(_epoch(7200), UTC).isoformat()

    # Codex RPC error payloads sanitize private data instead of forwarding raw dicts.
    err_reading = quota.probe_codex(
        "/x/codex",
        now=NOW,
        rpc_fn=lambda *_a: {
            "id": 2,
            "error": {"code": -32000, "message": f"failed at {Path.home()}/secret"},
        },
    )
    assert err_reading.probe_ok is False
    assert str(Path.home()) not in (err_reading.reason or "")


# ------------------------------------------------------------ opencode/hermes


def test_opencode_probe_is_free_tier() -> None:
    reading = quota.probe_opencode(now=NOW)
    assert reading.available is True
    assert reading.model == "opencode/deepseek-v4.1-flash-free"


def test_hermes_model_from_cache_and_fallback(tmp_path: Path) -> None:
    cache = tmp_path / ".hermes" / "cache"
    cache.mkdir(parents=True)
    (cache / "nous_recommended_cache.json").write_text(
        json.dumps(
            {
                "https://portal.example": {
                    "data": {
                        "freeRecommendedModels": [{"modelName": "vendor/free-a:free"}]
                    },
                    "ts": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    reading = quota.probe_hermes(home=tmp_path, now=NOW)
    assert reading.model == "vendor/free-a:free"
    assert reading.available is True
    assert (
        quota.hermes_free_model(tmp_path / "missing")[0] == quota.HERMES_FALLBACK_MODEL
    )
    (cache / "nous_recommended_cache.json").write_text(
        json.dumps({"freeRecommendedModels": []}), encoding="utf-8"
    )
    assert quota.hermes_free_model(tmp_path)[0] == quota.HERMES_FALLBACK_MODEL


def test_iso_time_variants() -> None:
    assert quota.iso_time(0) == "1970-01-01T00:00:00+00:00"
    assert quota.iso_time("1970-01-01T00:00:00Z") == "1970-01-01T00:00:00+00:00"
    assert quota.iso_time("10") == "1970-01-01T00:00:10+00:00"
    assert quota.iso_time("2000-01-01T00:00:00") is None  # naive rejected
    assert quota.iso_time(True) is None
    assert quota.iso_time("garbage") is None


# ---------------------------------------------------------- real transports


def _fake_bin(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_rpc_and_run_capture_with_fake_processes(tmp_path: Path) -> None:
    reply = json.dumps({"id": 2, "result": {"ok": True}})
    fake = _fake_bin(
        tmp_path, "meter", f"read line\necho 'garbage'\necho '{reply}'\nsleep 30"
    )
    got = quota.rpc([str(fake)], [{"id": 1}], lambda m: m.get("id") == 2, timeout=10)
    assert got["result"] == {"ok": True}
    silent = _fake_bin(tmp_path, "silent", "sleep 30")
    with pytest.raises(ValueError):
        quota.rpc([str(silent)], [{"id": 1}], lambda _m: True, timeout=0.5)
    printer = _fake_bin(tmp_path, "printer", "echo hello")
    assert quota.run_capture([str(printer)], dict(os.environ), 10).strip() == b"hello"
    failing = _fake_bin(tmp_path, "failing", "exit 3")
    with pytest.raises(ValueError):
        quota.run_capture([str(failing)], dict(os.environ), 10)


def test_execute_captures_timeout_and_missing_binary(tmp_path: Path) -> None:
    ok = _fake_bin(tmp_path, "ok", "echo OK; echo err >&2")
    res = runner.execute([str(ok)], dict(os.environ), 10)
    assert (res.exit_code, res.stdout.strip(), res.stderr.strip()) == (0, "OK", "err")
    slow = _fake_bin(tmp_path, "slow", "sleep 30")
    res = runner.execute([str(slow)], dict(os.environ), 0.3)
    assert res.timed_out is True
    res = runner.execute([str(tmp_path / "absent")], dict(os.environ), 1)
    assert res.exit_code is None


# ------------------------------------------------------------- runner logic


def _which_all(name: str) -> str | None:
    return f"/fake/bin/{name}"


def _probe_with(reading: EnvSmokeQuotaReading):
    return lambda _h, _b, _n: reading


def test_missing_binary_is_skip_missing() -> None:
    outcome = runner.smoke_harness(
        EnvSmokeHarness.CODEX,
        mode=EnvSmokeMode.SMOKE,
        which=lambda _n: None,
        probe=lambda *_a: pytest.fail("probe must not run"),
    )
    assert outcome.status == EnvSmokeStatus.SKIP_MISSING
    assert TS_RE.fullmatch(outcome.timestamp)


def test_quota_skip_never_runs_prompt() -> None:
    reading = _reading(
        available=False, reset=_iso(60), reason="quota exhausted: Weekly"
    )
    outcome = runner.smoke_harness(
        EnvSmokeHarness.CLAUDE,
        mode=EnvSmokeMode.SMOKE,
        which=_which_all,
        probe=_probe_with(reading),
        exec_fn=lambda *_a: pytest.fail("prompt must not run"),
    )
    assert outcome.status == EnvSmokeStatus.SKIP_QUOTA
    assert f"reset={_iso(60)}" in (outcome.reason or "")
    assert outcome.returns[-1].code == EnvSmokeReturnCode.RET_SKIPPED


def test_dry_run_sends_zero_prompts_and_fails_on_broken_probe() -> None:
    def no_exec(*_a):
        pytest.fail("dry-run must not prompt")

    ok = runner.smoke_harness(
        EnvSmokeHarness.CODEX,
        mode=EnvSmokeMode.DRY_RUN,
        which=_which_all,
        probe=_probe_with(_reading(model="gpt-6-luna")),
        exec_fn=no_exec,
    )
    assert ok.status == EnvSmokeStatus.OK
    assert ok.command == []
    broken = runner.smoke_harness(
        EnvSmokeHarness.CODEX,
        mode=EnvSmokeMode.DRY_RUN,
        which=_which_all,
        probe=_probe_with(_reading(probe_ok=False, reason="meter down")),
        exec_fn=no_exec,
    )
    assert broken.status == EnvSmokeStatus.FAIL
    assert broken.errors[0].code == EnvSmokeErrorCode.ERR_QUOTA_PROBE_FAILED


@pytest.mark.parametrize(
    ("harness", "model", "expected"),
    [
        (
            EnvSmokeHarness.CLAUDE,
            "haiku",
            [
                "-p",
                "Reply with exactly: OK",
                "--model",
                "haiku",
                "--effort",
                "low",
                "--no-session-persistence",
                "--output-format",
                "text",
                "--strict-mcp-config",
            ],
        ),
        (
            EnvSmokeHarness.CODEX,
            "gpt-6-luna",
            [
                "exec",
                "-m",
                "gpt-6-luna",
                "-c",
                'model_reasoning_effort="low"',
                "--ephemeral",
                "-s",
                "read-only",
                "--skip-git-repo-check",
                "Reply with exactly: OK",
            ],
        ),
        (
            EnvSmokeHarness.AGY,
            "gemini-3.8-flash-low",
            [
                "--model",
                "gemini-3.8-flash-low",
                "--effort",
                "low",
                "--output-format",
                "json",
                "--print-timeout",
                "60s",
                "--log-file",
                "/dev/null",
                "--print=Reply with exactly: OK",
            ],
        ),
        (
            EnvSmokeHarness.OPENCODE,
            "opencode/deepseek-v4.1-flash-free",
            [
                "--standalone",
                "run",
                "--archived",
                "--format",
                "json",
                "-m",
                "opencode/deepseek-v4.1-flash-free",
                "Reply with exactly: OK",
            ],
        ),
        (
            EnvSmokeHarness.ZCODE,
            "account:zai-start-plan/GLM-5.3-Flash",
            [
                "--prompt",
                "Reply with exactly: OK",
                "--model",
                "account:zai-start-plan/GLM-5.3-Flash",
                "--effort",
                "low",
                "--json",
            ],
        ),
        (
            EnvSmokeHarness.HERMES,
            "upstage/solar-mini4:free",
            [
                "chat",
                "-q",
                "Reply with exactly: OK",
                "-m",
                "upstage/solar-mini4:free",
                "--provider",
                "nous",
                "--reasoning",
                "none",
                "--max-turns",
                "2",
                "--format",
                "stream-json",
                "--source",
                "tool",
                "--yolo",
            ],
        ),
    ],
)
def test_smoke_commands_match_contract(
    harness: EnvSmokeHarness, model: str, expected: list[str]
) -> None:
    cmd = runner.build_command(harness, "/b", model, skill_check=False, timeout=120)
    assert cmd == ["/b", *expected]
    assert "--bare" not in cmd
    skill = runner.build_command(harness, "/b", model, skill_check=True, timeout=90)
    if harness == EnvSmokeHarness.AGY:
        assert "--dangerously-skip-permissions" in skill
        assert skill[-1] == f"--print={runner.SKILL_PROMPT}"
        assert "--print" not in skill
    else:
        assert runner.SKILL_PROMPT in skill
    if harness == EnvSmokeHarness.CODEX:
        assert "read-only" not in skill
        assert "workspace-write" in skill
        assert "sandbox_workspace_write.network_access=true" in skill
    if harness == EnvSmokeHarness.HERMES:
        idx = skill.index("--max-turns")
        assert skill[idx + 1] == "16"
        r_idx = skill.index("--reasoning")
        assert skill[r_idx + 1] == "low"
    if harness == EnvSmokeHarness.OPENCODE:
        assert "--auto" in skill


def _run_one(
    harness: EnvSmokeHarness,
    result: EnvSmokeCommandResult,
    *,
    mode: EnvSmokeMode = EnvSmokeMode.SMOKE,
    reading: EnvSmokeQuotaReading | None = None,
):
    calls: list[list[str]] = []

    def fake_exec(args, env, timeout):
        calls.append(list(args))
        if harness == EnvSmokeHarness.AGY:
            assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
            assert Path(env["BROWSER"]).name == "open"
        assert not any(k.startswith("__MISE_") for k in env)
        return result

    outcome = runner.smoke_harness(
        harness,
        mode=mode,
        which=_which_all,
        probe=_probe_with(reading or _reading()),
        exec_fn=fake_exec,
    )
    return outcome, calls


def test_smoke_ok_and_reply_mismatch() -> None:
    outcome, calls = _run_one(EnvSmokeHarness.CLAUDE, _result("OK\n"))
    assert outcome.status == EnvSmokeStatus.OK
    assert calls[0][0] == "/fake/bin/claude"
    assert outcome.model == "haiku"
    for bad_out in (
        "hello",
        "not OK",
        runner.SMOKE_PROMPT,
        f"{runner.SMOKE_PROMPT}\nno",
        json.dumps(
            [
                {"role": "user", "content": runner.SMOKE_PROMPT},
                {"type": "result", "result": "no"},
            ]
        ),
    ):
        bad, _ = _run_one(EnvSmokeHarness.CODEX, _result(bad_out))
        assert bad.status == EnvSmokeStatus.FAIL
        assert bad.reason == "reply did not contain OK"
    for harness, bad_json in (
        (
            EnvSmokeHarness.AGY,
            json.dumps(
                {"event": "result", "result": {"status": "ERROR", "response": "OK"}}
            ),
        ),
        (
            EnvSmokeHarness.HERMES,
            json.dumps({"type": "result", "exit_code": 1, "text": "OK"}),
        ),
    ):
        err_smoke, _ = _run_one(harness, _result(bad_json))
        assert err_smoke.status == EnvSmokeStatus.FAIL


def test_zcode_quota_codes_do_not_override_success_or_other_harnesses() -> None:
    ok, _ = _run_one(EnvSmokeHarness.ZCODE, _result('{"text":"OK","id":1113}'))
    assert ok.status == EnvSmokeStatus.OK
    fail, _ = _run_one(
        EnvSmokeHarness.CODEX, _result(exit_code=1, stderr="providerCode: 1113")
    )
    assert fail.status == EnvSmokeStatus.FAIL
    assert fail.errors[-1].code == EnvSmokeErrorCode.ERR_COMMAND_FAILED
    # Incidental source line numbers and exit-0 messages never trigger SKIP_QUOTA / SKIP_RATE_LIMIT.
    line_fail, _ = _run_one(
        EnvSmokeHarness.ZCODE, _result(exit_code=1, stderr="error on source line 1005")
    )
    assert line_fail.status == EnvSmokeStatus.FAIL
    hermes_ok, _ = _run_one(
        EnvSmokeHarness.HERMES,
        _result(
            exit_code=0,
            stdout="\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_call",
                        "name": "terminal",
                        "tool_call_id": "call_429",
                        "arguments": json.dumps({"command": "agentsview session list"}),
                    },
                    {
                        "type": "tool_result",
                        "name": "terminal",
                        "tool_call_id": "call_429",
                        "duration_ms": 429,
                        "output": json.dumps({"output": "[]", "exit_code": 0}),
                        "is_error": False,
                    },
                ]
            ),
        ),
        mode=EnvSmokeMode.SKILL_CHECK,
    )
    assert hermes_ok.status == EnvSmokeStatus.OK


def test_opencode_and_hermes_429_map_to_skip_rate_limit() -> None:
    for harness in (EnvSmokeHarness.OPENCODE, EnvSmokeHarness.HERMES):
        outcome, _ = _run_one(
            harness,
            _result(exit_code=1, stderr="APIError: 429 Too Many Requests"),
            reading=_reading(model="m"),
        )
        assert outcome.status == EnvSmokeStatus.SKIP_RATE_LIMIT


@pytest.mark.parametrize(
    "stderr",
    [
        '{"error":{"code":"1113","message":"余额不足"}}',
        "error 1005",
        "exceed quota limit",
    ],
)
def test_zcode_quota_codes_map_to_skip_quota(stderr: str) -> None:
    outcome, _ = _run_one(
        EnvSmokeHarness.ZCODE,
        _result(exit_code=1, stderr=stderr),
        reading=_reading(model="m"),
    )
    assert outcome.status == EnvSmokeStatus.SKIP_QUOTA


def test_timeout_is_fail() -> None:
    outcome, _ = _run_one(EnvSmokeHarness.AGY, _result(exit_code=-9, timed_out=True))
    assert outcome.status == EnvSmokeStatus.FAIL
    assert outcome.errors[-1].code == EnvSmokeErrorCode.ERR_TIMEOUT


def test_agy_fallback_model_used_in_command() -> None:
    outcome, calls = _run_one(
        EnvSmokeHarness.AGY,
        _result('{"result":"OK"}'),
        reading=_reading(model=quota.AGY_FALLBACK_MODEL, reason="fallback"),
    )
    assert outcome.status == EnvSmokeStatus.OK
    assert calls[0][calls[0].index("--model") + 1] == quota.AGY_FALLBACK_MODEL
    assert EnvSmokeReturnCode.RET_MODEL_FALLBACK in {r.code for r in outcome.returns}


# ------------------------------------------------------------- skill check

CLAUDE_CLI_STREAM = "\n".join(
    json.dumps(x)
    for x in [
        {
            "type": "system",
            "subtype": "init",
            "mcp_servers": [{"name": "agentsview"}],
            "tools": ["Bash", "mcp__agentsview__search"],
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "input": {"skill": "agentsview-finding-history"},
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_bash_1",
                        "name": "Bash",
                        "input": {
                            "command": "agentsview session list --limit 1 --format json"
                        },
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_bash_1",
                        "content": "[]",
                        "is_error": False,
                    }
                ]
            },
        },
        {"type": "result", "result": "abc123"},
    ]
)
CLAUDE_MCP_STREAM = "\n".join(
    json.dumps(x)
    for x in [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp__agentsview__list_sessions",
                        "input": {},
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "agentsview session list"},
                    }
                ]
            },
        },
    ]
)
CODEX_CLI_JSONL = "\n".join(
    json.dumps(x)
    for x in [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "/bin/zsh -lc 'agentsview session list --limit 1'",
                "exit_code": 0,
            },
        },
    ]
)
CODEX_MCP_JSONL = json.dumps(
    {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "agentsview",
            "tool": "list_sessions",
        },
    }
)
OPENCODE_CLI_JSON = json.dumps(
    {
        "type": "tool_use",
        "part": {
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {
                    "command": "~/.local/share/mise/shims/agentsview session list -n 1"
                },
            },
        },
    }
)
HERMES_ARGS_JSON = "\n".join(
    json.dumps(x)
    for x in [
        {
            "type": "tool_call",
            "name": "terminal",
            "tool_call_id": "call_args",
            "arguments": json.dumps({"command": "agentsview session list --limit 1"}),
        },
        {
            "type": "tool_result",
            "name": "terminal",
            "tool_call_id": "call_args",
            "output": json.dumps({"output": "[]", "exit_code": 0}),
            "is_error": False,
        },
    ]
)
HERMES_EXEC_CODE_NOT_SHELL_JSON = "\n".join(
    json.dumps(x)
    for x in [
        {
            "type": "tool_use",
            "name": "execute_code",
            "tool_call_id": "call_py",
            "input": {
                "code": 'from hermes_tools import terminal\nprint(terminal("agentsview session list --limit 1 --json"))'
            },
        },
        {
            "type": "tool_result",
            "name": "execute_code",
            "tool_call_id": "call_py",
            "output": json.dumps(
                {
                    "status": "success",
                    "output": "{'output': '[]', 'exit_code': 0}",
                    "exit_code": 0,
                    "tool_calls_made": 1,
                }
            ),
            "is_error": False,
        },
    ]
)
HERMES_TRUNCATED_TERMINAL_JSON = "\n".join(
    json.dumps(x)
    for x in [
        {
            "type": "tool_use",
            "name": "terminal",
            "tool_call_id": "call_trunc",
            "input": {"command": "agentsview session list --bad"},
        },
        {
            "type": "tool_result",
            "name": "terminal",
            "tool_call_id": "call_trunc",
            "output": '{"output": "' + ("x" * 5000) + "...",
            "is_error": False,
        },
    ]
)
HERMES_NO_ID_FAILED_TERMINAL_JSON = "\n".join(
    json.dumps(x)
    for x in [
        {
            "type": "tool_use",
            "name": "terminal",
            "input": {"command": "agentsview session list --bad"},
        },
        {
            "type": "tool_result",
            "name": "terminal",
            "output": json.dumps({"output": "fatal", "exit_code": 64}),
            "is_error": False,
        },
    ]
)
HERMES_FAILED_TERMINAL_JSON = "\n".join(
    json.dumps(x)
    for x in [
        {
            "type": "tool_use",
            "name": "terminal",
            "tool_call_id": "call_fail",
            "input": {"command": "agentsview session list --bad"},
        },
        {
            "type": "tool_result",
            "name": "terminal",
            "tool_call_id": "call_fail",
            "output": json.dumps({"output": "fatal: bad flag", "exit_code": 64}),
            "is_error": False,
        },
    ]
)
AGY_CHATTER_ONLY = json.dumps(
    {
        "type": "message",
        "text": "I would run agentsview session list",
        "command": "echo agentsview",
    }
)
AGY_CLI_STREAM_JSON = "\n".join(
    json.dumps(x)
    for x in [
        {
            "event": "init",
            "init": {"tools": ["run_command", "call_mcp_tool"]},
        },
        {
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "agentsview list --limit 1 --json"},
                },
            },
        },
        {
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "agentsview list --limit 1 --json"},
                    "output": "Command exited with code 0.\n\nCommand output:\n[]\n",
                },
            },
        },
        {
            "event": "result",
            "result": {"status": "SUCCESS", "response": "sess_1"},
        },
    ]
)
AGY_MCP_STREAM_JSON = json.dumps(
    {
        "event": "step_update",
        "step_update": {
            "step_index": 1,
            "state": "DONE",
            "step_type": "tool",
            "tool_name": "call_mcp_tool",
            "tool_info": {
                "name": "call_mcp_tool",
                "parameters": {"ServerName": "agentsview", "ToolName": "search"},
            },
        },
    }
)
AGY_FAILED_CLI_STREAM_JSON = "\n".join(
    json.dumps(x)
    for x in [
        {
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "agentsview list --limit 1"},
                },
            },
        },
        {
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "agentsview list --limit 1"},
                    "output": "Command exited with code 127.\n",
                },
            },
        },
    ]
)
CODEX_FAILED_CLI_JSONL = json.dumps(
    {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "agentsview session list",
            "exit_code": 127,
        },
    }
)
ECHO_AGENTSVIEW_TOOL = json.dumps(
    {
        "type": "tool_use",
        "name": "Bash",
        "input": {"command": "echo agentsview"},
    }
)


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        (CLAUDE_CLI_STREAM, EnvSmokeSkillVerdict.PASS),
        (CLAUDE_MCP_STREAM, EnvSmokeSkillVerdict.FAIL_MCP_USED),
        (CODEX_CLI_JSONL, EnvSmokeSkillVerdict.PASS),
        (CODEX_MCP_JSONL, EnvSmokeSkillVerdict.FAIL_MCP_USED),
        (OPENCODE_CLI_JSON, EnvSmokeSkillVerdict.PASS),
        (HERMES_ARGS_JSON, EnvSmokeSkillVerdict.PASS),
        (HERMES_EXEC_CODE_NOT_SHELL_JSON, EnvSmokeSkillVerdict.FAIL_NO_CLI),
        (HERMES_TRUNCATED_TERMINAL_JSON, EnvSmokeSkillVerdict.FAIL_NO_CLI),
        (HERMES_NO_ID_FAILED_TERMINAL_JSON, EnvSmokeSkillVerdict.FAIL_NO_CLI),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_use",
                        "name": "terminal",
                        "tool_call_id": "call_unnamed",
                        "input": {"command": "agentsview session list"},
                    },
                    {
                        "type": "tool_result",
                        "tool_call_id": "call_unnamed",
                        "output": "",
                        "is_error": False,
                    },
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_use",
                        "name": "terminal",
                        "tool_call_id": "dup_1",
                        "input": {"command": "agentsview session list"},
                    },
                    {
                        "type": "tool_use",
                        "name": "terminal",
                        "tool_call_id": "dup_1",
                        "input": {"command": "pwd"},
                    },
                    {
                        "type": "tool_result",
                        "name": "terminal",
                        "tool_call_id": "dup_1",
                        "output": json.dumps({"output": "/tmp", "exit_code": 0}),
                        "is_error": False,
                    },
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_use",
                        "id": "e1",
                        "name": "terminal",
                        "tool_call_id": "a",
                        "input": {"command": "agentsview session list --limit 1"},
                    },
                    {
                        "type": "tool_result",
                        "id": "e2",
                        "name": "terminal",
                        "tool_call_id": "a",
                        "output": json.dumps({"output": "[]", "exit_code": 0}),
                        "is_error": False,
                    },
                ]
            ),
            EnvSmokeSkillVerdict.PASS,
        ),
        (
            json.dumps(
                {
                    "type": "tool_call",
                    "id": "c",
                    "name": "bash",
                    "input": {"command": "agentsview session list"},
                }
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_use",
                        "name": "terminal",
                        "tool_call_id": "call_true_or",
                        "input": {"command": "true || agentsview session list"},
                    },
                    {
                        "type": "tool_result",
                        "name": "terminal",
                        "tool_call_id": "call_true_or",
                        "output": json.dumps({"output": "", "exit_code": 0}),
                        "is_error": False,
                    },
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_use",
                        "name": "terminal",
                        "tool_call_id": "call_or_true",
                        "input": {"command": "agentsview session list || true"},
                    },
                    {
                        "type": "tool_result",
                        "name": "terminal",
                        "tool_call_id": "call_or_true",
                        "output": json.dumps({"output": "", "exit_code": 0}),
                        "is_error": False,
                    },
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_use",
                        "name": "terminal",
                        "tool_call_id": "call_or_both_av",
                        "input": {
                            "command": "agentsview session list --limit 1 --json || agentsview session --help"
                        },
                    },
                    {
                        "type": "tool_result",
                        "name": "terminal",
                        "tool_call_id": "call_or_both_av",
                        "output": json.dumps({"output": "[]", "exit_code": 0}),
                        "is_error": False,
                    },
                ]
            ),
            EnvSmokeSkillVerdict.PASS,
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_use",
                        "name": "terminal",
                        "tool_call_id": "call_mismatch",
                        "input": {"command": "agentsview session list"},
                    },
                    {
                        "type": "tool_result",
                        "name": "read_file",
                        "tool_call_id": "call_mismatch",
                        "output": "",
                        "is_error": False,
                    },
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            json.dumps(
                {
                    "type": "tool_result",
                    "name": "read_file",
                    "content": [
                        {
                            "event": "step_update",
                            "step_update": {
                                "state": "DONE",
                                "step_type": "tool",
                                "tool_name": "run_command",
                                "tool_info": {
                                    "name": "run_command",
                                    "parameters": {
                                        "CommandLine": "agentsview session list"
                                    },
                                    "output": "Command exited with code 0.\n",
                                },
                            },
                        }
                    ],
                }
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_call",
                        "name": "terminal",
                        "tool_call_id": "call_tc_cap",
                        "arguments": json.dumps({"command": "agentsview session list"}),
                    },
                    {
                        "type": "tool_result",
                        "name": "terminal",
                        "tool_call_id": "call_tc_cap",
                        "output": ('{"output": "' + ("x" * 5000))[:5000] + "...",
                        "is_error": False,
                    },
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (AGY_CLI_STREAM_JSON, EnvSmokeSkillVerdict.PASS),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "step_type": "tool",
                            "tool_name": "run_command",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {
                                    "CommandLine": "agentsview session list --limit 1 --json"
                                },
                                "output": "Command exited with code 0.\n\nCommand output:\nCommand exited with code 1.\n",
                            },
                        },
                    }
                ]
            ),
            EnvSmokeSkillVerdict.PASS,
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "ACTIVE",
                            "step_type": "tool",
                            "tool_name": "call_mcp_tool",
                            "tool_info": {
                                "name": "call_mcp_tool",
                                "parameters": {
                                    "ServerName": "agentsview",
                                    "ToolName": "search_content",
                                },
                            },
                        },
                    },
                    json.loads(AGY_CLI_STREAM_JSON.splitlines()[2]),
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_MCP_USED,
        ),
        (
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "state": "",
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "tool_info": {
                            "name": "run_command",
                            "parameters": {"CommandLine": "agentsview session list"},
                            "output": "",
                        },
                    },
                }
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (AGY_MCP_STREAM_JSON, EnvSmokeSkillVerdict.FAIL_MCP_USED),
        (AGY_FAILED_CLI_STREAM_JSON, EnvSmokeSkillVerdict.FAIL_NO_CLI),
        (
            {"type": "tool_call", "server": "agentsview", "tool": "search"},
            EnvSmokeSkillVerdict.FAIL_MCP_USED,
        ),
        (AGY_CHATTER_ONLY, EnvSmokeSkillVerdict.FAIL_NO_CLI),
        (CODEX_FAILED_CLI_JSONL, EnvSmokeSkillVerdict.FAIL_NO_CLI),
        (ECHO_AGENTSVIEW_TOOL, EnvSmokeSkillVerdict.FAIL_NO_CLI),
        ("plain text, no json", EnvSmokeSkillVerdict.FAIL_NO_CLI),
        *(
            (
                json.dumps(
                    {
                        "type": kind,
                        "name": alias,
                        "input": {"command": "agentsview session list"},
                    }
                ),
                EnvSmokeSkillVerdict.FAIL_NO_CLI,
            )
            for kind in ("tool_use", "tool_call", "function_call")
            for alias in (
                "bash",
                "shell",
                "sh",
                "zsh",
                "exec_command",
                "run_command",
                "command_execution",
            )
        ),
        (
            json.dumps(
                {
                    "type": "tool_call",
                    "name": "terminal",
                    "tool_call_id": "call_part_bypass",
                    "part": {},
                    "arguments": json.dumps({"command": "agentsview session list"}),
                }
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "running",
                            "input": {"command": "agentsview session list"},
                        },
                    },
                }
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_use",
                        "id": "bash_fail_meta",
                        "name": "bash",
                        "input": {"command": "agentsview session list"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "bash_fail_meta",
                        "output": "",
                        "exit_code": 1,
                        "status": "failed",
                    },
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        *(
            (
                "\n".join(
                    json.dumps(x)
                    for x in [
                        {
                            "type": "tool_use",
                            "name": "terminal",
                            "tool_call_id": "call_shell_guard",
                            "input": {"command": cmd},
                        },
                        {
                            "type": "tool_result",
                            "name": "terminal",
                            "tool_call_id": "call_shell_guard",
                            "output": json.dumps({"output": "[]", "exit_code": 0}),
                            "is_error": False,
                        },
                    ]
                ),
                expected,
            )
            for cmd, expected in (
                ("true # ; agentsview session list", EnvSmokeSkillVerdict.FAIL_NO_CLI),
                ("true;#agentsview session list", EnvSmokeSkillVerdict.FAIL_NO_CLI),
                ("agentsview session list & true", EnvSmokeSkillVerdict.FAIL_NO_CLI),
                ("agentsview session list &", EnvSmokeSkillVerdict.FAIL_NO_CLI),
                ("agentsview session list 2>&1", EnvSmokeSkillVerdict.PASS),
                ("agentsview session list |& head -5", EnvSmokeSkillVerdict.PASS),
                ("true & agentsview session list", EnvSmokeSkillVerdict.PASS),
            )
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "item.started",
                        "item": {
                            "id": "item_1",
                            "type": "command_execution",
                            "command": "agentsview session list",
                            "status": "in_progress",
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "command_execution",
                            "command": "agentsview session list",
                            "exit_code": 1,
                            "status": "failed",
                        },
                    },
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        *(
            (
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "step_type": "tool",
                            "tool_name": "run_command",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {
                                    "CommandLine": "agentsview session list"
                                },
                                "output": out_text,
                            },
                        },
                    }
                ),
                EnvSmokeSkillVerdict.FAIL_NO_CLI,
            )
            for out_text in (
                "Command exited with code 0.5\n",
                "Command exited with code 0.\nCommand exited with code 1.\n",
                "Command exited with code 0.\nCommand exited with code 0.5\n",
                f"Command exited with code 0.\nCommand exited with code {'0' * 5000}.\n",
                f"Command exited with code {'0' * 5000}.\n",
            )
        ),
        *(
            (
                json.dumps(
                    {
                        "kind": kind,
                        "name": "bash",
                        "input": {"command": "agentsview session list"},
                    }
                ),
                EnvSmokeSkillVerdict.FAIL_NO_CLI,
            )
            for kind in ("tool_use", "tool_call", "function_call")
        ),
        (
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "tool": "bash",
                        "state": {"input": {"command": "agentsview session list"}},
                    },
                }
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_use",
                        "id": "bash_json_fail",
                        "name": "bash",
                        "input": {"command": "agentsview session list"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "bash_json_fail",
                        "output": json.dumps(
                            {
                                "output": "[]",
                                "exit_code": 0,
                                "error": "fatal",
                                "metadata": {"exit": 1},
                            }
                        ),
                    },
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        *(
            (
                "\n".join(
                    json.dumps(x)
                    for x in [
                        {
                            "type": "tool_use",
                            "name": "terminal",
                            "tool_call_id": "call_shell_ext",
                            "input": {"command": cmd},
                        },
                        {
                            "type": "tool_result",
                            "name": "terminal",
                            "tool_call_id": "call_shell_ext",
                            "output": json.dumps({"output": "[]", "exit_code": 0}),
                            "is_error": False,
                        },
                    ]
                ),
                EnvSmokeSkillVerdict.FAIL_NO_CLI,
            )
            for cmd in (
                "true ||\n agentsview session list",
                "true || # comment\n agentsview session list",
                r"agentsview a\ #x & true",
                "agentsview missing \\|\ntrue",
                "cat <<agentsview\nfixture\nagentsview",
                'cat <<""\nagentsview session list\n\n',
                'bash -n -c "agentsview session list"',
                'bash /dev/null -c "agentsview session list"',
                "agentsview missing | true",
                "env -u agentsview true",
                "env --unset agentsview true",
                "env -S true agentsview session list",
                "env --help agentsview",
            )
        ),
        *(
            (
                "\n".join(
                    json.dumps(x)
                    for x in [
                        {
                            "type": "tool_use",
                            "name": "terminal",
                            "tool_call_id": "call_shell_ok2",
                            "input": {"command": cmd},
                        },
                        {
                            "type": "tool_result",
                            "name": "terminal",
                            "tool_call_id": "call_shell_ok2",
                            "output": json.dumps({"output": "[]", "exit_code": 0}),
                            "is_error": False,
                        },
                    ]
                ),
                EnvSmokeSkillVerdict.PASS,
            )
            for cmd in (
                "true || false && agentsview session list",
                "cat <<'EOF'\nignore\nEOF\nagentsview session list",
            )
        ),
        (
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "bash",
                        "error": "fatal",
                        "state": {
                            "status": "completed",
                            "input": {"command": "agentsview session list"},
                        },
                    },
                }
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "other",
                        "tool": "bash",
                        "state": {
                            "status": "running",
                            "input": {"command": "agentsview session list"},
                        },
                    },
                }
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            "\n".join(
                [
                    *AGY_CLI_STREAM_JSON.splitlines()[:-1],
                    json.dumps(
                        {
                            "event": "result",
                            "result": {"status": "ERROR", "response": "OK"},
                        }
                    ),
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            json.dumps(
                [
                    json.loads(OPENCODE_CLI_JSON),
                    {
                        "event": "result",
                        "result": {"status": "ERROR", "response": "OK"},
                    },
                ]
            ),
            EnvSmokeSkillVerdict.FAIL_NO_CLI,
        ),
        (
            "\n".join(
                json.dumps(x)
                for x in [
                    {
                        "type": "tool_use",
                        "name": "terminal",
                        "tool_call_id": "call_retry_1",
                        "input": {"command": "agentsview session search"},
                    },
                    {
                        "type": "tool_result",
                        "name": "terminal",
                        "tool_call_id": "call_retry_1",
                        "output": json.dumps({"output": "bad", "exit_code": 65}),
                        "is_error": True,
                    },
                    {
                        "type": "tool_use",
                        "name": "terminal",
                        "tool_call_id": "call_retry_2",
                        "input": {"command": "agentsview session list --limit 1"},
                    },
                    {
                        "type": "tool_result",
                        "name": "terminal",
                        "tool_call_id": "call_retry_2",
                        "output": json.dumps({"output": "[]", "exit_code": 0}),
                        "is_error": False,
                    },
                    {"type": "result", "session_id": "s_retry", "exit_code": 0},
                ]
            ),
            EnvSmokeSkillVerdict.PASS,
        ),
    ],
)
def test_analyze_skill_output_cli_vs_mcp(
    text: str | dict[str, str], verdict: EnvSmokeSkillVerdict
) -> None:
    raw = json.dumps(text) if isinstance(text, dict) else text
    assert runner.analyze_skill_output(raw).verdict == verdict


def test_analyze_skill_output_records_calls() -> None:
    check = runner.analyze_skill_output(CODEX_MCP_JSONL + "\n" + CODEX_CLI_JSONL)
    assert check.mcp_calls == ["agentsview/list_sessions"]
    assert check.cli_calls == ["/bin/zsh -lc 'agentsview session list --limit 1'"]


def test_rehydrate_hermes_capped_output(tmp_path: Path) -> None:
    import sqlite3

    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir(parents=True)
    db = hermes_dir / "state.db"
    sid = "local-cap-test"
    full_ok = json.dumps({"output": "x" * 5500, "exit_code": 0})
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, tool_name TEXT, tool_call_id TEXT, content TEXT)"
        )
        conn.execute(
            "INSERT INTO messages VALUES (1, ?, 'tool', 'terminal', 'call_cap', ?)",
            (sid, full_ok),
        )
    capped = "\n".join(
        json.dumps(x)
        for x in [
            {
                "type": "tool_use",
                "name": "terminal",
                "tool_call_id": "call_cap",
                "input": {"command": "agentsview session list --json"},
            },
            {
                "type": "tool_result",
                "name": "terminal",
                "tool_call_id": "call_cap",
                "output": full_ok[:5000] + "...",
                "is_error": False,
            },
            {"type": "result", "session_id": sid, "exit_code": 0, "text": "s1"},
        ]
    )
    assert (
        runner.analyze_skill_output(capped).verdict == EnvSmokeSkillVerdict.FAIL_NO_CLI
    )
    hydrated = runner._rehydrate_hermes_capped_output(capped, home=tmp_path)
    assert hydrated is not None
    assert runner.analyze_skill_output(hydrated).verdict == EnvSmokeSkillVerdict.PASS

    yielded = json.dumps(
        {"output": "x" * 5500, "exit_code": None, "status": "yielded_to_background"}
    )
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE messages SET content = ? WHERE id = 1", (yielded,))
    hydrated_yield = runner._rehydrate_hermes_capped_output(capped, home=tmp_path)
    assert hydrated_yield is not None
    assert (
        runner.analyze_skill_output(hydrated_yield).verdict
        == EnvSmokeSkillVerdict.FAIL_NO_CLI
    )

    # ID-less rehydration requires an exact 5,000-byte prefix match (never positional mix-up).
    db_noid = hermes_dir / "state.db"
    other_ok = json.dumps({"output": "a" * 5500, "exit_code": 0})
    fail_matching = json.dumps({"output": "x" * 5500, "exit_code": 2})
    with sqlite3.connect(db_noid) as conn:
        conn.execute("DELETE FROM messages")
        conn.execute(
            "INSERT INTO messages VALUES (1, ?, 'tool', 'terminal', NULL, ?)",
            (sid, other_ok),
        )
        conn.execute(
            "INSERT INTO messages VALUES (2, ?, 'tool', 'terminal', NULL, ?)",
            (sid, fail_matching),
        )
    capped_noid = "\n".join(
        json.dumps(x)
        for x in [
            {
                "type": "tool_use",
                "name": "terminal",
                "input": {"command": "agentsview session list --json"},
            },
            {
                "type": "tool_result",
                "name": "terminal",
                "output": fail_matching[:5000] + "...",
                "is_error": False,
            },
            {"type": "result", "session_id": sid, "exit_code": 0, "text": "s1"},
        ]
    )
    hydrated_noid = runner._rehydrate_hermes_capped_output(capped_noid, home=tmp_path)
    assert hydrated_noid is not None
    assert (
        runner.analyze_skill_output(hydrated_noid).verdict
        == EnvSmokeSkillVerdict.FAIL_NO_CLI
    )

    # UTF-8 5,000-byte multibyte cap (2,500 'é' characters = 5,000 bytes, len=2503) rehydrates cleanly.
    utf8_ok = json.dumps({"output": "é" * 2600, "exit_code": 0}, ensure_ascii=False)
    utf8_prefix = (
        utf8_ok.encode("utf-8")[:5000].decode("utf-8", errors="ignore") + "..."
    )
    with sqlite3.connect(db_noid) as conn:
        conn.execute("DELETE FROM messages")
        conn.execute(
            "INSERT INTO messages VALUES (1, ?, 'tool', 'terminal', 'call_utf8', ?)",
            (sid, utf8_ok),
        )
    capped_utf8 = "\n".join(
        json.dumps(x, ensure_ascii=False)
        for x in [
            {
                "type": "tool_use",
                "name": "terminal",
                "tool_call_id": "call_utf8",
                "input": {"command": "agentsview session list --json"},
            },
            {
                "type": "tool_result",
                "name": "terminal",
                "tool_call_id": "call_utf8",
                "output": utf8_prefix,
                "is_error": False,
            },
            {"type": "result", "session_id": sid, "exit_code": 0, "text": "s1"},
        ]
    )
    hydrated_utf8 = runner._rehydrate_hermes_capped_output(capped_utf8, home=tmp_path)
    assert hydrated_utf8 is not None
    assert (
        runner.analyze_skill_output(hydrated_utf8).verdict == EnvSmokeSkillVerdict.PASS
    )

    # Split multibyte char at byte 5,000 leaves 4,997-4,999 bytes (4-byte emoji cut -> 4,997 bytes).
    utf8_split_ok = json.dumps(
        {"output": ("a" * 4985) + "🚀" + ("z" * 200), "exit_code": 0},
        ensure_ascii=False,
    )
    utf8_split_prefix = (
        utf8_split_ok.encode("utf-8")[:5000].decode("utf-8", errors="ignore") + "..."
    )
    assert len(utf8_split_prefix.encode("utf-8")) == 5000  # 4997 prefix + 3 '...'
    with sqlite3.connect(db_noid) as conn:
        conn.execute("DELETE FROM messages")
        conn.execute(
            "INSERT INTO messages VALUES (1, ?, 'tool', 'terminal', 'call_split', ?)",
            (sid, utf8_split_ok),
        )
    capped_split = "\n".join(
        json.dumps(x, ensure_ascii=False)
        for x in [
            {
                "type": "tool_use",
                "name": "terminal",
                "tool_call_id": "call_split",
                "input": {"command": "agentsview session list --json"},
            },
            {
                "type": "tool_result",
                "name": "terminal",
                "tool_call_id": "call_split",
                "output": utf8_split_prefix,
                "is_error": False,
            },
            {"type": "result", "session_id": sid, "exit_code": 0, "text": "s1"},
        ]
    )
    hydrated_split = runner._rehydrate_hermes_capped_output(capped_split, home=tmp_path)
    assert hydrated_split is not None
    assert (
        runner.analyze_skill_output(hydrated_split).verdict == EnvSmokeSkillVerdict.PASS
    )

    # Oversized (>5000 char ASCII) exact prefix of full_ok isolates rejection strictly by size.
    with sqlite3.connect(db_noid) as conn:
        conn.execute("DELETE FROM messages")
        conn.execute(
            "INSERT INTO messages VALUES (1, ?, 'tool', 'terminal', 'call_cap', ?)",
            (sid, full_ok),
        )
    oversized = "\n".join(
        json.dumps(x)
        for x in [
            {
                "type": "tool_use",
                "name": "terminal",
                "tool_call_id": "call_cap",
                "input": {"command": "agentsview session list"},
            },
            {
                "type": "tool_result",
                "name": "terminal",
                "tool_call_id": "call_cap",
                "output": full_ok[:5001] + "...",
                "is_error": False,
            },
            {"type": "result", "session_id": sid, "exit_code": 0, "text": "s1"},
        ]
    )
    assert len(json.loads(oversized.splitlines()[1])["output"]) == 5004
    assert runner._rehydrate_hermes_capped_output(oversized, home=tmp_path) is None

    # Duplicate DB rows sharing one tool_call_id fail closed when second row has NULL or different content.
    dup_diff_fail = json.dumps({"output": "z" * 5500, "exit_code": 1})
    with sqlite3.connect(db_noid) as conn:
        conn.execute("DELETE FROM messages")
        conn.execute(
            "INSERT INTO messages VALUES (1, ?, 'tool', 'terminal', 'call_cap', ?)",
            (sid, full_ok),
        )
        conn.execute(
            "INSERT INTO messages VALUES (2, ?, 'tool', 'terminal', 'call_cap', NULL)",
            (sid,),
        )
    assert runner._rehydrate_hermes_capped_output(capped, home=tmp_path) is None
    with sqlite3.connect(db_noid) as conn:
        conn.execute(
            "UPDATE messages SET content = ? WHERE id = 2",
            (dup_diff_fail,),
        )
    assert runner._rehydrate_hermes_capped_output(capped, home=tmp_path) is None
    assert (
        runner.analyze_skill_output(capped).verdict == EnvSmokeSkillVerdict.FAIL_NO_CLI
    )

    # Duplicate DB rows sharing both tool_call_id and capped prefix also fail closed.
    dup_same_fail = json.dumps({"output": "x" * 5500, "exit_code": 1})
    with sqlite3.connect(db_noid) as conn:
        conn.execute("DELETE FROM messages")
        conn.execute(
            "INSERT INTO messages VALUES (1, ?, 'tool', 'terminal', 'call_cap', ?)",
            (sid, full_ok),
        )
        conn.execute(
            "INSERT INTO messages VALUES (2, ?, 'tool', 'terminal', 'call_cap', ?)",
            (sid, dup_same_fail),
        )
    assert runner._rehydrate_hermes_capped_output(capped, home=tmp_path) is None
    assert (
        runner.analyze_skill_output(capped).verdict == EnvSmokeSkillVerdict.FAIL_NO_CLI
    )

    # Escaped lone surrogate in output or session_id never raises UnicodeEncodeError.
    surrogate = "\n".join(
        [
            json.dumps(
                {
                    "type": "tool_use",
                    "name": "terminal",
                    "tool_call_id": "call_surr",
                    "input": {"command": "agentsview session list"},
                }
            ),
            '{"type": "tool_result", "name": "terminal", "tool_call_id": "call_surr", "output": "\\ud800...", "is_error": false}',
            json.dumps({"type": "result", "session_id": sid, "exit_code": 0}),
        ]
    )
    assert runner._rehydrate_hermes_capped_output(surrogate, home=tmp_path) is None
    assert (
        runner.analyze_skill_output(surrogate).verdict
        == EnvSmokeSkillVerdict.FAIL_NO_CLI
    )
    surrogate_sid = "\n".join(
        [
            json.dumps(
                {
                    "type": "tool_use",
                    "name": "terminal",
                    "tool_call_id": "call_cap",
                    "input": {"command": "agentsview session list"},
                }
            ),
            json.dumps(
                {
                    "type": "tool_result",
                    "name": "terminal",
                    "tool_call_id": "call_cap",
                    "output": full_ok[:5000] + "...",
                    "is_error": False,
                }
            ),
            '{"type": "result", "session_id": "\\ud800", "exit_code": 0}',
        ]
    )
    assert runner._rehydrate_hermes_capped_output(surrogate_sid, home=tmp_path) is None


def test_skill_check_mode_pass_and_fail() -> None:
    ok, calls = _run_one(
        EnvSmokeHarness.CLAUDE,
        _result(CLAUDE_CLI_STREAM),
        mode=EnvSmokeMode.SKILL_CHECK,
    )
    assert ok.status == EnvSmokeStatus.OK
    assert ok.skill_check is not None
    assert ok.skill_check.verdict == EnvSmokeSkillVerdict.PASS
    assert "stream-json" in calls[0]
    assert "--strict-mcp-config" not in calls[0]
    bad, _ = _run_one(
        EnvSmokeHarness.CODEX, _result(CODEX_MCP_JSONL), mode=EnvSmokeMode.SKILL_CHECK
    )
    assert bad.status == EnvSmokeStatus.FAIL
    assert bad.errors[-1].code == EnvSmokeErrorCode.ERR_SKILL_CHECK_FAILED
    agy_err_stream = "\n".join(
        [
            *AGY_CLI_STREAM_JSON.splitlines()[:-1],
            json.dumps(
                {"event": "result", "result": {"status": "ERROR", "response": "OK"}}
            ),
        ]
    )
    agy_err, _ = _run_one(
        EnvSmokeHarness.AGY, _result(agy_err_stream), mode=EnvSmokeMode.SKILL_CHECK
    )
    assert agy_err.status == EnvSmokeStatus.FAIL
    assert agy_err.errors[-1].code == EnvSmokeErrorCode.ERR_SKILL_CHECK_FAILED


# ----------------------------------------------------------- aggregate + cli


def test_parse_harnesses() -> None:
    assert runner.parse_harnesses(None) == list(EnvSmokeHarness)
    assert runner.parse_harnesses(["claude,codex", "agy", "codex"]) == [
        EnvSmokeHarness.CLAUDE,
        EnvSmokeHarness.CODEX,
        EnvSmokeHarness.AGY,
    ]
    with pytest.raises(ValueError):
        runner.parse_harnesses(["gemini"])


def test_run_smoke_aggregates_and_rc() -> None:
    def probe(harness, _b, _n):
        if harness == EnvSmokeHarness.CLAUDE:
            return _reading(available=False, reason="exhausted")
        return _reading(model="m")

    def fake_exec(args, _env, _t):
        return _result("nope") if "/fake/bin/codex" in args else _result("OK")

    resp = runner.run_smoke(
        list(EnvSmokeHarness),
        mode=EnvSmokeMode.SMOKE,
        which=lambda n: None if n == "hermes" else _which_all(n),
        probe=probe,
        exec_fn=fake_exec,
    )
    s = resp.summary
    assert (s.total, s.ok, s.skip_quota, s.skip_missing, s.fail) == (6, 3, 1, 1, 1)
    assert resp.rc == 1
    assert resp.returns[0].code == EnvSmokeReturnCode.RET_PARTIAL
    assert [o.harness for o in resp.outcomes] == list(EnvSmokeHarness)
    assert TS_RE.fullmatch(resp.timestamp)
    EnvSmokeResponse.model_validate(resp.model_dump(mode="json"))


def test_run_smoke_isolates_crashing_harness() -> None:
    def probe(harness, _b, _n):
        if harness == EnvSmokeHarness.ZCODE:
            raise RuntimeError("kaboom")
        return _reading()

    resp = runner.run_smoke(
        [EnvSmokeHarness.ZCODE, EnvSmokeHarness.OPENCODE],
        mode=EnvSmokeMode.DRY_RUN,
        which=_which_all,
        probe=probe,
    )
    assert [o.status for o in resp.outcomes] == [EnvSmokeStatus.FAIL, EnvSmokeStatus.OK]
    assert resp.rc == 1


def test_cli_main_json_dry_run(monkeypatch, capsys, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_smoke(harnesses, *, mode, timeout):
        captured.update(harnesses=harnesses, mode=mode, timeout=timeout)
        return runner.run_smoke(
            harnesses, mode=mode, which=lambda _n: None, probe=lambda *_a: _reading()
        )

    monkeypatch.setattr(cli, "run_smoke", fake_run_smoke)
    rc = cli.main(
        [
            "--harness",
            "claude,zcode",
            "--dry-run",
            "--json",
            "--timeout",
            "30",
            "--state-dir",
            str(tmp_path),
            "--write-log",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert captured["mode"] == EnvSmokeMode.DRY_RUN
    assert captured["timeout"] == 30.0
    assert payload["command"] == "env:smoke"
    assert payload["mode"] == "dry-run"
    assert captured["harnesses"] == [EnvSmokeHarness.CLAUDE, EnvSmokeHarness.ZCODE]
    assert {o["status"] for o in payload["outcomes"]} == {"SKIP_MISSING"}
    for key in ("timestamp", "timestamp_unix_ns", "errors", "returns"):
        assert key in payload
        assert key in payload["summary"]
    assert (tmp_path / "env-smoke.log.jsonl").is_file()
    assert cli.main(["--harness", "bogus"]) == 2
    assert cli.main(["--harness", ","]) == 2
    assert cli.main(["--timeout", "0"]) == 2
    assert cli.main(["--timeout", "nan"]) == 2
    assert cli.main(["--timeout", "inf"]) == 2


def test_task_wrapper_help_and_fake_harness_dry_run(tmp_path: Path) -> None:
    help_proc = subprocess.run(
        ["/bin/bash", str(TASK), "--help"], capture_output=True, text=True, check=True
    )
    assert "env-smoke" in help_proc.stdout
    assert "--dry-run" in help_proc.stdout

    # Default empty invocation on macOS /bin/bash must not abort on unbound `args[@]`.
    empty_proc = subprocess.run(
        ["/bin/bash", str(TASK)],
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_dry_run": "true",
            "usage_json": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    empty_payload = json.loads(empty_proc.stdout)
    assert [o["harness"] for o in empty_payload["outcomes"]] == [
        h.value for h in EnvSmokeHarness
    ]

    bin_dir = tmp_path / "bin"
    usage = {
        "type": "control_response",
        "response": {
            "request_id": quota.CLAUDE_REQUEST_ID,
            "response": {"rate_limits_available": False},
        },
    }
    _fake_bin(bin_dir, "claude", f"echo '{json.dumps(usage)}'; sleep 30")
    codex_reply = {"id": 2, "result": _codex_result(primary=100)}
    _fake_bin(bin_dir, "codex", f"echo '{json.dumps(codex_reply)}'; sleep 30")
    _fake_bin(bin_dir, "agy", f"echo '{json.dumps(_agy_reply(0.5, 0.5))}'")
    _fake_bin(bin_dir, "opencode", "echo should-not-run; exit 9")
    env = {
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "usage_harness": "claude,agy",
        "usage_dry_run": "true",
        "usage_json": "true",
    }
    proc = subprocess.run(
        ["/bin/bash", str(TASK)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    payload = json.loads(proc.stdout)
    status = {o["harness"]: o["status"] for o in payload["outcomes"]}
    assert status == {
        "claude": "OK",
        "agy": "OK",
    }
    assert all(o["command"] == [] for o in payload["outcomes"])
    assert payload["rc"] == 0
    assert payload["returns"][0]["code"] == "RET_DRY_RUN"


def test_module_imports_without_side_effects(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import env_smoke.cli, env_smoke.quota, env_smoke.runner; "
            "assert env_smoke.runner.SMOKE_PROMPT == 'Reply with exactly: OK'",
            str(REPO_ROOT / "unix" / "home" / ".config" / "mise" / "lib"),
        ],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.returncode == 0


def test_resolve_binary_harness_install_dirs_and_model_fallbacks(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("PATH", "")
    oc = tmp_path / ".opencode" / "bin" / "opencode"
    oc.parent.mkdir(parents=True)
    oc.write_text("#!/bin/sh\n", encoding="utf-8")
    oc.chmod(0o755)
    hm = tmp_path / ".local" / "share" / "hermes-cli.noindex" / "bin" / "hermes"
    hm.parent.mkdir(parents=True)
    hm.write_text("#!/bin/sh\n", encoding="utf-8")
    hm.chmod(0o755)

    assert runner.resolve_binary("opencode") == str(oc)
    assert runner.resolve_binary("hermes") == str(hm)

    out = runner.smoke_harness(
        EnvSmokeHarness.HERMES,
        mode=EnvSmokeMode.SMOKE,
        which=lambda _n: str(hm),
        probe=lambda _h, _b, _now: _reading(model=None),
        exec_fn=lambda cmd, _env, _t: (
            _result("OK")
            if quota.HERMES_FALLBACK_MODEL in cmd
            else _result("missing model", exit_code=1)
        ),
        now=NOW,
    )
    assert out.status == EnvSmokeStatus.OK
    assert out.model == quota.HERMES_FALLBACK_MODEL
