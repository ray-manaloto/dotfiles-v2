import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType

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
            "-n -o BatchMode=yes -- fixture export OMARCHY_PATH=/usr/share/omarchy;"
            " [[ $(omarchy-channel-current) == rc ]] || omarchy-channel-set rc;"
            " omarchy update -y",
        )
    assert commands == expected


@pytest.mark.skipif(
    shutil.which("zsh") is None, reason="zsh not installed (e.g. CI runner)"
)
def test_pc_overlay_and_zsh(tmp_path: Path) -> None:
    source = (MISE / "config.pc.toml").read_text()
    assert source.splitlines()[0] == "#:schema https://mise.jdx.dev/schema/mise.json"
    config = tomllib.loads(source)
    assert config["settings"]["upgrade"] == {"auto_prune": True, "prune_after": "24h"}
    assert (
        "env" not in config
    )  # no age key on pc: fnox uses the Doppler CLI's stored token
    dotfiles = config["dotfiles"]
    assert "~/.zshenv" not in dotfiles
    pc_fnox = "~/.config/fnox/config.pc.toml"
    assert dotfiles[pc_fnox] == {"main_checkout": True}
    assert dotfiles["~/.config/fnox/config.toml"] == {
        "source": pc_fnox,
        "main_checkout": True,
        "depends": [pc_fnox],
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
        ["zsh", "-c", 'echo "$MISE_ENV $ZDOTDIR $MISE_SYSTEM_CONFIG_DIR"'],
        env=env,
        text=True,
    )
    assert result.strip() == f"linux,pc {tmp_path}/.config/zsh /nonexistent"
    for rc in (".bashrc", ".profile"):
        result = subprocess.check_output(
            [
                "bash",
                "-c",
                '. "$HOME/$1"; echo "$MISE_ENV $MISE_SYSTEM_CONFIG_DIR"',
                "bash",
                rc,
            ],
            env=env,
            text=True,
        )
        assert result.strip() == "linux,pc /nonexistent"


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
        "git": 'test "$MISE_SYSTEM_CONFIG_DIR" = "/nonexistent" && test "$*" = "-c core.hooksPath=/dev/null -C $HOME/dev/dotfiles-v2 pull --ff-only"',
        "getent": f'printf "fixture:x:1000:1000::/tmp:{login_shell}\\n"',
        "zsh": "exit 0",
        "sudo": f'printf "%s\\n" "$*" >> "{sudo_calls}"',
        "mise": """
set -eu
test "$MISE_SYSTEM_CONFIG_DIR" = "/nonexistent"
test "$MISE_YES" = "1"
if [ "$1" = bootstrap ]; then
    ln -s "$HOME/dev/dotfiles-v2/unix/home/.config/mise" "$HOME/.config/mise"
else
    test "$#" -eq 4
    test "$1" = "-C"
    test "$2" = "$HOME/dev/dotfiles-v2"
    test "$3" = "deps"
    test "$4" = "uv"
    test -L "$HOME/.config/mise"
    touch "$2/deps-uv-ran"
fi
""",
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
    assert "doppler-pc.env" in result.stderr and "0600" in result.stderr
    assert stat.S_IMODE((tmp_path / ".config/fnox").stat().st_mode) == 0o700
    assert not (tmp_path / ".config/fnox/age.txt").exists()
    assert (tmp_path / ".config/mise").resolve() == target
    assert (checkout / "deps-uv-ran").is_file()


@pytest.fixture
def pc_stack_doctor() -> ModuleType:
    path = ROOT / "tasks/py/pc_stack_doctor.py"
    spec = importlib.util.spec_from_file_location("pc_stack_doctor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("check", ["status", "plan"])
def test_doctor_rejects_each_missing_bootstrap_resource(
    pc_stack_doctor: ModuleType, monkeypatch: pytest.MonkeyPatch, check: str
) -> None:
    config = tomllib.loads((MISE / "config.pc.toml").read_text())["bootstrap"]
    declared = {("file", name) for name in config["files"]}
    declared.update(
        ("user-service" if spec.get("scope") == "user" else "service", name)
        for name, spec in config["services"].items()
    )
    if check == "plan":
        declared.update(("package", name) for name in config["packages"])

    def report_for(resources: set[tuple[str, str]]) -> dict:
        entries = [
            (
                kind,
                {
                    "id": {"kind": kind, "name": str(Path(name).expanduser())},
                    "origin": None if kind == "package" else {"environment": ["pc"]},
                    "action": "noop",
                },
            )
            for kind, name in sorted(resources)
        ]
        if check == "plan":
            return {"resources": [entry for _, entry in entries]}
        return {
            "files": [entry for kind, entry in entries if kind == "file"],
            "services": [entry for kind, entry in entries if kind == "service"],
            "user_services": [
                {"name": name, "action": "noop"}
                for kind, name in resources
                if kind == "user-service"
            ],
        }

    def fake_run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[2] == check
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(report))

    monkeypatch.setattr(pc_stack_doctor, "run", fake_run)
    report = report_for(declared)
    assert pc_stack_doctor.bootstrap(check) == f"{len(declared)} pc resources match"

    for kind, name in sorted(declared):
        report = report_for(declared - {(kind, name)})
        with pytest.raises(ValueError) as error:
            pc_stack_doctor.bootstrap(check)
        assert str(error.value) == f"missing pc declarations: {kind}:{name}"


