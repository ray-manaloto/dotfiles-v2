import os
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks/pc"
MISE = ROOT / "unix/home/.config/mise"


@pytest.fixture
def offline_env(tmp_path: Path) -> dict[str, str]:
    # Record only the requested command. Never interpret a remote script.
    for command in ("ssh", "mise"):
        stub = tmp_path / command
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PC_CALLS"\n')
        stub.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PC_CALLS": str(tmp_path / "calls"),
    }


@pytest.mark.parametrize("task", ["up", "sync", "doctor"])
def test_usage_and_dry_run(task: str, offline_env: dict[str, str]) -> None:
    path = TASKS / task
    subprocess.run(["usage", "lint", "-W", "-q", path], check=True)
    for args, host in [([], "pc"), (["--host", "fixture"], "fixture")]:
        output = subprocess.check_output(
            [path, "--dry-run", *args], text=True, env=offline_env
        )
        assert f"pc:{task} dry-run ({host})" in output
        assert "omarchy update" not in output
    result = subprocess.run([path, "--unknown-option"], env=offline_env, check=False)
    assert result.returncode != 0
    assert not Path(offline_env["PC_CALLS"]).exists()


@pytest.mark.parametrize("opt_in", [False, True])
def test_upgrade_requires_opt_in(opt_in: bool, offline_env: dict[str, str]) -> None:
    offline_env["usage_upgrade_omarchy"] = "true"
    args = [TASKS / "up", "--host", "fixture"]
    if opt_in:
        args.append("--upgrade-omarchy")
    output = subprocess.check_output([*args, "--dry-run"], text=True, env=offline_env)
    calls = Path(offline_env["PC_CALLS"])
    assert ("omarchy update -y" in output) == opt_in
    assert not calls.exists()
    subprocess.run(args, env=offline_env, check=True)
    commands = calls.read_text().splitlines()
    expected = [
        "-o BatchMode=yes -- fixture /bin/bash -euo pipefail",
        "run pc:sync --host fixture",
    ]
    if opt_in:
        expected.insert(
            0,
            "-n -o BatchMode=yes -- fixture OMARCHY_PATH=/usr/share/omarchy omarchy update -y",
        )
    assert commands == expected


def test_pc_overlay_and_zsh(tmp_path: Path) -> None:
    source = (MISE / "config.pc.toml").read_text()
    assert source.splitlines()[0] == "#:schema https://mise.jdx.dev/schema/mise.json"
    config = tomllib.loads(source)
    assert config["settings"]["upgrade"] == {"auto_prune": True, "prune_after": "24h"}
    assert config["env"] == {"FNOX_AGE_KEY_FILE": "{{ env.HOME }}/.config/fnox/age.txt"}
    dotfiles = config["dotfiles"]
    assert "~/.zshenv" not in dotfiles
    headless = "~/.config/fnox/config.headless.toml"
    assert dotfiles["~/.config/fnox/config.toml"] == {
        "source": headless,
        "main_checkout": True,
        "depends": [headless],
    }
    (tmp_path / ".zshenv").symlink_to(ROOT / "unix/home/.zshenv")
    (tmp_path / ".bashrc").write_text("return\n")
    (tmp_path / ".profile").write_text("export MISE_ENV=old\n")
    (tmp_path / "mise.toml").write_text(source)
    env = {
        "HOME": str(tmp_path),
        "PATH": os.defpath,
        "MISE_EXPERIMENTAL": "1",
        "MISE_TRUSTED_CONFIG_PATHS": str(tmp_path),
        "MISE_OFFLINE": "1",
    }
    mise = shutil.which("mise")
    assert mise
    subprocess.run(
        [
            mise,
            "dotfiles",
            "apply",
            "~/.config/dotfiles-v2/pc-env.sh",
            "~/.bashrc",
            "~/.profile",
        ],
        env=env,
        cwd=tmp_path,
        check=True,
    )
    result = subprocess.check_output(
        ["zsh", "-c", 'echo "$MISE_ENV $ZDOTDIR"'],
        env=env,
        text=True,
    )
    assert result.strip() == f"linux,pc {tmp_path}/.config/zsh"
    for rc in (".bashrc", ".profile"):
        result = subprocess.check_output(
            ["bash", "-c", '. "$HOME/$1"; echo "$MISE_ENV"', "bash", rc],
            env=env,
            text=True,
        )
        assert result.strip() == "linux,pc"


@pytest.mark.parametrize("login_shell", ["/usr/bin/bash", "/usr/bin/zsh"])
def test_sync_bootstraps_without_identity(tmp_path: Path, login_shell: str) -> None:
    assert "agent-secrets" not in (TASKS / "up").read_text()
    checkout = tmp_path / "dev/dotfiles-v2"
    (checkout / ".git").mkdir(parents=True)
    target = checkout / "unix/home/.config/mise"
    target.mkdir(parents=True)
    (target / "config.pc.toml").touch()
    sudo_calls = tmp_path / "sudo_calls"
    for name, body in {
        "git": 'test "$*" = "-C $HOME/dev/dotfiles-v2 pull --ff-only"',
        "getent": f'printf "fixture:x:1000:1000::/tmp:{login_shell}\\n"',
        "zsh": "exit 0",
        "sudo": f'printf "%s\\n" "$*" >> "{sudo_calls}"',
        "mise": 'test "$1" = bootstrap && ln -s "$HOME/dev/dotfiles-v2/unix/home/.config/mise" "$HOME/.config/mise"',
    }.items():
        stub = tmp_path / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    # Exercise the SSH payload locally with a disposable standalone mise.
    script = (
        (TASKS / "sync")
        .read_text()
        .split("<<'REMOTE'\n", 1)[1]
        .removesuffix("REMOTE\n")
    )
    script = script.replace("/usr/local/bin", '"$HOME"')
    result = subprocess.run(
        ["bash", "-euo", "pipefail"],
        input=script,
        text=True,
        capture_output=True,
        env={"HOME": str(tmp_path), "PATH": f"{tmp_path}:{os.defpath}"},
        check=True,
    )
    user = subprocess.check_output(["id", "-un"], text=True).strip()
    if login_shell.endswith("/bash"):
        assert sudo_calls.read_text().splitlines() == [
            f"-n usermod -s {tmp_path / 'zsh'} {user}"
        ]
    else:
        assert not sudo_calls.exists()
    assert "age.txt" in result.stderr and "0600" in result.stderr
    assert stat.S_IMODE((tmp_path / ".config/fnox").stat().st_mode) == 0o700
    assert not (tmp_path / ".config/fnox/age.txt").exists()
    assert (tmp_path / ".config/mise").resolve() == target
