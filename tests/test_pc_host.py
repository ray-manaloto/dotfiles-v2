import os
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
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config/mise").symlink_to(MISE)
    (tmp_path / ".zshenv").write_text(config["dotfiles"]["~/.zshenv"]["content"])
    result = subprocess.check_output(
        ["zsh", "-c", 'echo "$MISE_ENV $ZDOTDIR"'],
        env={"HOME": str(tmp_path), "PATH": os.defpath},
        text=True,
    )
    assert result.strip() == f"linux,pc {tmp_path}/.config/zsh"
