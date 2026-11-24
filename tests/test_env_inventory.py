"""Tests for schema-first `env_inventory`, privacy sanitization, BuildKit/parallel tasks, and CLI."""

from __future__ import annotations

import json
import shutil
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
    timed_out_tools: set[str] | None = None,
    failing_cmds: set[str] | None = None,
    corrupt_json_cmds: set[str] | None = None,
    ssh_host_reply: tuple[int, str, str] = (0, "hostname private-linux-box.lan\n", ""),
) -> tuple[Any, list[list[str]]]:
    alive = {"host", "pc", "mac", "vm"} if alive_targets is None else alive_targets
    missing = set() if missing_tools is None else missing_tools
    timed_out = set() if timed_out_tools is None else timed_out_tools
    failing = set() if failing_cmds is None else failing_cmds
    corrupt = set() if corrupt_json_cmds is None else corrupt_json_cmds
    invoked: list[list[str]] = []

    def _harness_and_mise(
        tool_cmd: list[str], prefix_home: str
    ) -> tuple[int, str, str]:
        if not tool_cmd:
            return 1, "", "empty command"
        exe = tool_cmd[0]
        cmd_str = " ".join(tool_cmd)
        if exe in timed_out or cmd_str in timed_out:
            return 124, "", f"timed out: {cmd_str}"
        if exe in missing:
            return 127, "", f"command not found: {exe}"
        for pat in failing:
            if pat in cmd_str:
                return 2, "", f"failed: {pat}"
        for pat in corrupt:
            if pat in cmd_str:
                return 0, "{not-valid-json", ""

        if exe == "uname":
            if prefix_home == "/home/dev":
                # Distinct arches per container target tested via prefix or caller; see docker inspect too.
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

        return 1, "", f"unmocked command: {cmd_str}"

    def _runner(
        argv: list[str],
        timeout: float = 15.0,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        _ = (timeout, env)
        invoked.append(list(argv))
        if argv[:2] == ["ssh", "-G"]:
            return ssh_host_reply
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
                                "RepoTags": ["dotfiles-v2:dev"],
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
                inner_idx = rest.index("dotfiles-v2") + 1
                inner_cmd = rest[inner_idx:]
                if inner_cmd == ["uname", "-ms"]:
                    return 0, ("Linux x86_64\n" if is_pc else "Linux aarch64\n"), ""
                return _harness_and_mise(inner_cmd, "/home/dev")

        # VM tart commands
        if argv[:2] == ["tart", "exec"]:
            if "vm" not in alive:
                return 1, "", "VM is not running"
            if argv[2:] == ["dotfiles-v2-macos", "true"]:
                return 0, "", ""
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


def test_privacy_sanitizer_strips_paths_users_hostnames_tailnet_and_tokens() -> None:
    ctx = build_privacy_context(
        ssh_host="pc",
        runner=lambda argv, _t, _e: (
            (0, "hostname secret-pc-node.lan\n", "")
            if argv[:2] == ["ssh", "-G"]
            else (1, "", "")
        ),
        extra_homes=["/Users/alice", "/home/bob"],
        extra_users=["alice", "bob"],
        extra_hostnames={"host": ["Alice-MacBook-Pro.local", "nas"]},
    )
    gh_tok = "ghp_" + "ExampleSecretToken98765"
    pat_tok = "github_pat_" + "11ABC_SecretExample99"
    sk_tok = "sk-" + "proj-SecretKeyValue1234"
    ts_tok = "tskey-" + "auth-ExampleKey12345"
    dp_tok = "dp.st." + "prd.ExampleSecret123"
    slack_tok = "xoxb-" + "123456-abcdef-Secret"
    bearer_tok = "Bearer " + "OpaqueJwtToken9988"

    sample = (
        f"/Users/alice/.local/bin/claude on Alice-MacBook-Pro.local and NAS "
        f"reaching /home/bob/dev via secret-pc-node.lan and mynode.tail12345.ts.net "
        f"owned by ALICE and bob with {gh_tok} {pat_tok} {sk_tok} {ts_tok} "
        f"{dp_tok} {slack_tok} {bearer_tok} https://alice:hunter2@proxy.example.com/api"
    )
    cleaned = sanitize_text(sample, ctx)
    for forbidden in (
        "/Users/alice",
        "/home/bob",
        "alice",
        "bob",
        "secret-pc-node",
        "Alice-MacBook-Pro",
        "ts.net",
        gh_tok,
        pat_tok,
        sk_tok,
        ts_tok,
        dp_tok,
        slack_tok,
        "OpaqueJwtToken9988",
        "hunter2",
    ):
        assert forbidden.lower() not in cleaned.lower(), (
            f"{forbidden!r} leaked in {cleaned!r}"
        )
    assert "nas" not in cleaned.lower().split()
    assert "~/.local/bin/claude" in cleaned
    assert "~/dev" in cleaned
    assert "<tailnet-host>" in cleaned
    assert "<redacted>" in cleaned
    assert "https://<redacted>@proxy.example.com/api" in cleaned
    assert validate_no_privacy_leaks(cleaned, ctx) == []

    # Leak detector flags raw private paths, users, short/long hostnames, .ts.net, and tokens
    assert validate_no_privacy_leaks('{"path": "/Users/alice/foo"}', ctx)
    assert validate_no_privacy_leaks('{"path": "/home/bob/foo"}', ctx)
    assert validate_no_privacy_leaks('{"host": "secret-pc-node.lan"}', ctx)
    assert validate_no_privacy_leaks('{"host": "NAS"}', ctx)
    assert validate_no_privacy_leaks('{"owner": "ALICE"}', ctx)
    assert validate_no_privacy_leaks('{"url": "https://host.tailnet.ts.net"}', ctx)
    assert validate_no_privacy_leaks(f'{{"tok": "{gh_tok}"}}', ctx)
    assert validate_no_privacy_leaks(f'{{"tok": "{dp_tok}"}}', ctx)
    assert validate_no_privacy_leaks('{"url": "https://user:pass@example.com"}', ctx)

    # sanitize_model sanitizes nested models and re-validates strictly
    dirty_harness = HarnessStatus(
        name="agy",
        installed=True,
        version="1.5.16",
        skills=["/Users/alice/.agents/skills/fnox", "node.tailnet.ts.net"],
    )
    clean_harness = sanitize_model(dirty_harness, ctx)
    assert clean_harness.skills == ["~/.agents/skills/fnox", "<tailnet-host>"]


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
                    "image.name": "docker.io/library/dotfiles-v2:dev",
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
    # Note ssh -G failure + custom ssh_host + VM-discovered /Users/vmtester without passing explicit privacy_ctx!
    runner, invoked = _fake_runner_factory(
        home_dir=fake_home,
        ssh_host_reply=(1, "", "ssh config missing"),
    )

    resp = collect_inventory(
        requested_target="all",
        ssh_host="custom-pc-alias",
        write_repo=True,
        repo_root=tmp_path / "repo",
        out_dir=out_dir,
        state_dir=state_dir,
        runner=runner,
    )
    assert resp.rc == 0
    assert resp.summary.targets_collected == 4
    assert resp.summary.targets_reachable == 4
    assert len(resp.inventories) == 4
    assert not any("--smoke" in cmd for cmd in invoked)

    by_target = {inv.target.value: inv for inv in resp.inventories}
    assert by_target["host"].container is None
    assert by_target["vm"].container is None
    assert by_target["pc"].arch == "x86_64"
    assert by_target["mac"].arch == "aarch64"
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

    # Verify propagated VM user/home and non-canonical ssh_host were scrubbed everywhere
    ctx_check = build_privacy_context(
        ssh_host="custom-pc-alias",
        runner=runner,
        extra_homes=[str(fake_home), "/Users/vmtester"],
        extra_users=["alice", "vmtester"],
    )
    for tgt in ("host", "pc", "mac", "vm"):
        latest_path = out_dir / f"latest.{tgt}.json"
        assert latest_path.is_file()
        raw_text = latest_path.read_text(encoding="utf-8")
        assert "vmtester" not in raw_text
        assert "custom-pc-alias" not in raw_text
        assert validate_no_privacy_leaks(raw_text, ctx_check) == []
        TargetInventory.model_validate(orjson.loads(raw_text))

    history_file = state_dir / "history.jsonl"
    assert history_file.is_file()
    lines = history_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "vmtester" not in lines[0]
    assert validate_no_privacy_leaks(lines[0], ctx_check) == []
    InventoryHistoryRunRecord.model_validate(orjson.loads(lines[0]))


def test_buildx_stale_metadata_ignored_and_down_container_arch_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "Users" / "alice"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    # 1. Digest mismatch on inspected image -> stale metadata ignored
    (state_dir / "buildx-mac.metadata.json").write_text(
        json.dumps(
            {
                "image.name": "dotfiles-v2:dev",
                "containerimage.digest": "sha256:stale-other-digest",
                "buildx.build.provenance": {
                    "buildType": "https://mobyproject.org/buildkit@v1",
                    "materials": [{"uri": "pkg:docker/docker/buildkit-syft-scanner"}],
                },
            }
        ),
        encoding="utf-8",
    )
    # 2. Image name mismatch on inspected image -> stale metadata ignored
    (state_dir / "buildx-pc.metadata.json").write_text(
        json.dumps(
            {
                "image.name": "other-repo:latest",
                "containerimage.digest": "sha256:aaaapc",
                "buildx.build.provenance": {
                    "buildType": "https://mobyproject.org/buildkit@v1",
                },
            }
        ),
        encoding="utf-8",
    )

    runner, _ = _fake_runner_factory(home_dir=fake_home)
    ctx = build_privacy_context(
        ssh_host="pc",
        runner=runner,
        extra_homes=[str(fake_home)],
        extra_users=["alice"],
    )
    resp = collect_inventory(
        requested_target="all",
        state_dir=state_dir,
        runner=runner,
        privacy_ctx=ctx,
    )
    by_target = {inv.target.value: inv for inv in resp.inventories}
    for tgt in ("mac", "pc"):
        meta = by_target[tgt].container
        assert meta is not None
        assert meta.inspected is True
        assert meta.buildx_metadata_present is False
        assert meta.sbom_present is False
        assert meta.provenance_present is False
        assert any(
            "ignored stale buildx metadata" in (r.message or "")
            for r in by_target[tgt].returns
        )

    # 3. When image is not inspected (unreachable), do not load buildx metadata and normalize arch when inspected+stopped
    def _stopped_with_image(
        argv: list[str], timeout: float = 15.0, env: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        if "container" in argv and "inspect" in argv:
            return 0, "false\n", ""
        return runner(argv, timeout, env)

    resp_stopped = collect_inventory(
        requested_target="mac",
        state_dir=state_dir,
        runner=_stopped_with_image,
        privacy_ctx=ctx,
    )
    assert resp_stopped.inventories[0].reachable is False
    assert resp_stopped.inventories[0].arch == "aarch64"

    # Uninspected image must not load metadata even if metadata file matches name
    (state_dir / "buildx-mac.metadata.json").write_text(
        json.dumps(
            {
                "image.name": "dotfiles-v2:dev",
                "buildx.build.provenance": {
                    "buildType": "https://mobyproject.org/buildkit@v1"
                },
            }
        ),
        encoding="utf-8",
    )
    runner_down, _ = _fake_runner_factory(home_dir=fake_home, alive_targets=set())
    resp_uninspected = collect_inventory(
        requested_target="mac",
        state_dir=state_dir,
        runner=runner_down,
        privacy_ctx=ctx,
    )
    assert resp_uninspected.inventories[0].container is not None
    assert resp_uninspected.inventories[0].container.inspected is False
    assert resp_uninspected.inventories[0].container.buildx_metadata_present is False


def test_collect_inventory_timeouts_failures_and_degraded_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "Users" / "alice"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    runner, _ = _fake_runner_factory(
        home_dir=fake_home,
        alive_targets={"host"},
        missing_tools={"agy", "opencode", "zcode"},
        timed_out_tools={"claude"},
        failing_cmds={"codex plugin list --json"},
        corrupt_json_cmds={"codex mcp list --json"},
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
    host_inv = by_target["host"]
    assert host_inv.reachable is True
    assert host_inv.harnesses.agy.installed is False
    assert host_inv.harnesses.claude.installed is False
    assert host_inv.harnesses.opencode.installed is False
    assert host_inv.harnesses.codex.installed is True
    assert any(e.code is InventoryErrorCode.ERR_TIMEOUT for e in host_inv.errors)
    assert any(
        e.code is InventoryErrorCode.ERR_SUBPROCESS_FAILED for e in host_inv.errors
    )
    for down in ("pc", "mac", "vm"):
        assert by_target[down].reachable is False
        assert any(
            e.code is InventoryErrorCode.ERR_TARGET_UNREACHABLE
            for e in by_target[down].errors
        )


def test_collect_inventory_zero_writes_if_history_jsonl_leaks(
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

    # First N calls pass (for latest.host.json); leak only on history.jsonl call
    orig_validate = collector_mod.validate_no_privacy_leaks
    call_count = 0

    def _leak_on_second(text: str, c: Any) -> list[str]:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            return ["simulated history leak"]
        return orig_validate(text, c)

    monkeypatch.setattr(collector_mod, "validate_no_privacy_leaks", _leak_on_second)

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


def test_skills_probe_py_resilient_to_corrupt_or_null_manifest(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".agents" / "skills"
    (skills_dir / "alpha-skill").mkdir(parents=True)
    (skills_dir / "beta-skill").mkdir(parents=True)

    # 1. Corrupt JSON manifest still returns skills list
    (skills_dir / ".mise-skills.json").write_text("{corrupt", encoding="utf-8")
    proc1 = subprocess.run(
        [sys.executable, "-c", collector_mod._SKILLS_PROBE_PY, str(skills_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    d1 = json.loads(proc1.stdout)
    assert d1 == {"skills": ["alpha-skill", "beta-skill"], "mise_skill_links": []}

    # 2. "links": null still returns skills list cleanly
    (skills_dir / ".mise-skills.json").write_text('{"links": null}', encoding="utf-8")
    proc2 = subprocess.run(
        [sys.executable, "-c", collector_mod._SKILLS_PROBE_PY, str(skills_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    d2 = json.loads(proc2.stdout)
    assert d2 == {"skills": ["alpha-skill", "beta-skill"], "mise_skill_links": []}


def test_env_inventory_cli_empty_target_json_logging_and_help_smoke(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Empty requested_target returns rc=2 ERR_INVALID_INPUT without ValidationError
    resp_empty = collect_inventory(requested_target="")
    assert resp_empty.rc == 2
    assert resp_empty.summary.requested_target == "<empty>"
    assert resp_empty.errors[0].code is InventoryErrorCode.ERR_INVALID_INPUT

    rc_bad = env_inventory_cli_main(["--target", "bogus"])
    assert rc_bad == 2
    bad_out = capsys.readouterr().out
    parsed_bad = EnvInventoryResponse.model_validate(orjson.loads(bad_out))
    assert parsed_bad.errors[0].code is InventoryErrorCode.ERR_INVALID_INPUT

    rc_unknown = env_inventory_cli_main(["--definitely-unknown-flag"])
    assert rc_unknown == 2
    err_out = capsys.readouterr().err
    EnvInventoryResponse.model_validate(orjson.loads(err_out))

    # Mutating --write-repo --json keeps stdout pure JSON, stderr clean, and writes INFO to env-inventory.log.jsonl
    fake_home = tmp_path / "Users" / "alice"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))
    runner, _ = _fake_runner_factory(home_dir=fake_home, alive_targets={"host"})
    monkeypatch.setattr(collector_mod, "default_run_command", runner)

    state_dir = tmp_path / "state"
    out_dir = tmp_path / "repo" / "inventory"
    rc_ok = env_inventory_cli_main(
        [
            "--target",
            "host",
            "--write-repo",
            "--repo",
            str(tmp_path / "repo"),
            "--out-dir",
            str(out_dir),
            "--state-dir",
            str(state_dir),
            "--json",
        ]
    )
    assert rc_ok == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    parsed_ok = EnvInventoryResponse.model_validate(orjson.loads(captured.out))
    assert parsed_ok.summary.targets_collected == 1
    assert parsed_ok.summary.targets_reachable == 1

    log_file = state_dir / "env-inventory.log.jsonl"
    assert log_file.is_file()
    log_rows = [
        orjson.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(row.get("event") == "env_inventory_collected" for row in log_rows)

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

    # _format_json_with_oxfmt produces oxfmt --check compliant JSON when oxfmt is installed
    written_host = out_dir / "latest.host.json"
    assert written_host.is_file()
    if shutil.which("oxfmt"):
        ox_proc = subprocess.run(
            ["oxfmt", "--check", str(written_host)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert ox_proc.returncode == 0, ox_proc.stdout + ox_proc.stderr


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
