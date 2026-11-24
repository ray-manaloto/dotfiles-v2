"""Tests for schema-first `env_inventory`, privacy sanitization, BuildKit/parallel tasks, and CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

import env_inventory.collector as collector_mod
from dotfiles_py import NANOSECOND_TIMESTAMP_RE
from dotfiles_py.generated import (
    ContainerOciMetadata,
    EnvInventoryResponse,
    HarnessesInventory,
    HarnessStatus,
    InventoryErrorCode,
    InventoryHistoryRunRecord,
    InventoryReturnCode,
    InventoryTargetKind,
    MiseInventory,
    TargetInventory,
)
from env_inventory import (
    build_privacy_context,
    collect_inventory,
    sanitize_model,
    sanitize_text,
    validate_no_privacy_leaks,
)
from env_inventory.cli import main as env_inventory_cli_main

REPO_ROOT = Path(__file__).resolve().parents[1]


def _fake_runner_factory(
    *,
    home_dir: Path,
    alive_targets: set[str] | None = None,
    missing_tools: set[str] | None = None,
) -> tuple[Any, list[list[str]]]:
    alive = {"host", "pc", "mac", "vm"} if alive_targets is None else alive_targets
    missing = set() if missing_tools is None else missing_tools
    invoked: list[list[str]] = []

    def _harness_and_mise(
        tool_cmd: list[str], prefix_home: str
    ) -> tuple[int, str, str]:
        if not tool_cmd:
            return 1, "", "empty command"
        exe = tool_cmd[0]
        if exe in missing:
            return 127, "", f"command not found: {exe}"

        if exe == "uname":
            if prefix_home == "/home/dev":
                return 0, "Linux x86_64\n", ""
            return 0, "Darwin arm64\n", ""

        if exe == "printenv" and tool_cmd[1:] == ["HOME"]:
            return 0, f"{prefix_home}\n", ""

        if exe == "mise":
            sub = tool_cmd[3] if len(tool_cmd) > 3 and tool_cmd[1] == "-C" else ""
            if sub == "doctor":
                return (
                    0,
                    json.dumps(
                        {
                            "version": "2026.11.13",
                            "activated": True,
                            "build_info": {"target": "aarch64-apple-darwin"},
                            "config_files": [f"{prefix_home}/.config/mise/config.toml"],
                            "errors": None,
                            "warnings": None,
                        }
                    ),
                    "",
                )
            if sub == "ls":
                return (
                    0,
                    json.dumps(
                        {
                            "oxfmt": [
                                {
                                    "version": "0.80.0",
                                    "requested_version": "0.80.0",
                                    "install_path": f"{prefix_home}/.local/share/mise/installs/oxfmt/0.80.0",
                                    "source": {
                                        "type": "mise.toml",
                                        "path": f"{prefix_home}/.config/mise/config.toml",
                                    },
                                    "installed": True,
                                    "active": True,
                                }
                            ],
                            "syft": [
                                {
                                    "version": "1.57.2",
                                    "requested_version": "1.57.2",
                                    "install_path": f"{prefix_home}/.local/share/mise/installs/syft/1.57.2",
                                    "source": {
                                        "type": "mise.toml",
                                        "path": f"{prefix_home}/.config/mise/config.toml",
                                    },
                                    "installed": True,
                                    "active": True,
                                }
                            ],
                        }
                    ),
                    "",
                )
            if sub == "skills":
                return (
                    0,
                    json.dumps(
                        [
                            {
                                "name": "fnox",
                                "tool": "fnox",
                                "version": "1.50.0",
                                "path": f"{prefix_home}/.local/share/mise/installs/fnox/1.50.0/skills/fnox",
                            }
                        ]
                    ),
                    "",
                )

        if exe == "agy" and "--version" in tool_cmd:
            return 0, "1.5.16\n", ""

        if exe == "python3" and "-c" in tool_cmd:
            return (
                0,
                json.dumps(
                    {
                        "skills": ["fnox", "mise-bootstrap-ops"],
                        "mise_skill_links": ["fnox"],
                    }
                ),
                "",
            )

        if exe == "claude":
            if "--version" in tool_cmd:
                return 0, "2.1.331 (Claude Code)\n", ""
            if tool_cmd[1:4] == ["plugin", "list", "--json"]:
                return (
                    0,
                    json.dumps(
                        [
                            {
                                "id": "code-review@claude-plugins-official",
                                "version": "1.0.0",
                                "scope": "user",
                                "enabled": True,
                                "installPath": f"{prefix_home}/.claude/plugins/cache/code-review/1.0.0",
                                "projectPath": f"{prefix_home}/dev/github/example/dotfiles-v2",
                            }
                        ]
                    ),
                    "",
                )

        if exe == "codex":
            if "--version" in tool_cmd:
                return 0, "codex-cli 0.172.0\n", ""
            if tool_cmd[1:4] == ["plugin", "list", "--json"]:
                return (
                    0,
                    json.dumps(
                        {
                            "installed": [
                                {
                                    "pluginId": "documents@openai-primary-runtime",
                                    "name": "documents",
                                    "marketplaceName": "openai-primary-runtime",
                                    "version": "26.1119.11507",
                                    "installed": True,
                                    "enabled": False,
                                    "source": {
                                        "source": "local",
                                        "path": f"{prefix_home}/.cache/codex-runtimes/documents",
                                    },
                                }
                            ],
                            "available": [],
                        }
                    ),
                    "",
                )
            if tool_cmd[1:4] == ["mcp", "list", "--json"]:
                return (
                    0,
                    json.dumps(
                        [
                            {
                                "name": "mise",
                                "enabled": True,
                                "disabled_reason": None,
                                "transport": {
                                    "type": "stdio",
                                    "command": f"{prefix_home}/.local/share/mise/shims/mise",
                                    "env_vars": ["MISE_ENV"],
                                },
                                "auth_status": "unsupported",
                            }
                        ]
                    ),
                    "",
                )

        if exe == "opencode" and "--version" in tool_cmd:
            return 0, "opencode v2.4.1\n", ""

        if exe == "zcode" and "--version" in tool_cmd:
            return 0, "zcode-app-cli 3.18.2-48\nzcode-runtime 0.17.1\n", ""

        return 1, "", f"unmocked command: {' '.join(tool_cmd)}"

    def _runner(
        argv: list[str],
        timeout: float = 15.0,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        _ = (timeout, env)
        invoked.append(list(argv))
        if argv[:2] == ["ssh", "-G"]:
            return 0, "hostname private-linux-box.lan\n", ""
        if argv[:2] == ["scutil", "--get"]:
            return 0, "Alice-MacBook-Pro\n", ""

        # PC or MAC docker commands
        if "docker" in argv:
            is_pc = any("DOCKER_HOST=ssh://" in tok for tok in argv)
            tgt = "pc" if is_pc else "mac"
            docker_idx = argv.index("docker")
            rest = argv[docker_idx + 1 :]
            if rest[:2] == ["--context", "desktop-linux"]:
                rest = rest[2:]
            if rest[:2] == ["image", "inspect"]:
                if tgt not in alive:
                    return 1, "", "no such image"
                return (
                    0,
                    json.dumps(
                        [
                            {
                                "Id": f"sha256:1111{tgt}",
                                "Architecture": "amd64" if is_pc else "arm64",
                                "Os": "linux",
                                "Size": 123456,
                                "Created": "2026-11-24T04:00:00Z",
                                "RepoDigests": [f"dotfiles-v2@sha256:aaaa{tgt}"],
                                "Descriptor": {
                                    "mediaType": "application/vnd.oci.image.index.v1+json",
                                    "digest": f"sha256:aaaa{tgt}",
                                    "size": 856,
                                },
                            }
                        ]
                    ),
                    "",
                )
            if rest[:2] == ["container", "inspect"]:
                if tgt not in alive:
                    return 1, "false", "container not running"
                return 0, "true\n", ""
            if rest and rest[0] == "exec":
                # ["exec", "-i", "-w", "/workspace", "dotfiles-v2", ...]
                inner_idx = rest.index("dotfiles-v2") + 1
                return _harness_and_mise(rest[inner_idx:], "/home/dev")

        # VM tart commands
        if argv[:2] == ["tart", "exec"]:
            if "vm" not in alive:
                return 1, "", "VM is not running"
            if argv[2:] == ["dotfiles-v2-macos", "true"]:
                return 0, "", ""
            # ["tart", "exec", "-i", VM_NAME, "/bin/bash", "-lc", wrapper, "bash", *cmd]
            bash_idx = argv.index("bash")
            return _harness_and_mise(argv[bash_idx + 1 :], "/Users/vmtester")

        # Host direct commands
        return _harness_and_mise(argv, str(home_dir))

    return _runner, invoked


def test_generated_env_inventory_models_strict_and_forbid_extra() -> None:
    empty_mise = MiseInventory(
        installed=False, errors=[], warnings=[], tools=[], skills=[]
    )
    empty_harness = HarnessStatus(name="agy", installed=False)
    harnesses = HarnessesInventory(
        agy=empty_harness,
        claude=HarnessStatus(name="claude", installed=False),
        codex=HarnessStatus(name="codex", installed=False),
        opencode=HarnessStatus(name="opencode", installed=False),
    )
    valid = TargetInventory(
        schema_version=1,
        target=InventoryTargetKind.HOST,
        reachable=True,
        arch="arm64",
        os="macos",
        mise=empty_mise,
        container=None,
        harnesses=harnesses,
        timestamp="20261123T234500.000000123-0600",
        timestamp_unix_ns=123,
        errors=[],
        returns=[],
    )
    assert NANOSECOND_TIMESTAMP_RE.fullmatch(valid.timestamp)

    with pytest.raises(ValidationError):
        TargetInventory.model_validate({**valid.model_dump(), "extra_key": True})

    with pytest.raises(ValidationError):
        TargetInventory.model_validate(
            {**valid.model_dump(), "timestamp_unix_ns": True}
        )

    with pytest.raises(ValidationError):
        ContainerOciMetadata(
            image_tag="dotfiles-v2:dev",
            inspected=True,
            size_bytes=-1,
            buildx_metadata_present=False,
            sbom_present=False,
            provenance_present=False,
        )


def test_privacy_sanitizer_strips_paths_users_and_private_hostnames() -> None:
    ctx = build_privacy_context(
        ssh_host="pc",
        runner=lambda argv, _t, _e: (
            (0, "hostname secret-pc-node.lan\n", "")
            if argv[:2] == ["ssh", "-G"]
            else (1, "", "")
        ),
        extra_homes=["/Users/alice", "/home/bob"],
        extra_users=["alice", "bob"],
        extra_hostnames={"host": ["Alice-MacBook-Pro.local"]},
    )
    sample = (
        "/Users/alice/.local/bin/claude on Alice-MacBook-Pro.local "
        "reaching /home/bob/dev via secret-pc-node.lan owned by alice and bob"
    )
    cleaned = sanitize_text(sample, ctx)
    assert "/Users/alice" not in cleaned
    assert "/home/bob" not in cleaned
    assert "alice" not in cleaned.lower()
    assert "bob" not in cleaned.lower()
    assert "secret-pc-node" not in cleaned.lower()
    assert "Alice-MacBook-Pro" not in cleaned
    assert "~/.local/bin/claude" in cleaned
    assert "~/dev" in cleaned
    assert "host" in cleaned
    assert "pc" in cleaned
    assert validate_no_privacy_leaks(cleaned, ctx) == []

    # Leak detector flags raw private paths, users, and private hostnames
    assert validate_no_privacy_leaks('{"path": "/Users/alice/foo"}', ctx)
    assert validate_no_privacy_leaks('{"path": "/home/bob/foo"}', ctx)
    assert validate_no_privacy_leaks('{"host": "secret-pc-node.lan"}', ctx)
    assert validate_no_privacy_leaks('{"owner": "alice"}', ctx)

    # sanitize_model sanitizes nested models and re-validates strictly
    dirty_harness = HarnessStatus(
        name="agy",
        installed=True,
        version="1.5.16",
        skills=["/Users/alice/.agents/skills/fnox"],
    )
    clean_harness = sanitize_model(dirty_harness, ctx)
    assert clean_harness.skills == ["~/.agents/skills/fnox"]


def test_collect_inventory_all_targets_writes_sanitized_json_and_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "Users" / "alice"
    skills_dir = fake_home / ".agents" / "skills"
    (skills_dir / "fnox").mkdir(parents=True)
    (skills_dir / "mise-bootstrap-ops").mkdir(parents=True)
    (skills_dir / ".mise-skills.json").write_text(
        json.dumps({"links": {"fnox": str(fake_home / ".local/share/mise/fnox")}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    state_dir = tmp_path / "state" / "dotfiles-v2" / "inventory"
    state_dir.mkdir(parents=True)
    for tgt in ("pc", "mac"):
        (state_dir / f"buildx-{tgt}.metadata.json").write_text(
            json.dumps(
                {
                    "buildx.build.ref": f"{tgt}/default/abc123",
                    "containerimage.digest": f"sha256:aaaa{tgt}",
                    "containerimage.descriptor": {
                        "mediaType": "application/vnd.oci.image.index.v1+json",
                        "digest": f"sha256:aaaa{tgt}",
                        "size": 856,
                    },
                    "buildx.build.provenance": {
                        "buildType": "https://mobyproject.org/buildkit@v1",
                        "materials": [
                            {"uri": "pkg:docker/docker/buildkit-syft-scanner@stable-1"},
                            {"uri": "pkg:docker/ubuntu@26.10"},
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )

    out_dir = tmp_path / "repo" / "inventory"
    runner, invoked = _fake_runner_factory(home_dir=fake_home)
    ctx = build_privacy_context(
        ssh_host="pc",
        runner=runner,
        extra_homes=[str(fake_home), "/Users/vmtester"],
        extra_users=["alice", "vmtester"],
    )

    resp = collect_inventory(
        requested_target="all",
        ssh_host="pc",
        write_repo=True,
        repo_root=tmp_path / "repo",
        out_dir=out_dir,
        state_dir=state_dir,
        runner=runner,
        privacy_ctx=ctx,
    )
    assert resp.rc == 0
    assert resp.summary.targets_collected == 4
    assert resp.summary.targets_reachable == 4
    assert len(resp.inventories) == 4
    assert not any("--smoke" in cmd for cmd in invoked)

    by_target = {inv.target.value: inv for inv in resp.inventories}
    assert by_target["host"].container is None
    assert by_target["vm"].container is None
    for c_tgt in ("pc", "mac"):
        meta = by_target[c_tgt].container
        assert meta is not None
        assert meta.inspected is True
        assert meta.buildx_metadata_present is True
        assert meta.sbom_present is True
        assert meta.provenance_present is True
        assert meta.provenance_build_type == "https://mobyproject.org/buildkit@v1"

    assert by_target["host"].harnesses.claude.plugins[0].project_path == (
        "~/dev/github/example/dotfiles-v2"
    )
    assert by_target["host"].harnesses.agy.skills == ["fnox", "mise-bootstrap-ops"]
    assert by_target["host"].harnesses.agy.mise_skill_links == ["fnox"]

    for tgt in ("host", "pc", "mac", "vm"):
        latest_path = out_dir / f"latest.{tgt}.json"
        assert latest_path.is_file()
        raw_text = latest_path.read_text(encoding="utf-8")
        assert validate_no_privacy_leaks(raw_text, ctx) == []
        TargetInventory.model_validate(orjson.loads(raw_text))

    history_file = state_dir / "history.jsonl"
    assert history_file.is_file()
    lines = history_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert validate_no_privacy_leaks(lines[0], ctx) == []
    InventoryHistoryRunRecord.model_validate(orjson.loads(lines[0]))


def test_collect_inventory_degrades_gracefully_when_tools_or_targets_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "Users" / "alice"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    runner, _ = _fake_runner_factory(
        home_dir=fake_home,
        alive_targets={"host"},
        missing_tools={"agy", "claude", "codex", "opencode", "zcode"},
    )
    ctx = build_privacy_context(
        ssh_host="pc",
        runner=runner,
        extra_homes=[str(fake_home)],
        extra_users=["alice"],
    )
    resp = collect_inventory(
        requested_target="all",
        write_repo=False,
        repo_root=tmp_path / "repo",
        state_dir=tmp_path / "state",
        runner=runner,
        privacy_ctx=ctx,
    )
    assert resp.rc == 0
    assert resp.summary.targets_collected == 4
    assert resp.summary.targets_reachable == 1

    by_target = {inv.target.value: inv for inv in resp.inventories}
    assert by_target["host"].reachable is True
    assert by_target["host"].harnesses.agy.installed is False
    assert by_target["host"].harnesses.claude.installed is False
    assert by_target["host"].harnesses.codex.installed is False
    assert by_target["host"].harnesses.opencode.installed is False
    for down in ("pc", "mac", "vm"):
        assert by_target[down].reachable is False
        assert any(
            e.code is InventoryErrorCode.ERR_TARGET_UNREACHABLE
            for e in by_target[down].errors
        )


def test_collect_inventory_refuses_write_on_privacy_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "Users" / "alice"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    runner, _ = _fake_runner_factory(home_dir=fake_home, alive_targets={"host"})
    ctx = build_privacy_context(
        ssh_host="pc",
        runner=runner,
        extra_homes=[str(fake_home)],
        extra_users=["alice"],
    )

    # Bypass sanitize_model to prove pre-write leak validator fails closed.
    monkeypatch.setattr(collector_mod, "sanitize_model", lambda model, _ctx: model)

    out_dir = tmp_path / "repo" / "inventory"
    state_dir = tmp_path / "state"
    resp = collect_inventory(
        requested_target="host",
        write_repo=True,
        repo_root=tmp_path / "repo",
        out_dir=out_dir,
        state_dir=state_dir,
        runner=runner,
        privacy_ctx=ctx,
    )
    assert resp.rc == 1
    assert any(e.code is InventoryErrorCode.ERR_PRIVACY_LEAK for e in resp.errors)
    assert not (out_dir / "latest.host.json").exists()
    assert not (state_dir / "history.jsonl").exists()


def test_env_inventory_cli_invalid_target_and_help_smoke(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rc_bad = env_inventory_cli_main(["--target", "bogus"])
    assert rc_bad == 2
    bad_out = capsys.readouterr().out
    bad_payload = orjson.loads(bad_out)
    parsed_bad = EnvInventoryResponse.model_validate(bad_payload)
    assert parsed_bad.errors[0].code is InventoryErrorCode.ERR_INVALID_INPUT

    rc_unknown = env_inventory_cli_main(["--definitely-unknown-flag"])
    assert rc_unknown == 2
    err_out = capsys.readouterr().err
    err_payload = orjson.loads(err_out)
    EnvInventoryResponse.model_validate(err_payload)

    fake_home = tmp_path / "Users" / "alice"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))
    runner, _ = _fake_runner_factory(home_dir=fake_home, alive_targets={"host"})
    monkeypatch.setattr(collector_mod, "default_run_command", runner)

    rc_ok = env_inventory_cli_main(
        [
            "--target",
            "host",
            "--state-dir",
            str(tmp_path / "state"),
            "--json",
        ]
    )
    assert rc_ok == 0
    ok_out = capsys.readouterr().out
    ok_payload = orjson.loads(ok_out)
    parsed_ok = EnvInventoryResponse.model_validate(ok_payload)
    assert parsed_ok.summary.targets_collected == 1
    assert parsed_ok.summary.targets_reachable == 1

    task_script = REPO_ROOT / "tasks" / "env" / "inventory"
    assert task_script.is_file()
    proc = subprocess.run(
        [str(task_script), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--target" in proc.stdout
    assert "--write-repo" in proc.stdout
    assert "--out-dir" in proc.stdout

    assert isinstance(InventoryReturnCode.RET_OK.value, str)
    assert sys.version_info >= (3, 15)


def test_parallel_container_up_and_env_doctor_and_buildx_flags() -> None:
    container_up = (REPO_ROOT / "tasks" / "container" / "up").read_text(
        encoding="utf-8"
    )
    for required in (
        "docker buildx build",
        "--load",
        "--sbom=true",
        "--provenance=mode=max",
        "--metadata-file",
        "buildx-pc.metadata.json",
        "buildx-mac.metadata.json",
        '"${build_args[@]:1}"',
        "run_all_containers",
    ):
        assert required in container_up, f"Missing {required!r} in tasks/container/up"

    env_doctor = (REPO_ROOT / "tasks" / "env" / "doctor").read_text(encoding="utf-8")
    for required in (
        "log_pc=",
        "log_mac=",
        "log_vm=",
        'wait "${pid_pc}"',
        'wait "${pid_mac}"',
        'wait "${pid_vm}"',
        "[[ ${fail_count} -eq 0 ]]",
    ):
        assert required in env_doctor, f"Missing {required!r} in tasks/env/doctor"
