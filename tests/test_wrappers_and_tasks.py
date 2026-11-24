"""Tests for smart CLI wrappers (`codex-wrapper`, `codex-hc`, `agy-wrapper`) and operational host/enter tasks."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "unix" / "home" / ".local" / "bin"
TASKS_DIR = REPO_ROOT / "tasks"
MISE_TASKS_DIR = REPO_ROOT / "unix" / "home" / ".config" / "mise" / "tasks"


def _make_stub(directory: Path, name: str, script_body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{script_body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_codex_wrapper_strips_ephemeral_env_and_respects_herdr_mode(
    tmp_path: Path,
) -> None:
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    real_home = tmp_path / "home"
    (real_home / ".local" / "bin").mkdir(parents=True)
    (real_home / ".config" / "fnox").mkdir(parents=True)
    (real_home / ".config" / "fnox" / "config.toml").write_text("", encoding="utf-8")

    # Stub ~/.local/bin/codex and fnox recording argv + env keys cleanly
    _make_stub(
        real_home / ".local" / "bin",
        "codex",
        'python3 -c \'import json, os, sys; print(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}))\' "$@"',
    )
    _make_stub(
        bin_stub,
        "fnox",
        'while [ "$#" -gt 0 ]; do case "$1" in --) shift; break ;; *) shift ;; esac; done\nFNOX_WRAPPED=1 exec "$@"',
    )

    env = {
        "HOME": str(real_home),
        "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HTTP_PROXY": "http://127.0.0.1:9999",
        "HTTPS_PROXY": "http://127.0.0.1:9999",
        "ALL_PROXY": "http://127.0.0.1:9999",
        "SSL_CERT_FILE": "/tmp/fnox-proxy-ca-deleted.pem",
        "NODE_EXTRA_CA_CERTS": "/tmp/fnox-proxy-ca-deleted.pem",
        "REQUESTS_CA_BUNDLE": "/tmp/fnox-proxy-ca-deleted.pem",
        "CURL_CA_BUNDLE": "/tmp/fnox-proxy-ca-deleted.pem",
        "GIT_SSL_CAINFO": "/tmp/fnox-proxy-ca-deleted.pem",
        "CARGO_HTTP_CAINFO": "/tmp/fnox-proxy-ca-deleted.pem",
        "DENO_CERT": "/tmp/fnox-proxy-ca-deleted.pem",
        "RUST_LOG": "debug",
    }

    for sub in (
        ["app-server", "--listen", "stdio://"],
        ["remote-control"],
        ["update"],
        ["login"],
        ["doctor"],
        ["features", "list"],
        ["--version"],
        ["--help"],
        [],
    ):
        proc = subprocess.run(
            [str(BIN_DIR / "codex-wrapper"), *sub],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)
        assert payload["argv"] == sub
        assert "--disable" not in payload["argv"]
        assert "-c" not in payload["argv"]
        assert "FNOX_WRAPPED" not in payload["env"]
        for stripped in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "SSL_CERT_FILE",
            "NODE_EXTRA_CA_CERTS",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "GIT_SSL_CAINFO",
            "CARGO_HTTP_CAINFO",
            "DENO_CERT",
            "RUST_LOG",
        ):
            assert stripped not in payload["env"], stripped

    # Herdr isolation (CODEX_HERDR=1 / codex-hc) passes --disable daemon_auto_start without -c overrides
    hc_proc = subprocess.run(
        [str(BIN_DIR / "codex-wrapper"), "fix bug"],
        env={**env, "CODEX_HERDR": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    hc_payload = json.loads(hc_proc.stdout)
    assert hc_payload["argv"] == ["--disable", "daemon_auto_start", "fix bug"]
    assert hc_payload["env"].get("FNOX_WRAPPED") == "1"


def test_agy_wrapper_bypasses_fnox_for_management_subcommands(tmp_path: Path) -> None:
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    real_home = tmp_path / "home"
    (real_home / ".local" / "bin").mkdir(parents=True)
    (real_home / ".config" / "fnox").mkdir(parents=True)
    (real_home / ".config" / "fnox" / "config.toml").write_text("", encoding="utf-8")

    _make_stub(
        real_home / ".local" / "bin",
        "agy",
        'python3 -c \'import json, os, sys; print(json.dumps({"argv": sys.argv[1:], "fnox": os.environ.get("FNOX_WRAPPED"), "ssl": os.environ.get("SSL_CERT_FILE"), "rust_log": os.environ.get("RUST_LOG")}))\' "$@"',
    )
    _make_stub(
        bin_stub,
        "fnox",
        'while [ "$#" -gt 0 ]; do case "$1" in --) shift; break ;; *) shift ;; esac; done\nFNOX_WRAPPED=1 exec "$@"',
    )

    env = {
        "HOME": str(real_home),
        "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HTTPS_PROXY": "http://127.0.0.1:9999",
        "SSL_CERT_FILE": "/tmp/fnox-proxy-ca-deleted.pem",
        "RUST_LOG": "trace",
    }

    for sub in (
        ["remote-control", "status"],
        ["update"],
        ["changelog"],
        ["install"],
        ["models"],
        ["help"],
        ["--version"],
        ["--help"],
    ):
        proc = subprocess.run(
            [str(BIN_DIR / "agy-wrapper"), *sub],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        assert data["argv"] == sub
        assert data["fnox"] is None
        assert data["ssl"] is None
        assert data["rust_log"] is None

    interactive = subprocess.run(
        [str(BIN_DIR / "agy-wrapper"), "chat"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    idata = json.loads(interactive.stdout)
    assert idata["argv"] == ["chat"]
    assert idata["fnox"] == "1"
    assert idata["rust_log"] is None


def test_agy_rc_guard_check_and_never_runs_serve_help(tmp_path: Path) -> None:
    guard_script = MISE_TASKS_DIR / "host" / "agy-rc-guard"
    txt = guard_script.read_text(encoding="utf-8")
    # Issue #1742 invariant: serve --help leaks zombie remote-control listener
    non_comment_lines = "\n".join(
        line for line in txt.splitlines() if not line.lstrip().startswith("#")
    )
    assert "serve --help" not in non_comment_lines
    assert "kickstart" in txt  # #1269 handling
    assert "antigravity-oauth-token" in txt  # #1401 token check

    # Mock LaunchAgent + clean token passes --check --json
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    fake_home = tmp_path / "home"
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True)
    (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text(
        "tok\n", encoding="utf-8"
    )
    plist = (
        fake_home
        / "Library"
        / "LaunchAgents"
        / "com.google.antigravity-cli-daemon.plist"
    )
    plist.write_text(
        "<plist><dict><key>Label</key><string>com.google.antigravity-cli-daemon</string></dict></plist>\n",
        encoding="utf-8",
    )
    _make_stub(
        bin_stub,
        "agy",
        'if [ "$1" = "remote-control" ] && [ "$2" = "status" ]; then echo "Daemon status: running"; exit 0; fi; exit 1',
    )
    _make_stub(
        bin_stub,
        "launchctl",
        'echo "state = running"; exit 0',
    )

    if os.uname().sysname == "Darwin":
        proc = subprocess.run(
            ["bash", str(guard_script)],
            env={
                "HOME": str(fake_home),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_check": "true",
                "usage_json": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        parsed = json.loads(proc.stdout)
        assert parsed["token_ok"] is True
        assert parsed["rc_running"] is True
        assert parsed["issues"] == []


def test_env_enter_falls_back_to_docker_when_tailscale_ssh_unavailable(
    tmp_path: Path,
) -> None:
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    log_file = tmp_path / "calls.log"
    _make_stub(bin_stub, "tailscale", "exit 1")
    _make_stub(bin_stub, "ssh", "exit 255")
    _make_stub(
        bin_stub,
        "docker",
        f'printf "%s\\n" "$*" >> "{log_file}"; exit 0',
    )

    proc = subprocess.run(
        ["bash", str(MISE_TASKS_DIR / "env" / "enter")],
        env={
            "HOME": str(tmp_path),
            "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_target": "pc",
            "usage_project": "kb-v2",
            "usage_cmd": "pwd",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "falling back to direct runtime exec" in proc.stderr
    logged = log_file.read_text(encoding="utf-8")
    assert "exec -i dotfiles-v2 /bin/bash -il -c" in logged
    assert "/workspace-kb" in logged
