"""Exercise Codex tasks with an isolated home and local command doubles."""

import os
import subprocess
from pathlib import Path

import pytest

BOOTSTRAP = (
    Path(__file__).resolve().parents[1] / "unix/home/.config/mise/tasks/bootstrap"
)


def write_executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)


@pytest.fixture
def isolated_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake_curl = tmp_path / "bin/curl"
    write_executable(
        fake_curl,
        """
        test "$1" = -fsSLo
        test "$3" = https://chatgpt.com/codex/install.sh
        cat > "$2" <<'INSTALLER'
set -eu
test "$CODEX_NON_INTERACTIVE" = 1
test "$1 $2" = '--release latest'
test "$CODEX_INSTALL_DIR" = "$HOME/.local/bin"
case "$PATH" in "$CODEX_INSTALL_DIR:"*) ;; *) exit 1 ;; esac
if read -r unexpected; then exit 1; fi
printf '%s\n' invoked >> "$HOME/installer-calls"
release_bin="${CODEX_HOME:-$HOME/.codex}/packages/standalone/current/bin"
mkdir -p "$CODEX_INSTALL_DIR" "$release_bin"
printf '%s\n' '#!/bin/sh' 'test "$1" = --version && echo codex-test' > "$release_bin/codex"
chmod +x "$release_bin/codex"
ln -sf "$release_bin/codex" "$CODEX_INSTALL_DIR/codex"
INSTALLER
""",
    )
    return home, {
        "HOME": str(home),
        "PATH": f"{fake_curl.parent}:{os.defpath}",
        "TMPDIR": str(tmp_path),
    }


def run_task(task, env, **flags):
    return subprocess.run(
        ["/bin/bash", str(BOOTSTRAP / task)],
        env=env | flags,
        input="this input belongs to the caller\n",
        text=True,
        capture_output=True,
        check=False,
    )


def test_install_keep_and_upgrade(isolated_home):
    home, env = isolated_home
    calls = home / "installer-calls"

    dry_run = run_task("codex", env, usage_dry_run="true")
    assert dry_run.returncode == 0, dry_run.stderr
    assert "would install" in dry_run.stdout
    assert not list(home.iterdir())

    for flags in ({}, {}, {"usage_upgrade": "true"}):
        result = run_task("codex", env, **flags)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "codex-test\n"
    assert calls.read_text().splitlines() == ["invoked", "invoked"]

    dry_run = run_task("codex", env, usage_upgrade="true", usage_dry_run="true")
    assert dry_run.returncode == 0, dry_run.stderr
    assert "would upgrade" in dry_run.stdout
    assert len(calls.read_text().splitlines()) == 2


def test_headless_login_delegates_and_propagates_exit_status(isolated_home):
    home, env = isolated_home
    missing = run_task("codex-device-login", env)
    assert missing.returncode == 1
    assert "mise run bootstrap:codex" in missing.stderr
    write_executable(
        home / ".local/bin/codex",
        """
        test "$#" = 2
        test "$1 $2" = 'login --device-auth'
        if read -r unexpected; then exit 1; fi
        exit 13
""",
    )
    result = run_task("codex-device-login", env)
    assert result.returncode == 13, result.stderr
    assert not result.stdout


def test_container_install_leaves_persistent_codex_state_intact(isolated_home):
    home, env = isolated_home
    state = home / ".codex"
    old_binary = state / "packages/standalone/current/bin/codex"
    write_executable(old_binary, "echo codex-old\n")
    binary = home / ".local/bin/codex"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(old_binary)
    env["container"] = "docker"
    env["CODEX_HOME"] = str(state)

    for _ in range(2):
        result = run_task("codex", env)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "codex-test\n"
    assert not binary.resolve().is_relative_to(state)
    assert old_binary.read_text().endswith("echo codex-old\n")
    assert (home / "installer-calls").read_text().splitlines() == ["invoked"]
