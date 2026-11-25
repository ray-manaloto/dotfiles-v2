"""Offline contract and prompt-gating tests for the standalone quota driver."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from dotfiles_py.generated.env_quota_schema import EnvQuotaResponse
from env_quota import cli
from env_smoke import quota

ROOT = Path(__file__).resolve().parents[1]
DOCTOR = (ROOT / "tasks/env/doctor").read_text()


def test_rpc_is_usage_only(monkeypatch, capsys, tmp_path):
    def fake_rpc(args, messages, accept):
        assert args[1:] == list(quota.CLAUDE_USAGE_ARGS)
        assert len(messages) == 1
        assert messages[0]["type"] == "control_request"
        assert messages[0]["request"]["subtype"] == "get_usage"
        return {
            "response": {
                "response": {
                    "rate_limits_available": True,
                    "rate_limits": {"five_hour": {"utilization": 100}},
                }
            }
        }

    monkeypatch.setattr(quota, "rpc", fake_rpc)
    assert (
        cli.main(
            [
                "--json",
                "--harness",
                "claude,opencode",
                "--write-log",
                "--state-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    response = EnvQuotaResponse.model_validate(data)
    assert [r.harness.value for r in response.readings] == ["claude", "opencode"]
    assert not response.readings[0].quota.available
    assert response.readings[1].quota.available
    assert (
        response.timestamp.split(".")[1][:9]
        == f"{response.timestamp_unix_ns % 10**9:09d}"
    )
    assert data["errors"] == [] and data["returns"][0]["code"] == "RET_OK"
    event = json.loads((tmp_path / "env-quota.log.jsonl").read_text().splitlines()[-1])
    assert event["response"] == data
    for bad in (
        {"timestamp": "bad"},
        {"timestamp_unix_ns": -1},
        {"returns": [{"code": "BAD"}]},
        {"errors": [{"code": "ERR_QUOTA_PROBE_FAILED", "extra": 1}]},
    ):
        with pytest.raises(ValidationError):
            EnvQuotaResponse.model_validate(data | bad)
    assert cli.main(["--harness", "claude"]) == 0
    assert "claude: SKIP_QUOTA(resets unknown)" in capsys.readouterr().out


def test_unknown_quota_is_visible(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["--harness", "zcode", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["returns"][0]["code"] == "RET_PARTIAL"
    assert data["errors"][0]["detail"] == {"harness": "zcode"}
    assert not data["readings"][0]["quota"]["probe_ok"]


@pytest.mark.parametrize("smoke", [True, False])
def test_doctor_host_snapshot_before_parallel_targets(tmp_path, smoke):
    calls = tmp_path / "calls"
    harnesses = ["claude", "codex", "agy", "opencode", "zcode", "hermes"]
    readings = [
        {
            "harness": name,
            "quota": {
                "available": name not in ("claude", "zcode"),
                "reset": "next-week" if name == "claude" else None,
            },
        }
        for name in harnesses
    ]
    mise = tmp_path / "mise"
    mise.write_text(
        '#!/bin/bash\necho "$*" >> "$CALLS"\nif [[ "$2" == env:quota ]]; then echo "$DATA"; fi\n'
    )
    mise.chmod(0o755)
    # Replace target infrastructure checks, retaining the real gate, fan-out and host smoke.
    probes = """
check_container() { echo "$1:$skip_claude:$skip_opencode:$skip_zcode" >> "$CALLS"; }
check_vm() { check_container vm; }
"""
    script = (
        DOCTOR[: DOCTOR.index("check_container()")]
        + probes
        + DOCTOR[DOCTOR.index("run_doctor_target()") :]
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            PATH=f"{tmp_path}:{os.environ['PATH']}",
            DATA=json.dumps({"readings": readings}),
            CALLS=str(calls),
            usage_smoke=str(smoke).lower(),
            usage_target="all",
        ),
    )
    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    if smoke:
        assert lines[0] == "run env:quota --json"
        assert sorted(lines[1:-1]) == [
            f"{target}:SKIP_QUOTA(claude resets next-week)::SKIP_QUOTA(zcode resets unknown)"
            for target in ("mac", "pc", "vm")
        ]
        assert lines[-1] == "run env:smoke --harness codex,agy,opencode,hermes"
    else:
        assert sorted(lines) == ["mac:::", "pc:::", "vm:::"]


@pytest.mark.parametrize("delimiter", ["CONTAINER", "GUEST"])
def test_guest_skips_exhausted_prompts(delimiter):
    # Exercise the actual guest shell fragment without contacting either transport.
    guest = DOCTOR.split(f"<<'{delimiter}'", 1)[1].split(f"\n{delimiter}\n", 1)[0]
    block = guest[guest.index('smoke_suffix=""') : guest.rindex("\nprintf")]
    result = subprocess.run(
        ["/bin/bash", "-euc", block + '\necho "$smoke_suffix"'],
        text=True,
        capture_output=True,
        env={
            "SMOKE": "true",
            "SKIP_CLAUDE": "SKIP_QUOTA(claude resets soon)",
            "SKIP_OPENCODE": "SKIP_QUOTA(opencode resets unknown)",
            "SKIP_ZCODE": "SKIP_QUOTA(zcode resets later)",
            "PATH": "/nonexistent",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "claude-smoke=SKIP_QUOTA(claude resets soon)" in result.stdout
    assert "zcode-smoke=SKIP_QUOTA(zcode resets later)" in result.stdout