@pytest.mark.parametrize("second_state", ["inactive", "activating", "failed"])
def test_doctor_retries_activating_cdi_once(
    pc_stack_doctor: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    second_state: str,
) -> None:
    states = iter(("activating", second_state))
    shown = []
    sleeps = []
    spec = tmp_path / "nvidia.yaml"
    spec.touch()

    def fake_run(*args: str) -> subprocess.CompletedProcess[str]:
        output = ""
        if args[:2] == ("systemctl", "show"):
            state = next(states)
            shown.append(state)
            output = (
                f"LoadState=loaded\nUnitFileState=enabled\nActiveState={state}\n"
                "Result=success\n"
            )
        elif args[0] == "nvidia-ctk":
            output = "nvidia.com/gpu=all\n"
        return subprocess.CompletedProcess(args, 0, stdout=output)

    monkeypatch.setattr(pc_stack_doctor, "run", fake_run)
    monkeypatch.setattr(pc_stack_doctor, "Path", lambda _: spec)
    monkeypatch.setattr(pc_stack_doctor.os.path, "lexists", lambda _: False)
    monkeypatch.setattr(pc_stack_doctor.time, "sleep", sleeps.append)
    if second_state == "failed":
        with pytest.raises(ValueError, match="outside its successful oneshot state"):
            pc_stack_doctor.gpu_cdi()
    else:
        assert "device spec match" in pc_stack_doctor.gpu_cdi()
    assert sleeps == [2]
    assert shown == ["activating", second_state]


def test_omarchy_channel_pin_matches_up_and_doctor() -> None:
    up = (TASKS / "up").read_text()
    doctor = (TASKS / "doctor").read_text()
    assert "omarchy_channel=rc" in up
    assert "[[ $channel == rc ]]" in doctor


def test_pc_reviewer_plugins_match_manifest(pc_stack_doctor: ModuleType) -> None:
    """pc runs the doctor from stdin and cannot read the manifest, so its plugin list is inlined."""
    manifest = json.loads((MISE / "assets/reviewer-plugins.json").read_text())["claude"]
    assert not manifest.get("platforms"), "gated plugins need per-host filtering in pc"
    assert list(pc_stack_doctor.REVIEWER_CLAUDE_PLUGINS) == manifest["plugins"]


def test_pc_reviewer_plugins_flags_missing_and_disabled(
    pc_stack_doctor: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugins = pc_stack_doctor.REVIEWER_CLAUDE_PLUGINS
    rows = [{"id": p, "enabled": True, "scope": "user"} for p in plugins]

    def fake_run(*args: str, **_kw: object) -> subprocess.CompletedProcess[str]:
        assert args[1:] == ("plugin", "list", "--json")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(rows))

    monkeypatch.setattr(pc_stack_doctor, "run", fake_run)
    assert "installed" in pc_stack_doctor.reviewer_plugins()
    rows.pop(0)
    rows[0]["enabled"] = False
    with pytest.raises(ValueError, match="2 claude plugins missing/disabled"):
        pc_stack_doctor.reviewer_plugins()


def test_pc_dead_shims_reports_count_and_first_names(
    pc_stack_doctor: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shims = tmp_path / "shims"
    shims.mkdir()
    live_bin = tmp_path / "bin"
    live_bin.mkdir()
    for name in ("alive", "gone1", "gone2", "gone3", "gone4", "gone5", "gone6", "mise"):
        (shims / name).touch()
    (live_bin / "alive").touch()
    probed: list[str] = []

    def fake_run(*args: str, **_kw: object) -> subprocess.CompletedProcess[str]:
        if args[1] == "bin-paths":
            return subprocess.CompletedProcess(args, 0, stdout=f"{live_bin}\n")
        probed.append(args[2])
        if (
            args[2] == "gone6"
        ):  # installed but outside the active config, like hub:up's ollama
            msg = "mise ERROR gone6 is a mise bin however it is not currently active."
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=msg)
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr="mise ERROR no tool"
        )

    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pc_stack_doctor, "run", fake_run)
    with pytest.raises(ValueError, match="5 of 8 mise shims are dead") as error:
        pc_stack_doctor.dead_shims()
    assert "gone1, gone2, gone3, gone4, gone5;" in str(error.value)
    assert "1 installed but inactive: gone6" in str(error.value)
    assert probed == [f"gone{i}" for i in range(1, 7)]  # live shims are never probed
    for name in probed[:5]:
        (live_bin / name).touch()
    assert pc_stack_doctor.dead_shims() == (
        "no dead mise shims among 8 (1 installed but inactive: gone6)"
    )
    (live_bin / "gone6").touch()
    assert pc_stack_doctor.dead_shims() == "no dead mise shims among 8"
