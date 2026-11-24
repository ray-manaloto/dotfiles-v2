"""Offline unit tests and latency benchmarks for Step 4 UI/TUI, Herdr mods, statusLine, and dead Codex [otel] cleanup."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from tasks_py_loader import load_module

from dotfiles_py.generated import (
    AgySessionStatus,
    EnvStatsReturnCode,
    EnvStatsSnapshotResponse,
    EnvTargetName,
    EnvTargetState,
)
from env_stats.collector import collect_env_stats

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = REPO_ROOT / "tasks"
MISE_TASKS_DIR = REPO_ROOT / "unix" / "home" / ".config" / "mise" / "tasks"
AGY_STATUSLINE = REPO_ROOT / "unix" / "home" / ".local" / "bin" / "agy-statusline"


def _make_stub(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_bottom_locked_and_env_top_targets(tmp_path: Path) -> None:
    cfg_txt = (
        REPO_ROOT / "unix" / "home" / ".config" / "mise" / "config.toml"
    ).read_text(encoding="utf-8")
    cfg = tomllib.loads(cfg_txt)
    tools = cfg["tools"]
    assert tools.get("bottom") == "0.15.0"
    for forbidden in ("btop", "beszel", "macmon", "alloy", "otelcol"):
        assert forbidden not in tools

    lock_path = REPO_ROOT / "unix" / "home" / ".config" / "mise" / "mise.lock"
    assert (stat.S_IMODE(lock_path.stat().st_mode) & 0o044) == 0o044
    lock_data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    bottom_entries = lock_data["tools"]["bottom"]
    assert len(bottom_entries) == 1
    assert bottom_entries[0]["version"] == "0.15.0"
    assert bottom_entries[0]["backend"] == "aqua:ClementTsang/bottom"
    for plat in ("linux-arm64", "linux-x64", "macos-arm64"):
        assert f"platforms.{plat}" in bottom_entries[0]

    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    call_log = tmp_path / "top-calls.log"
    _make_stub(
        bin_stub,
        "btm",
        f'printf "btm %s\\n" "$*" >> "{call_log}"\nexit 0\n',
    )
    _make_stub(
        bin_stub,
        "ssh",
        f'if [ "$1" = "-n" ]; then exit 0; fi\nprintf "ssh %s\\n" "$*" >> "{call_log}"\nexit 0\n',
    )
    _make_stub(
        bin_stub,
        "tailscale",
        'if [ "$1" = "status" ]; then echo \'{"Peer":[]}\'; fi\nexit 0\n',
    )
    _make_stub(
        bin_stub,
        "docker",
        f'printf "docker %s\\n" "$*" >> "{call_log}"\nexit 0\n',
    )
    if sys.platform == "darwin":
        _make_stub(
            bin_stub,
            "tart",
            f'printf "tart %s\\n" "$*" >> "{call_log}"\nexit 0\n',
        )

    top_task = TASKS_DIR / "env" / "top"
    base_env = {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
    }

    for tgt in ("host", "pc-host", "pc", "mac", "vm"):
        res = subprocess.run(
            ["bash", str(top_task)],
            env={**base_env, "usage_target": tgt},
            capture_output=True,
            text=True,
            check=True,
        )
        assert res.returncode == 0

    log_lines = call_log.read_text(encoding="utf-8").splitlines()
    assert any("btm --basic" in line for line in log_lines)
    assert any("pc /usr/bin/btop" in line for line in log_lines)
    assert any(
        "dev@dotfiles-v2-pc" in line and "btm --basic" in line for line in log_lines
    )
    assert any(
        "dev@dotfiles-v2-mac" in line and "btm --basic" in line for line in log_lines
    )
    assert any(
        "admin@dotfiles-v2-vm" in line and "btm --basic" in line for line in log_lines
    )

    # --exec forces docker exec on pc/mac
    exec_pc = subprocess.run(
        ["bash", str(top_task)],
        env={**base_env, "usage_target": "pc", "usage_exec": "true"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert exec_pc.returncode == 0
    assert any(
        "docker exec -i dotfiles-v2 /bin/bash -lc" in line
        for line in call_log.read_text(encoding="utf-8").splitlines()
    )

    bad = subprocess.run(
        ["bash", str(top_task)],
        env={**base_env, "usage_target": "invalid-target"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1


def test_dead_codex_otel_cleanup_and_doctor_guard(tmp_path: Path, monkeypatch) -> None:
    doc_mod = load_module("doctor_checks")
    fake_home = tmp_path / "home"
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir(parents=True)
    codex_cfg = codex_dir / "config.toml"
    codex_cfg.write_text(
        'model = "gpt-6.2-astra"\n\n'
        "[otel]\n"
        '  environment = "symphony-cpp-local"\n'
        "  log_user_prompt = false\n\n"
        "    [otel.exporter.otlp-http]\n"
        '      endpoint = "http://127.0.0.1:4318/v1/logs"\n'
        '      protocol = "binary"\n\n'
        "[notice]\n"
        "hide_rate_limit_model_nudge = true\n",
        encoding="utf-8",
    )

    assert doc_mod.find_dead_codex_otel(codex_cfg) != []

    # Check host hygiene catches dead-codex-otel cleanly in isolated fake_home.
    (fake_home / ".agents" / "skills").mkdir(parents=True)
    (fake_home / ".agents" / "skills" / ".mise-skills.json").write_text(
        '{"links":{}}', encoding="utf-8"
    )
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "mise_data"))
    monkeypatch.setenv("MISE_STATE_DIR", str(tmp_path / "mise_state"))
    monkeypatch.setattr(doc_mod.shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(
        doc_mod.subprocess,
        "check_output",
        lambda cmd, **_kw: "[]" if "skills" in cmd else "",
    )
    hygiene_errs, _ = doc_mod.check_host_hygiene(REPO_ROOT)
    assert any("dead-codex-otel:" in err for err in hygiene_errs)

    prune_task = MISE_TASKS_DIR / "host" / "prune-mcps"
    chk = subprocess.run(
        ["bash", str(prune_task)],
        env={
            "HOME": str(fake_home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_check": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "FOUND codex-dead-otel" in chk.stdout
    assert "[otel]" in codex_cfg.read_text(encoding="utf-8")

    app = subprocess.run(
        ["bash", str(prune_task)],
        env={
            "HOME": str(fake_home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_apply": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "PRUNED codex" in app.stdout
    after = codex_cfg.read_text(encoding="utf-8")
    assert "[otel]" not in after
    assert "symphony-cpp-local" not in after
    assert "127.0.0.1:4318" not in after
    assert "hide_rate_limit_model_nudge = true" in after
    assert (stat.S_IMODE(codex_cfg.stat().st_mode)) == 0o600
    backups = list(
        (fake_home / ".local" / "state" / "dotfiles-v2" / "backups").glob(
            "config.toml.*.bak"
        )
    )
    assert len(backups) == 1
    assert doc_mod.find_dead_codex_otel(codex_cfg) == []

    # Idempotent re-check passes cleanly, and non-dead [otel] is preserved.
    rechk = subprocess.run(
        ["bash", str(prune_task)],
        env={
            "HOME": str(fake_home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_check": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "zero legacy global MCP entries found" in rechk.stdout

    codex_cfg.write_text(
        "[otel]\n"
        '  environment = "custom-prod"\n'
        "[otel.exporter.otlp-http]\n"
        '  endpoint = "https://otel.example.com/v1/logs"\n',
        encoding="utf-8",
    )
    assert doc_mod.find_dead_codex_otel(codex_cfg) == []


def test_herdr_env_stats_mod_and_sync_tasks(tmp_path: Path) -> None:
    mod_dir = (
        REPO_ROOT / "unix" / "home" / ".config" / "herdr-app" / "mods" / "env-stats"
    )
    mod_json = json.loads((mod_dir / "mod.json").read_text(encoding="utf-8"))
    assert mod_json["place"] == "none"
    assert mod_json["focusable"] is True

    main_js = (mod_dir / "main.js").read_text(encoding="utf-8")
    assert "window()?.visible" in main_js
    assert "checking()" in main_js
    assert "watchFolder(" in main_js
    assert 'run("pane.split"' in main_js
    assert '"env:top"' in main_js
    assert "WebView" not in main_js

    if shutil.which("herdr-app") and sys.platform == "darwin":
        chk_mod_dir = tmp_path / "herdr-check-env-stats"
        shutil.copytree(mod_dir, chk_mod_dir)
        chk = subprocess.run(
            ["herdr-app", "check", str(chk_mod_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert chk.returncode == 0, chk.stderr + chk.stdout

    # Test bootstrap:herdr-mods offline using HERDR_USAGE_LIMITS_SRC fixture.
    fake_home = tmp_path / "home"
    herdr_home = fake_home / ".config" / "herdr-app"
    (herdr_home / "layouts").mkdir(parents=True)
    (herdr_home / "mods" / "env-stats").mkdir(parents=True)
    shutil.copy2(mod_dir / "mod.json", herdr_home / "mods" / "env-stats" / "mod.json")
    shutil.copy2(mod_dir / "main.js", herdr_home / "mods" / "env-stats" / "main.js")
    (herdr_home / "layouts" / "main.json").write_text(
        json.dumps(
            {
                "id": "root",
                "type": "column",
                "children": [
                    {
                        "id": "status",
                        "type": "block",
                        "size": {"px": 28},
                        "content": {"mod": "workspace-status"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    fake_src = tmp_path / "src-ul"
    (fake_src / "mods" / "usage-limits").mkdir(parents=True)
    (fake_src / "mods" / "usage-limits" / "main.js").write_text(
        "export default class UsageLimits {}\n", encoding="utf-8"
    )
    (fake_src / "mods" / "usage-limits" / "mod.json").write_text(
        '{"place":"none"}\n', encoding="utf-8"
    )

    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    _make_stub(bin_stub, "herdr-app", "exit 0\n")

    boot_task = MISE_TASKS_DIR / "bootstrap" / "herdr-mods"
    env = {
        "HOME": str(fake_home),
        "HERDR_APP_HOME": str(herdr_home),
        "HERDR_USAGE_LIMITS_SRC": str(fake_src),
        "PATH": f"{bin_stub}:{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    if sys.platform == "darwin":
        subprocess.run(
            ["bash", str(boot_task)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert (herdr_home / "mods" / "usage-limits" / "main.js").is_file()
        stats_layout = json.loads(
            (herdr_home / "layouts" / "main-stats.json").read_text(encoding="utf-8")
        )
        assert stats_layout["children"][0]["id"] == "status-row"
        assert (
            stats_layout["children"][0]["children"][1]["content"]["mod"]
            == "usage-limits"
        )

        subprocess.run(
            ["bash", str(boot_task)],
            env={**env, "usage_check": "true"},
            capture_output=True,
            text=True,
            check=True,
        )

    # Test herdr:realtime-stats --dry-run --grid --json with stubbed herdr-app
    _make_stub(
        bin_stub,
        "herdr-app",
        'if [ "$1" = "ls" ]; then echo \'{"spaces":[{"id":"sp1","name":"realtime-stats"}]}\'; exit 0; fi\nexit 0\n',
    )
    rt_task = MISE_TASKS_DIR / "herdr" / "realtime-stats"
    jq_dir = str(Path(shutil.which("jq") or "/usr/bin/jq").parent)
    rt = subprocess.run(
        ["bash", str(rt_task)],
        env={
            "HOME": str(fake_home),
            "PATH": f"{bin_stub}:{jq_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_dry_run": "true",
            "usage_grid": "true",
            "usage_env_stats": "true",
            "usage_json": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    rt_json = json.loads(rt.stdout)
    assert rt_json["ok"] is True
    assert rt_json["space"] == "sp1"
    assert rt_json["grid"] is True

    # Test host:prune cleans dangling symlinks in ~/.config/herdr-app/mods/ without deleting real mods
    dangling_mod = herdr_home / "mods" / "stale-mod"
    dangling_mod.symlink_to(tmp_path / "does-not-exist")
    _make_stub(bin_stub, "mise", 'if [ "$1" = "doctor" ]; then echo "{}"; fi\nexit 0\n')
    prune_res = subprocess.run(
        ["bash", str(MISE_TASKS_DIR / "host" / "prune")],
        env={
            "HOME": str(fake_home),
            "MISE_DATA_DIR": str(tmp_path / "mise_data"),
            "PATH": f"{bin_stub}:{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_apply": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"REMOVED dead symlink: {dangling_mod}" in prune_res.stdout
    assert not dangling_mod.is_symlink()
    assert (herdr_home / "mods" / "env-stats" / "main.js").is_file()


def test_agy_statusline_atomic_output_cache_and_sub25ms_latency(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    cid = "12345678-abcd-ef01-2345-6789abcdef01"
    swarm_dir = (
        fake_home
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / cid
        / ".system_generated"
        / "swarm"
    )
    swarm_dir.mkdir(parents=True)
    (swarm_dir / "swarm.json").write_text(
        json.dumps({"title": "Phase 13 UI"}), encoding="utf-8"
    )
    (swarm_dir / "members.json").write_text(
        json.dumps(
            {
                "members": {
                    "w1": {"phase": "WORKING", "killed": False},
                    "w2": {"phase": "WAITING", "killed": False},
                    "w3": {"phase": "KILLED", "killed": True},
                }
            }
        ),
        encoding="utf-8",
    )

    ws_dir = tmp_path / "workspace"
    (ws_dir / ".planning").mkdir(parents=True)
    (ws_dir / ".planning" / ".active_plan").write_text(
        "2026-11-24-phase13-stats-ui\n", encoding="utf-8"
    )

    cache_home = tmp_path / "cache"
    quota_cache = cache_home / "herdr-usage-limits" / "antigravity.json"
    payload = json.dumps(
        {
            "conversation_id": cid,
            "model": {
                "id": "gemini-4-hydrogen-high",
                "display_name": "Gemini 4 Hydrogen (High)",
                "effort": "high",
            },
            "agent_state": "working",
            "cycle_mode": "auto",
            "plan_tier": "Ultra",
            "terminal_width": 120,
            "context_window": {
                "context_window_size": 1048576,
                "used_percentage": 24.22,
                "current_usage": {
                    "input_tokens": 200000,
                    "cache_read_input_tokens": 54000,
                    "cache_creation_input_tokens": 0,
                },
            },
            "quota": {
                "gemini-5h": {
                    "remaining_fraction": 0.65,
                    "reset_time": "2026-11-30T20:00:00Z",
                }
            },
            "cost": {"total_usd": 0.42},
        }
    )

    env = {
        "HOME": str(fake_home),
        "XDG_CACHE_HOME": str(cache_home),
        "HERDR_USAGE_AGY_CACHE": str(quota_cache),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }

    res = subprocess.run(
        [str(AGY_STATUSLINE)],
        cwd=ws_dir,
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    line = res.stdout.strip()
    assert "[working]" in line
    assert "Gemini 4 Hydrogen (High)" in line
    assert "254k/~508k" in line
    assert "50%" in line
    assert "~254k left" in line
    assert "q:65%" in line
    assert "swarm:1w/1i" in line
    assert "plan:2026-11-24-phase13-stats-ui" in line

    sess_file = cache_home / "usage-telemetry" / "agy-sessions" / f"{cid}.json"
    assert sess_file.is_file()
    sess_model = AgySessionStatus.model_validate(
        json.loads(sess_file.read_text(encoding="utf-8"))
    )
    assert sess_model.compaction_limit == 508000
    assert sess_model.used_tokens == 254000
    assert sess_model.compaction_headroom_tokens == 254000
    assert sess_model.swarm_working == 1
    assert sess_model.swarm_idle == 1
    assert sess_model.plan_id == "2026-11-24-phase13-stats-ui"

    # Monotonic quota merge keeps lower remaining_fraction for the same reset_time.
    higher_frac_payload = json.dumps(
        {
            "conversation_id": cid,
            "model": {"id": "gemini-4-hydrogen-high"},
            "quota": {
                "gemini-5h": {
                    "remaining_fraction": 0.90,
                    "reset_time": "2026-11-30T20:00:00Z",
                }
            },
        }
    )
    subprocess.run(
        [str(AGY_STATUSLINE)],
        cwd=ws_dir,
        env=env,
        input=higher_frac_payload,
        capture_output=True,
        text=True,
        check=True,
    )
    merged_q = json.loads(quota_cache.read_text(encoding="utf-8"))
    assert merged_q["quota"]["gemini-5h"]["remaining_fraction"] == 0.65

    # Benchmark latency across 10 invocations (strictly < 25 ms median).
    timings_ms: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        subprocess.run(
            [str(AGY_STATUSLINE)],
            cwd=ws_dir,
            env=env,
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
        )
        timings_ms.append((time.perf_counter() - t0) * 1000.0)
    timings_ms.sort()
    median_ms = timings_ms[len(timings_ms) // 2]
    assert median_ms < 25.0, (
        f"agy-statusline median {median_ms:.2f}ms >= 25ms ({timings_ms})"
    )


def test_env_stats_schema_first_collector_and_cli(tmp_path: Path) -> None:
    cache_home = tmp_path / "cache"
    sess_dir = cache_home / "usage-telemetry" / "agy-sessions"
    sess_dir.mkdir(parents=True)
    (sess_dir / "cid-1.json").write_text(
        json.dumps(
            {
                "conversation_id": "cid-1",
                "updated_at": 1764000000,
                "model_id": "gemini-4-hydrogen-high",
                "model_display_name": "Gemini 4 Hydrogen (High)",
                "agent_state": "working",
                "context_window_size": 1048576,
                "used_tokens": 254000,
                "used_percentage": 24.2,
                "compaction_limit": 508000,
                "compaction_used_percentage": 50.0,
                "compaction_headroom_tokens": 254000,
                "swarm_working": 2,
                "swarm_idle": 1,
                "swarm_total": 3,
                "plan_id": "2026-11-24-phase13-stats-ui",
            }
        ),
        encoding="utf-8",
    )

    herdr_home = tmp_path / "herdr"
    (herdr_home / "mods" / "env-stats").mkdir(parents=True)
    (herdr_home / "mods" / "env-stats" / "main.js").write_text(
        "// mod", encoding="utf-8"
    )

    resp = collect_env_stats(
        cache_home=cache_home,
        herdr_home=herdr_home,
        remote_states={EnvTargetName.PC: EnvTargetState.READY},
    )
    assert resp.rc == 0
    assert resp.returns[0].code == EnvStatsReturnCode.RET_OK
    assert len(resp.environments) == 5
    assert resp.agy_sessions[0].conversation_id == "cid-1"
    assert resp.herdr.env_stats_installed is True
    assert resp.herdr.usage_limits_installed is False

    # Corrupt session file triggers RET_PARTIAL cleanly without raising.
    (sess_dir / "broken.json").write_text("{not-json", encoding="utf-8")
    partial = collect_env_stats(cache_home=cache_home, herdr_home=herdr_home)
    assert partial.rc == 1
    assert partial.returns[0].code == EnvStatsReturnCode.RET_PARTIAL
    assert len(partial.errors) == 1

    # Remove broken file and verify tasks/env/stats --json CLI.
    (sess_dir / "broken.json").unlink()
    cli_res = subprocess.run(
        ["bash", str(TASKS_DIR / "env" / "stats")],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "usage_cache_home": str(cache_home),
            "usage_herdr_home": str(herdr_home),
            "usage_json": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    validated = EnvStatsSnapshotResponse.model_validate(json.loads(cli_res.stdout))
    assert validated.rc == 0
    assert validated.agy_sessions[0].compaction_headroom_tokens == 254000
