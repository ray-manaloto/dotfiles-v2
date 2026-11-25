"""Tests for schema-first cross-OS `deps:audit`."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from deps_audit import auditor
from deps_audit.auditor import audit_dependencies
from dotfiles_py.generated.deps_audit_schema import (
    DepsAuditReturnCode,
    DepsAuditSourceKind,
    DepsAuditStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_deps_audit_offline_covers_cross_os_configs_and_detects_skew(
    tmp_path: Path,
) -> None:
    mise_dir = tmp_path / "unix" / "home" / ".config" / "mise"
    mise_dir.mkdir(parents=True)
    docker_dir = tmp_path / "docker" / "ubuntu"
    docker_dir.mkdir(parents=True)
    (tmp_path / "tasks" / "vm").mkdir(parents=True)

    (tmp_path / "mise.toml").write_text(
        'min_version = "2026.11.14"\n', encoding="utf-8"
    )
    (mise_dir / "config.toml").write_text(
        'min_version = "2026.11.13"\n[tools]\nhk = { version = "2.14.0" }\n',
        encoding="utf-8",
    )
    (mise_dir / "config.container.toml").write_text(
        '[tools]\ntailscale = { version = "1.104.7", os = ["linux"] }\n'
        '[bootstrap.packages]\n"apt:zsh" = "5.9-8ubuntu3"\n',
        encoding="utf-8",
    )
    (docker_dir / "Dockerfile").write_text(
        "FROM --platform=linux/arm64 ghcr.io/jdx/mise:2026.11.14@sha256:deadbeef AS mise\n"
        "FROM scratch\n"
        "FROM ubuntu:26.10@sha256:cafebabe AS runtime\n",
        encoding="utf-8",
    )
    (tmp_path / "tasks" / "vm" / "up").write_text(
        'MISE_VER="2026.11.14"\npackslip install --pin ps1_abc123\n',
        encoding="utf-8",
    )
    (tmp_path / "hk.pkl").write_text(
        'amends "https://raw.githubusercontent.com/ray-manaloto/hk-config/v1.2.0/presets.pkl"\n'
        'import "package://github.com/jdx/hk/releases/download/v2.13.0/hk@2.13.0#/Builtins.pkl"\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = ["pydantic==2.14.4"]\n',
        encoding="utf-8",
    )

    resp = audit_dependencies(
        tmp_path,
        offline=True,
        sources={
            DepsAuditSourceKind.MISE_BINARY,
            DepsAuditSourceKind.MISE_TOOL,
            DepsAuditSourceKind.UV_PACKAGE,
            DepsAuditSourceKind.OCI_IMAGE,
            DepsAuditSourceKind.HK_PRESET,
        },
        fail_on_outdated=True,
    )
    assert re.fullmatch(
        r"[0-9]{8}T[0-9]{6}\.[0-9]{9}(?:Z|[+-][0-9]{4})", resp.timestamp
    )
    assert resp.rc == 1
    by_name = {item.name: item for item in resp.items}
    assert by_name["mise"].status == DepsAuditStatus.OUTDATED
    assert "tailscale" in by_name
    assert by_name["tailscale"].os_scope == ["linux"]
    assert by_name["apt:zsh"].status == DepsAuditStatus.PINNED_HELD
    assert by_name["jdx/hk-builtins"].status == DepsAuditStatus.OUTDATED
    assert by_name["pydantic"].current_version == "2.14.4"
    assert by_name["ubuntu:26.10"].current_version == "sha256:cafebabe"
    assert "scratch" not in by_name


def test_deps_audit_online_handles_unknowns_prereleases_pep440_tailscale_and_kb(
    tmp_path: Path, monkeypatch
) -> None:
    mise_dir = tmp_path / "unix" / "home" / ".config" / "mise"
    mise_dir.mkdir(parents=True)
    docker_dir = tmp_path / "docker" / "ubuntu"
    docker_dir.mkdir(parents=True)
    (tmp_path / "tasks" / "vm").mkdir(parents=True)
    (tmp_path / "tasks" / "env").mkdir(parents=True)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)

    (tmp_path / "mise.toml").write_text(
        'min_version = "2026.11.14"\n', encoding="utf-8"
    )
    (mise_dir / "config.toml").write_text(
        'min_version = "2026.11.14"\n[tools]\npython = "3.15.2"\nhk = "2.14.0"\n',
        encoding="utf-8",
    )
    (mise_dir / "config.container.toml").write_text(
        '[tools]\ntailscale = { version = "1.104.7", os = ["linux"] }\n',
        encoding="utf-8",
    )
    (docker_dir / "Dockerfile").write_text(
        "FROM --platform=linux/arm64 ghcr.io/jdx/mise:2026.11.14@sha256:aaaa\n",
        encoding="utf-8",
    )
    (tmp_path / "tasks" / "vm" / "up").write_text(
        'MISE_VER="2026.11.14"\nPACKSLIP_MISE_PIN="ps1_xyz"\n',
        encoding="utf-8",
    )
    (tmp_path / "tasks" / "env" / "doctor").write_text(
        'version_ge "${mise_ver}" "2026.11.14"\n', encoding="utf-8"
    )
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "steps:\n  - uses: jdx/mise-action@123\n    with:\n      version: 2026.11.14\n",
        encoding="utf-8",
    )
    (tmp_path / "hk.pkl").write_text(
        'amends "https://raw.githubusercontent.com/ray-manaloto/hk-config/v1.0.0/presets.pkl"\n'
        'import "package://github.com/jdx/hk/releases/download/v2.14.0/hk@2.14.0#/Builtins.pkl"\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n'
        'dependencies = ["dirty-equals==0.11.0", "dirty-equals==0.11.0"]\n',
        encoding="utf-8",
    )

    kb_dir = tmp_path / "kb"
    (kb_dir / "prototype" / "kb").mkdir(parents=True)
    kb_state = tmp_path / "kb_state"
    kb_state.mkdir()
    (kb_state / "pins.json").write_text(
        json.dumps(
            {
                "/tmp/repos/tailscale-tailscale": {
                    "name": "tailscale-tailscale",
                    "ref": "v1.104.7",
                    "commit": "abc123",
                },
                "/tmp/repos/mise-mise": {
                    "name": "mise-mise",
                    "ref": "v2026.11.14",
                    "commit": "def456",
                },
            }
        ),
        encoding="utf-8",
    )
    (kb_dir / "prototype" / "kb" / "__init__.py").write_text("", encoding="utf-8")
    (kb_dir / "prototype" / "kb" / "snap.py").write_text(
        "def outdated():\n"
        "    return [\n"
        "        {'name': 'tailscale-tailscale', 'ref': 'v1.104.7', 'state': 'OUTDATED', 'newest': 'v1.106.0'},\n"
        "        {'name': 'mise-mise', 'ref': 'v2026.11.14', 'state': 'current', 'newest': 'v2026.11.14'},\n"
        "    ]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KB_STATE", str(kb_state))

    # Offline KB reads $KB_STATE/pins.json using rec["name"]
    kb_off = audit_dependencies(
        tmp_path,
        offline=True,
        sources={DepsAuditSourceKind.KB_WORKSPACE},
        kb_root=kb_dir,
    )
    kb_names = {i.name: i for i in kb_off.items}
    assert "tailscale-tailscale" in kb_names
    assert kb_names["tailscale-tailscale"].status == DepsAuditStatus.PINNED_HELD
    assert kb_names["mise-mise"].status == DepsAuditStatus.CURRENT

    # Mocked online probes: failed upstream -> UNKNOWN + RET_PARTIAL; 3.16.0a2 skipped; 0.11.0 == 0.11; tailscale 404 -> PINNED_HELD
    monkeypatch.setattr(
        auditor, "_gh_latest_tag", lambda _repo: (None, "https://github.com/x/y")
    )
    monkeypatch.setattr(
        auditor.subprocess,
        "check_output",
        lambda cmd, **_kw: (
            (
                "3.15.2\n3.16.0a2\n"
                if cmd[:2] == ["mise", "ls-remote"] and cmd[2] == "python"
                else ("2.14.0\n" if cmd[2] == "hk" else "1.104.7\n1.106.0\n")
            )
            if cmd[:2] == ["mise", "ls-remote"]
            else (_ for _ in ()).throw(OSError("docker down"))
        ),
    )
    monkeypatch.setattr(
        auditor,
        "_http_json",
        lambda url: {"info": {"version": "0.11"}} if "dirty-equals" in url else None,
    )
    monkeypatch.setattr(auditor, "_http_head_exists", lambda _url: False)

    online = audit_dependencies(
        tmp_path,
        offline=False,
        sources={
            DepsAuditSourceKind.MISE_BINARY,
            DepsAuditSourceKind.MISE_TOOL,
            DepsAuditSourceKind.UV_PACKAGE,
            DepsAuditSourceKind.OCI_IMAGE,
            DepsAuditSourceKind.HK_PRESET,
            DepsAuditSourceKind.KB_WORKSPACE,
        },
        kb_root=kb_dir,
    )
    by_name = {i.name: i for i in online.items}
    assert by_name["mise"].status == DepsAuditStatus.UNKNOWN
    assert by_name["ray-manaloto/hk-config"].status == DepsAuditStatus.UNKNOWN
    assert by_name["jdx/hk-builtins"].status == DepsAuditStatus.UNKNOWN
    assert by_name["ghcr.io/jdx/mise:2026.11.14"].status == DepsAuditStatus.UNKNOWN
    assert by_name["python"].status == DepsAuditStatus.CURRENT
    assert by_name["dirty-equals"].status == DepsAuditStatus.CURRENT
    assert by_name["tailscale"].status == DepsAuditStatus.PINNED_HELD
    assert online.summary.returns[0].code == DepsAuditReturnCode.RET_PARTIAL


def test_deps_audit_cli_help_and_repo_offline_json(tmp_path: Path) -> None:
    help_proc = subprocess.run(
        ["bash", str(REPO_ROOT / "tasks" / "deps" / "audit"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "deps-audit" in help_proc.stdout
    assert "--offline" in help_proc.stdout

    json_proc = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "tasks" / "deps" / "audit"),
        ],
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_offline": "true",
            "usage_json": "true",
            "usage_state_dir": str(tmp_path / "state"),
            "usage_write_log": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(json_proc.stdout)
    assert payload["command"] == "deps:audit"
    assert payload["offline"] is True
    assert payload["rc"] == 0
    names = {i["name"] for i in payload["items"]}
    assert "tailscale" in names
    assert "agent-browser" in names
    assert "bun" in names
    hk_cfg = next(i for i in payload["items"] if i["name"] == "ray-manaloto/hk-config")
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", hk_cfg["current_version"])
    assert (tmp_path / "state" / "deps-audit.log.jsonl").is_file()
