"""Unit tests for schema-first AST, schema contract, and doctor prevention gates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tasks_py_loader import load_module


def _load_task_module(name: str):
    return load_module(name)


def test_schema_first_ast_bans_handwritten_dtos_and_allows_generated_and_ignore() -> (
    None
):
    ast_mod = _load_task_module("schema_first_ast")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bad_file = root / "pkg" / "bad.py"
        bad_file.parent.mkdir(parents=True)
        bad_file.write_text(
            "from dataclasses import dataclass\n"
            "from enum import StrEnum\n"
            "from pydantic import BaseModel\n"
            "@dataclass\nclass Foo:\n    x: int\n"
            "class Bar(BaseModel):\n    y: str\n"
            "class Baz(StrEnum):\n    A = 'a'\n",
            encoding="utf-8",
        )
        violations = ast_mod.check_file(bad_file, root)
        assert len(violations) == 3

        ignored_file = root / "pkg" / "ok_handle.py"
        ignored_file.write_text(
            "from dataclasses import dataclass\n"
            "@dataclass  # schema-first-ignore: runtime handle only\n"
            "class RuntimeHandle:\n    fd: int\n",
            encoding="utf-8",
        )
        assert ast_mod.check_file(ignored_file, root) == []

        gen_file = (
            root
            / "unix"
            / "home"
            / ".config"
            / "mise"
            / "lib"
            / "dotfiles_py"
            / "generated"
            / "models.py"
        )
        gen_file.parent.mkdir(parents=True)
        gen_file.write_text(
            "from pydantic import BaseModel\nclass M(BaseModel):\n    x: int\n",
            encoding="utf-8",
        )
        assert ast_mod.check_file(gen_file, root) == []

        sub_and_call = root / "pkg" / "bypass.py"
        sub_and_call.write_text(
            "from enum import Enum as AliasEnum\n"
            "from typing import TypedDict\n"
            "from pydantic import RootModel\n"
            "class R(RootModel[dict[str, int]]):\n    pass\n"
            "DynTD = TypedDict('DynTD', {'a': int})\n",
            encoding="utf-8",
        )
        bypass_errs = ast_mod.check_file(sub_and_call, root)
        assert len(bypass_errs) == 3


def test_schema_contract_check_rejects_missing_additional_properties_and_envelope() -> (
    None
):
    contract_mod = _load_task_module("schema_contract_check")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bad_schema = root / "schemas" / "bad.schema.json"
        bad_schema.parent.mkdir(parents=True)
        bad_schema.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "https://dotfiles-v2.local/schemas/bad.schema.json",
                    "$defs": {
                        "BadResponse": {
                            "type": "object",
                            "required": ["ok"],
                            "properties": {"ok": {"type": "boolean"}},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        violations = contract_mod.check_schema_file(bad_schema, root)
        assert any("additionalProperties" in v for v in violations)
        assert any("missing required field `timestamp`" in v for v in violations)


def _clean_git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["HK"] = "0"
    return env


def test_doctor_checks_no_project_tools_and_pre_cutover_gate(monkeypatch) -> None:
    doc_mod = _load_task_module("doctor_checks")
    git_env = _clean_git_env()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(
            ["git", "init", str(root)], env=git_env, check=True, capture_output=True
        )
        (root / "mise.toml").write_text(
            "[settings]\nexperimental = true\n", encoding="utf-8"
        )
        (root / "mise.local.toml").write_text(
            '[tools]\nruff = "0.17.6"\n', encoding="utf-8"
        )
        (root / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["ruff"]\n[tool.uv]\npackage = true\n',
            encoding="utf-8",
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "add",
                "mise.toml",
                "mise.local.toml",
                "pyproject.toml",
            ],
            env=git_env,
            check=True,
            capture_output=True,
        )
        errs = doc_mod.check_no_project_tools(root)
        assert any("mise.local.toml" in e for e in errs)
        assert any("[tool.uv].package must be false" in e for e in errs)
        assert any("ruff" in e for e in errs)

        # Verify pre-cutover gate passes when ~/.config/mise is not yet symlinked to dotfiles-v2.
        fake_home = root / "home"
        (fake_home / ".config" / "mise").mkdir(parents=True)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        host_errs, msg = doc_mod.check_host_option_a(root, strict_host=False)
        assert host_errs == []
        assert "PRE-CUTOVER" in msg

        # Strict mode fails when ~/.config/mise is not cut over.
        cfg_file = root / "unix" / "home" / ".config" / "mise" / "config.toml"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text("[tools]\n", encoding="utf-8")
        monkeypatch.setattr(
            doc_mod.subprocess,
            "check_output",
            lambda cmd, **_kw: "{}" if "ls" in cmd else "[]",
        )
        strict_errs, _ = doc_mod.check_host_option_a(root, strict_host=True)
        assert any("host-mise-parity" in e for e in strict_errs)
        assert os.path.isdir(fake_home)


def test_schema_contract_check_nested_composites_and_doctor_multi_lock_entries(
    monkeypatch,
) -> None:
    contract_mod = _load_task_module("schema_contract_check")
    doc_mod = _load_task_module("doctor_checks")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bad_schema = root / "schemas" / "nested.schema.json"
        bad_schema.parent.mkdir(parents=True)
        bad_schema.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "https://dotfiles-v2.local/schemas/nested.schema.json",
                    "$defs": {
                        "Wrapper": {
                            "anyOf": [
                                {
                                    "type": ["object", "null"],
                                    "properties": {"a": {"type": "string"}},
                                },
                                {"type": "null"},
                            ]
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        errs = contract_mod.check_schema_file(bad_schema, root)
        assert any("anyOf[0]" in e and "additionalProperties" in e for e in errs)

        mise_dir = root / "unix" / "home" / ".config" / "mise"
        mise_dir.mkdir(parents=True)
        (mise_dir / "config.toml").write_text(
            '[tools]\n"github:microsoft/vcpkg-tool" = "2026-10-29"\n\n'
            '[settings]\nlockfile_platforms = ["macos-arm64", "linux-x64"]\n',
            encoding="utf-8",
        )
        (mise_dir / "mise.lock").write_text(
            '[[tools."github:microsoft/vcpkg-tool"]]\nversion = "2026-10-29"\n'
            '[tools."github:microsoft/vcpkg-tool"."platforms.macos-arm64"]\nurl = "https://example.com/mac"\n'
            '[[tools."github:microsoft/vcpkg-tool"]]\nversion = "2026-10-29"\n'
            '[tools."github:microsoft/vcpkg-tool"."platforms.linux-x64"]\nurl = "https://example.com/linux"\n',
            encoding="utf-8",
        )
        assert doc_mod.check_mise_lock_integrity(root) == []

        # Verify `mise dot status --json` object schema catches missing/differs/orphaned
        fake_home = root / "home"
        v2_mise = root / "dotfiles-v2" / "unix" / "home" / ".config" / "mise"
        v2_mise.mkdir(parents=True)
        (v2_mise / "config.toml").write_text("[tools]\n", encoding="utf-8")
        (fake_home / ".config").mkdir(parents=True)
        (fake_home / ".config" / "mise").symlink_to(v2_mise)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        tasks_json = json.dumps(
            [
                {
                    "name": f"roll:{s}",
                    "source": str(v2_mise / "tasks" / "roll" / s),
                }
                for s in ("snapshot", "restart", "stop", "start", "verify", "self")
            ]
        )
        dot_json = json.dumps(
            {
                "files": [
                    {"target": "~/.zshrc", "state": "missing"},
                    {"target": "~/.zshenv", "state": "tracked"},
                ],
                "orphaned": [],
            }
        )
        monkeypatch.setattr(
            doc_mod.subprocess,
            "check_output",
            lambda cmd, **_kw: tasks_json if "tasks" in cmd else dot_json,
        )
        host_errs, _ = doc_mod.check_host_option_a(
            root / "dotfiles-v2", strict_host=True
        )
        assert any(
            "host-dotfiles-status: ~/.zshrc state=missing" in e for e in host_errs
        )
        assert not any("~/.zshenv" in e for e in host_errs)


def _init_git_repo_with_worktree(
    tmp_path: Path, env: dict[str, str]
) -> tuple[Path, Path]:
    main_repo = tmp_path / "dotfiles-v2"
    main_repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(main_repo)],
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(main_repo), "config", "user.name", "Test"],
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(main_repo), "config", "user.email", "t@example.invalid"],
        env=env,
        check=True,
        capture_output=True,
    )
    (main_repo / "unix" / "home" / ".config" / "mise").mkdir(parents=True)
    (main_repo / "unix" / "home" / ".config" / "mise" / "config.toml").write_text(
        "[tools]\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(main_repo), "add", "."],
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(main_repo), "commit", "-m", "init"],
        env=env,
        check=True,
        capture_output=True,
    )
    wt_repo = tmp_path / "dotfiles-v2-other-wt"
    subprocess.run(
        [
            "git",
            "-C",
            str(main_repo),
            "worktree",
            "add",
            "-b",
            "wt/other-wt",
            str(wt_repo),
            "HEAD",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    return main_repo, wt_repo


def test_strict_host_rejects_dangling_and_worktree_symlinks(monkeypatch) -> None:
    doc_mod = _load_task_module("doctor_checks")
    git_env = _clean_git_env()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        main_repo, wt_repo = _init_git_repo_with_worktree(tmp_path, git_env)
        fake_home = tmp_path / "home"
        (fake_home / ".config").mkdir(parents=True)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        real_check_output = subprocess.check_output
        monkeypatch.setattr(
            doc_mod.subprocess,
            "check_output",
            lambda cmd, **_kw: (
                real_check_output(cmd, env=git_env, text=True)
                if cmd[0] == "git"
                else (
                    json.dumps(
                        [
                            {
                                "name": f"roll:{s}",
                                "source": str(
                                    main_repo
                                    / "unix"
                                    / "home"
                                    / ".config"
                                    / "mise"
                                    / "tasks"
                                    / "roll"
                                    / s
                                ),
                            }
                            for s in (
                                "snapshot",
                                "restart",
                                "stop",
                                "start",
                                "verify",
                                "self",
                            )
                        ]
                    )
                    if "tasks" in cmd
                    else "{}"
                )
            ),
        )

        # 1. Dangling symlink rejected
        mise_link = fake_home / ".config" / "mise"
        mise_link.symlink_to(tmp_path / "missing-target")
        dangling_errs, _ = doc_mod.check_host_option_a(wt_repo, strict_host=True)
        assert any("host-mise-parity" in e for e in dangling_errs)

        # 2. Linked worktree symlink rejected
        mise_link.unlink()
        mise_link.symlink_to(wt_repo / "unix" / "home" / ".config" / "mise")
        wt_errs, _ = doc_mod.check_host_option_a(wt_repo, strict_host=True)
        assert any("host-mise-parity" in e for e in wt_errs)

        # 3. Exact main_root/unix/home/.config/mise passes even when invoked from wt_repo
        mise_link.unlink()
        mise_link.symlink_to(main_repo / "unix" / "home" / ".config" / "mise")
        ok_errs, _ = doc_mod.check_host_option_a(wt_repo, strict_host=True)
        assert not any("host-mise-parity" in e for e in ok_errs), ok_errs


def test_tier1_tier2_offline_gates_catch_regressions(monkeypatch) -> None:
    doc_mod = _load_task_module("doctor_checks")
    repo_root = Path(__file__).resolve().parents[1]

    # Live repo passes all offline checks
    assert doc_mod.check_dockerignore_secret_guard(repo_root) == []
    assert doc_mod.check_mise_version_parity(repo_root) == []
    assert doc_mod.check_config_hygiene(repo_root) == []
    assert doc_mod.check_usage_lint(repo_root) == []
    assert doc_mod.check_pitchfork_config(repo_root) == []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # .dockerignore guard catches missing/misordered exclusions
        (root / ".dockerignore").write_text(
            "*\n**/*.local.*\n!unix/home/.config/mise/**\n", encoding="utf-8"
        )
        d_errs = doc_mod.check_dockerignore_secret_guard(root)
        assert any("must appear after" in e for e in d_errs)
        assert any("**/__pycache__" in e for e in d_errs)

        # mise-version-parity catches skew
        (root / "docker" / "ubuntu").mkdir(parents=True)
        (root / "docker" / "ubuntu" / "Dockerfile").write_text(
            "FROM ghcr.io/jdx/mise:2026.11.13@sha256:0000 AS mise\n", encoding="utf-8"
        )
        (root / "mise.toml").write_text(
            'min_version = "2026.11.12"\n', encoding="utf-8"
        )
        v_errs = doc_mod.check_mise_version_parity(root)
        assert any("version skew detected" in e for e in v_errs)

        # config-hygiene catches non-canonical short name, missing prerelease, --standalone, missing proxy RUST_LOG, programs.json, and raw .. traversal
        mise_dir = root / "unix" / "home" / ".config" / "mise"
        mise_dir.mkdir(parents=True)
        (mise_dir / "config.toml").write_text(
            "[tools]\n"
            '"aqua:FiloSottile/age" = "1.3.2"\n'
            '"aqua:oxc-project/oxc/oxfmt" = "0.80.0"\n'
            '"npm:zcode-app-cli" = "3.18.2-48"\n\n'
            "[wrappers.opencode]\n"
            'command = "fnox"\n'
            'args = ["proxy", "run", "--", "opencode", "--standalone"]\n'
            'env = { RUST_LOG = "info" }\n\n'
            "[dotfiles]\n"
            '"~/.zshenv" = { source = "../../.zshenv", mode = "symlink" }\n'
            '"~/.claude.json/mcp-jdx" = { source = "../../.claude.mcp-jdx.json", merge = true }\n',
            encoding="utf-8",
        )
        prog_dir = root / "unix" / "home" / ".config" / "herdr-app"
        prog_dir.mkdir(parents=True)
        (prog_dir / "programs.json").write_text(
            json.dumps({"programs": {"codex": {"run": ["codex"]}}}), encoding="utf-8"
        )
        monkeypatch.setattr(
            doc_mod,
            "_load_mise_registry_map",
            lambda: {
                "aqua:FiloSottile/age": ("age", ["aqua:FiloSottile/age"]),
                "aqua:oxc-project/oxc/oxfmt": (
                    "oxfmt",
                    ["npm:oxfmt", "aqua:oxc-project/oxc/oxfmt"],
                ),
            },
        )
        c_errs = doc_mod.check_config_hygiene(root)
        assert any("canonical registry short name `age`" in e for e in c_errs)
        assert any("without documented opt-out" in e for e in c_errs)
        assert any("prerelease = true" in e for e in c_errs)
        assert any("not hardcode `--standalone`" in e for e in c_errs)
        assert any("fnox::commands::proxy=error" in e for e in c_errs)
        assert any("raw parent traversal" in e for e in c_errs)
        assert not any("mcp-jdx" in e for e in c_errs)
        assert any("must invoke `codex-hc`" in e for e in c_errs)


def test_live_host_gates_ci_skip_and_mise_audit_fixable(monkeypatch) -> None:
    doc_mod = _load_task_module("doctor_checks")
    repo_root = Path(__file__).resolve().parents[1]

    # CI skips live host runtime assertions cleanly
    monkeypatch.setenv("CI", "1")
    assert doc_mod.check_mise_doctor_clean()[0] == []
    assert doc_mod.check_host_hygiene(repo_root)[0] == []
    assert doc_mod.check_cli_wrappers_smoke()[0] == []

    # mise-doctor-clean fails on warnings even when rc=0
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        doc_mod.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=a[0],
            returncode=0,
            stdout=json.dumps({"errors": None, "warnings": ["PATH order warning"]}),
            stderr="",
        ),
    )
    doc_errs, _ = doc_mod.check_mise_doctor_clean()
    assert any("PATH order warning" in e for e in doc_errs)

    # cli-wrappers-smoke fails on Unrecognized flag or fnox proxy warning
    monkeypatch.setattr(doc_mod.shutil, "which", lambda cmd, **_kw: f"/bin/{cmd}")
    monkeypatch.setattr(
        doc_mod.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=a[0],
            returncode=0,
            stdout="",
            stderr="WARN fnox::commands::proxy: not sandboxing\n",
        ),
    )
    smoke_errs, _ = doc_mod.check_cli_wrappers_smoke()
    assert any("WARN fnox::commands::proxy" in e for e in smoke_errs)

    # mise-audit-fixable: unpatched python CVE-2026-124249 is advisory only; fixed advisory fails
    fixable_0, adv_1 = doc_mod.evaluate_mise_audit_payload(
        {
            "vulnerabilities": [
                {
                    "tool": "python",
                    "version": "3.15.2",
                    "id": "CVE-2026-124249",
                    "summary": "cookiejar",
                    "fixed": [],
                }
            ]
        }
    )
    assert fixable_0 == []
    assert len(adv_1) == 1 and "CVE-2026-124249" in adv_1[0]

    fixable_1, _ = doc_mod.evaluate_mise_audit_payload(
        {
            "vulnerabilities": [
                {
                    "tool": "python",
                    "version": "3.15.2",
                    "id": "CVE-2026-999999",
                    "summary": "fixable",
                    "fixed": ["3.15.3"],
                }
            ]
        }
    )
    assert len(fixable_1) == 1 and "3.15.3" in fixable_1[0]

    # check_cli_wrappers_smoke skips on container=docker and VM vm-env.sh
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("container", "docker")
    assert doc_mod.check_cli_wrappers_smoke()[0] == []
    monkeypatch.delenv("container", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        fake_home = Path(tmp) / "home"
        vm_env = fake_home / ".config" / "dotfiles-v2" / "vm-env.sh"
        vm_env.parent.mkdir(parents=True)
        vm_env.write_text("# vm\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        assert doc_mod.check_cli_wrappers_smoke()[0] == []

        # check_host_hygiene skips vb-zsh-check when ~/.config/board/env.sh is absent
        monkeypatch.setattr(doc_mod.platform, "system", lambda: "Darwin")
        called_cmds: list[list[str]] = []

        def _fake_run(cmd, **_kw):
            called_cmds.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

        monkeypatch.setattr(doc_mod.subprocess, "run", _fake_run)
        monkeypatch.setattr(doc_mod.subprocess, "check_output", lambda cmd, **_kw: "[]")
        (fake_home / ".agents" / "skills").mkdir(parents=True)
        (fake_home / ".agents" / "skills" / ".mise-skills.json").write_text(
            '{"links": {}}', encoding="utf-8"
        )
        doc_mod.check_host_hygiene(repo_root)
        assert not any(c[:2] == ["zsh", "-ic"] for c in called_cmds)

        # check_mise_lock_integrity catches orphan locks/ dir even with 0 npm:* tools
        orphan_root = Path(tmp) / "orphan-lock-repo"
        mise_dir = orphan_root / "unix" / "home" / ".config" / "mise"
        (mise_dir / "locks" / "npm-stale" / "1.0.0").mkdir(parents=True)
        (mise_dir / "config.toml").write_text(
            '[tools]\n[settings]\nlockfile_platforms = ["macos-arm64"]\n',
            encoding="utf-8",
        )
        (mise_dir / "mise.lock").write_text("[tools]\n", encoding="utf-8")
        (mise_dir / "mise.lock").chmod(0o644)
        orphan_errs = doc_mod.check_mise_lock_integrity(orphan_root)
        assert any("Unreferenced npm lock directory" in e for e in orphan_errs)

    # mise-audit-fixable fails when --rc 1 and vulnerabilities empty
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        empty_audit = tmp_path / "audit.json"
        empty_err = tmp_path / "audit.err"
        empty_audit.write_text('{"vulnerabilities": []}', encoding="utf-8")
        empty_err.write_text("network timeout\n", encoding="utf-8")
        rc = doc_mod.main(
            [
                "mise-audit-fixable",
                "--json-file",
                str(empty_audit),
                "--rc",
                "1",
                "--err-file",
                str(empty_err),
            ]
        )
        assert rc == 1


def test_doctor_online_uv_audit_nested_adverse_and_zcode_failures() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    online_task = repo_root / "tasks" / "doctor" / "online"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        shim_bin = tmp_path / "bin"
        shim_bin.mkdir()

        def _mk(name: str, body: str) -> None:
            p = shim_bin / name
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)

        _mk(
            "mise",
            "#!/bin/sh\n"
            'case " $* " in\n'
            '  *" outdated "*) echo "{}"; exit 0 ;;\n'
            '  *" audit "*) echo \'{"vulnerabilities":[]}\'; exit 0 ;;\n'
            '  *" secret:doctor "*) exit 0 ;;\n'
            "esac\n"
            "exit 0\n",
        )
        _mk(
            "uv",
            "#!/bin/sh\n"
            'echo \'{"vulnerabilities":[{"display_id":"GHSA-1234","dependency":{"name":"pkgA","version":"1.0"},"fix_versions":["1.1"]}],"adverse_statuses":[{"dependency":{"name":"pkgB","version":"2.0"},"status":"yanked"}]}\'\n'
            "exit 1\n",
        )
        _mk("zcode", "#!/bin/sh\necho 'daemon crash' >&2\nexit 1\n")

        env = {
            "HOME": str(fake_home),
            "PATH": f"{shim_bin}:/usr/bin:/bin:/opt/homebrew/bin:{Path(sys.executable).parent}",
        }
        proc = subprocess.run(
            ["/bin/bash", str(online_task)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 1
        assert "pkgA@1.0 GHSA-1234 (fixed in 1.1)" in proc.stderr
        assert "adverse status pkgB@2.0: yanked" in proc.stderr
        assert "FAIL  zcode-doctor" in proc.stderr


def test_opencode_wrapper_routing_and_worktree_rm_common_dir() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    wrapper = repo_root / "unix" / "home" / ".local" / "bin" / "opencode-wrapper"
    worktree_rm = repo_root / "tasks" / "worktree" / "rm"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_home = tmp_path / "home"
        oc_bin = fake_home / ".opencode" / "bin" / "opencode"
        oc_bin.parent.mkdir(parents=True)
        oc_bin.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n", encoding="utf-8")
        oc_bin.chmod(0o755)

        env = {"HOME": str(fake_home), "PATH": ""}
        for args, want_standalone in (
            ([], True),
            (["run", "hello"], True),
            (["run", "--help"], False),
            (["update", "--help"], False),
            (["upgrade", "--help"], False),
            (["mcp", "--help"], False),
            (["service", "--help"], False),
            (["--prompt", "--standalone", "--help"], False),
            (["--server", "http://127.0.0.1:4096"], False),
        ):
            proc = subprocess.run(
                ["/bin/bash", str(wrapper), *args],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            out = proc.stdout.strip()
            has_standalone = out.startswith("--standalone")
            assert has_standalone is want_standalone, f"args={args} out={out!r}"

        # tasks/worktree/rm resolves --git-common-dir from inside a sibling worktree
        git_env = _clean_git_env()
        main_repo, wt_repo = _init_git_repo_with_worktree(tmp_path, git_env)
        wt_two = tmp_path / "dotfiles-v2-rm-me"
        subprocess.run(
            [
                "git",
                "-C",
                str(main_repo),
                "worktree",
                "add",
                "-b",
                "wt/rm-me",
                str(wt_two),
                "HEAD",
            ],
            env=git_env,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["bash", str(worktree_rm)],
            cwd=wt_repo,
            env={**git_env, "usage_slug": "rm-me", "usage_force": "false"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert not wt_two.exists()


def test_post_task_learn_single_trigger_and_worktree_rm(monkeypatch) -> None:
    import importlib.util

    repo_root = Path(__file__).resolve().parents[1]
    hook_path = (
        repo_root
        / "unix"
        / "home"
        / ".gemini"
        / "config"
        / "hooks"
        / "post-task-learn.py"
    )
    spec = importlib.util.spec_from_file_location("post_task_learn", hook_path)
    assert spec is not None and spec.loader is not None
    hook_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook_mod)

    for var in ("AGY_DISABLE_AUTO_LEARN", "PLAN_ID", "PWF_PLAN_ROOT"):
        monkeypatch.delenv(var, raising=False)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        monkeypatch.setenv("XDG_STATE_HOME", str(root / "xdg-state"))
        ws = root / "repo"
        ws.mkdir()
        art = root / "art"
        art.mkdir()
        tr = root / "t.jsonl"

        # 1. Same-turn learn:record-review -> worktree:rm --force slug does NOT double-trigger
        tr.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "USER_INPUT",
                            "content": "<USER_REQUEST>Ship</USER_REQUEST>",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "PLANNER_RESPONSE",
                            "tool_calls": [
                                {
                                    "name": "edit_file",
                                    "args": {"Path": str(ws / "a.py")},
                                },
                                {
                                    "name": "run_command",
                                    "args": {
                                        "CommandLine": "mise run learn:record-review --plan-key /tmp/p/task_plan.md --plan-sha256 deadbeef --outcome no_new_proposals --survivor-count 0"
                                    },
                                },
                                {
                                    "name": "run_command",
                                    "args": {
                                        "CommandLine": "mise run worktree:rm --force wt-slug"
                                    },
                                },
                            ],
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        res1 = hook_mod._run_hook(
            {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(tr),
                "artifactDirectoryPath": str(art),
                "cwd": str(ws),
            }
        )
        assert res1 == {}, res1

        # 2. Cross-turn worktree:rm --force clears when shared reviews.jsonl has valid review
        reviews = Path(root / "xdg-state" / "agy-learn" / "reviews.jsonl")
        reviews.parent.mkdir(parents=True, exist_ok=True)
        reviews.write_text(
            json.dumps(
                {
                    "review_id": "rev-1",
                    "plan_key": str(
                        root
                        / "repo-wt-slug"
                        / ".planning"
                        / "2026-11-23-wt-slug"
                        / "task_plan.md"
                    ),
                    "plan_sha256": "abc123",
                    "outcome": "no_new_proposals",
                    "worktree_slug": "wt-slug",
                    "timestamp_unix_ns": 1000,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        tr2 = root / "t2.jsonl"
        art2 = root / "art2"
        art2.mkdir()
        tr2.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "USER_INPUT",
                            "content": "<USER_REQUEST>Clean up</USER_REQUEST>",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "PLANNER_RESPONSE",
                            "tool_calls": [
                                {
                                    "name": "run_command",
                                    "args": {
                                        "CommandLine": "mise run worktree:rm --force wt-slug"
                                    },
                                }
                            ],
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        res2 = hook_mod._run_hook(
            {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(tr2),
                "artifactDirectoryPath": str(art2),
                "cwd": str(ws),
            }
        )
        assert res2 == {}, res2

        # 3. Unreviewed worktree:rm fires continue, and chained worktree:rm fails closed on unreviewed slug
        tr3 = root / "t3.jsonl"
        art3 = root / "art3"
        art3.mkdir()
        tr3.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "USER_INPUT",
                            "content": "<USER_REQUEST>Clean up unreviewed</USER_REQUEST>",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "PLANNER_RESPONSE",
                            "tool_calls": [
                                {
                                    "name": "run_command",
                                    "args": {
                                        "CommandLine": "mise run worktree:rm --force wt-slug && mise run worktree:rm --force unreviewed-wt"
                                    },
                                }
                            ],
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        res3 = hook_mod._run_hook(
            {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(tr3),
                "artifactDirectoryPath": str(art3),
                "cwd": str(ws),
            }
        )
        assert res3.get("decision") == "continue"
        assert "unreviewed-wt" in res3.get("reason", "")

        # 4. Same-turn learn:record-review followed by later edit_file still triggers
        tr4 = root / "t4.jsonl"
        art4 = root / "art4"
        art4.mkdir()
        tr4.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "USER_INPUT",
                            "content": "<USER_REQUEST>More work</USER_REQUEST>",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "PLANNER_RESPONSE",
                            "tool_calls": [
                                {
                                    "name": "run_command",
                                    "args": {
                                        "CommandLine": "mise run learn:record-review --plan-key /tmp/p/task_plan.md --plan-sha256 deadbeef --outcome no_new_proposals --survivor-count 0"
                                    },
                                },
                                {
                                    "name": "edit_file",
                                    "args": {"Path": str(ws / "later.py")},
                                },
                            ],
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        res4 = hook_mod._run_hook(
            {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(tr4),
                "artifactDirectoryPath": str(art4),
                "cwd": str(ws),
            }
        )
        assert res4.get("decision") == "continue", res4

        # 5. _slug_matches_plan_key ignores unrelated ancestor path segments
        assert not hook_mod._slug_matches_plan_key(
            "v2", "/tmp/v2/repo-other/.planning/2026-11-23-other/task_plan.md"
        )
        assert hook_mod._slug_matches_plan_key(
            "other", "/tmp/v2/repo-other/.planning/2026-11-23-other/task_plan.md"
        )


def _load_doctor_checks():
    import importlib.util

    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "tasks" / "py" / "doctor_checks.py"
    spec = importlib.util.spec_from_file_location("doctor_checks", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return repo_root, mod


def test_iter_managed_entries_skips_runtime_caches_and_bounds_codex() -> None:
    _, doc_mod = _load_doctor_checks()

    with tempfile.TemporaryDirectory() as tmp:
        codex = Path(tmp) / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text("", encoding="utf-8")
        tmp_dir = codex / ".tmp" / "sub1" / "sub2"
        tmp_dir.mkdir(parents=True)
        (tmp_dir / "noise.txt").write_text("x", encoding="utf-8")
        deep_dir = codex / "sessions" / "day1"
        deep_dir.mkdir(parents=True)
        (deep_dir / "rollout.jsonl").write_text("{}", encoding="utf-8")

        entries_d1 = doc_mod._iter_managed_entries(codex, max_depth=1)
        names_d1 = {p.name for p in entries_d1}
        assert "config.toml" in names_d1
        assert "noise.txt" not in names_d1
        assert "rollout.jsonl" not in names_d1

        entries_d3 = doc_mod._iter_managed_entries(codex, max_depth=3)
        names_d3 = {p.name for p in entries_d3}
        assert "noise.txt" not in names_d3
        assert "rollout.jsonl" not in names_d3


def test_host_hygiene_shim_classification_and_ignored_tracked_configs(
    monkeypatch,
) -> None:
    import sys

    repo_root, doc_mod = _load_doctor_checks()

    monkeypatch.delenv("CI", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        mise_dir = home / ".config" / "mise"
        mise_dir.mkdir(parents=True)
        ignored_repo = Path(tmp) / "ignored-offline"
        ignored_repo.mkdir()
        ignored_cfg = ignored_repo / "mise.toml"
        ignored_cfg.write_text("[tools]\n", encoding="utf-8")
        (mise_dir / "miserc.toml").write_text(
            f'ignored_config_paths = ["{ignored_repo}"]\n',
            encoding="utf-8",
        )

        data_dir = home / ".local" / "share" / "mise"
        installs_dir = data_dir / "installs" / "lefthook" / "2.2.2"
        installs_dir.mkdir(parents=True)
        darwin_bin = installs_dir / "lefthook_2.2.2_MacOS_arm64"
        darwin_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        darwin_bin.chmod(0o755)

        shims_dir = data_dir / "shims"
        shims_dir.mkdir(parents=True)
        for sname in ("lefthook_2.2.2_macos_arm64", "cargo", "webclaw"):
            (shims_dir / sname).write_text("", encoding="utf-8")

        state_dir = home / ".local" / "state" / "mise"
        tracked = state_dir / "tracked-configs"
        tracked.mkdir(parents=True)
        (tracked / "abc123").symlink_to(ignored_cfg)

        skills_dir = home / ".agents" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / ".mise-skills.json").write_text(
            json.dumps({"links": {}}), encoding="utf-8"
        )

        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("MISE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("MISE_STATE_DIR", str(state_dir))

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["mise", "-C", str(home)] and cmd[3] == "which":
                tool = cmd[4]
                if tool == "cargo":
                    return subprocess.CompletedProcess(
                        cmd,
                        1,
                        stdout="",
                        stderr="mise ERROR cargo is a mise bin however it is not currently active.",
                    )
                if tool == "webclaw":
                    return subprocess.CompletedProcess(
                        cmd,
                        1,
                        stdout="",
                        stderr="mise ERROR No version is set for shim: webclaw.",
                    )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        def fake_check_output(cmd, **kwargs):
            if "bin-paths" in cmd:
                return f"{installs_dir}\n"
            if "skills" in cmd:
                return "[]"
            return ""

        monkeypatch.setattr(doc_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(doc_mod.subprocess, "check_output", fake_check_output)

        errs, _ = doc_mod.check_host_hygiene(repo_root)
        joined = "\n".join(errs)
        assert "broken-shim: webclaw" in joined, joined
        assert "broken-shim: cargo" not in joined, joined
        assert "ignored-tracked-config: abc123" in joined, joined
        if sys.platform == "darwin":
            assert "broken-shim: lefthook_2.2.2_macos_arm64" not in joined, joined
        miserc_txt = (
            repo_root / "unix" / "home" / ".config" / "mise" / "miserc.toml"
        ).read_text(encoding="utf-8")
        assert "~/dev/github/ray-manaloto/opencode/offline" in miserc_txt


def test_host_prune_uses_daemon_id_and_backend_mismatch_cleanup() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    prune_txt = (
        repo_root / "unix" / "home" / ".config" / "mise" / "tasks" / "host" / "prune"
    ).read_text(encoding="utf-8")
    assert '["pitchfork", "clean", "--daemon", daemon_id]' in prune_txt
    assert '["pitchfork", "clean", ns, "--daemon", name]' not in prune_txt
    assert "unused mismatched-backend tool" in prune_txt
    assert '["mise", "-C", str(home), "doctor", "--json"]' in prune_txt
    assert "check=False" in prune_txt
    assert (
        '["mise", "-C", str(home), "uninstall", "--all", "--yes", tool_name]'
        in prune_txt
    )


def test_strip_git_core_from_path_preserves_real_ordering(monkeypatch) -> None:
    _, doc_mod = _load_doctor_checks()
    home = Path.home()
    shims = f"{home}/.local/share/mise/shims"
    raw = os.pathsep.join(
        [
            "/opt/homebrew/opt/git/libexec/git-core",
            "/custom/git-exec",
            "/usr/lib/git-core/",
            shims,
            "/opt/homebrew/bin",
        ]
    )
    cleaned = doc_mod._strip_git_core_from_path(raw, "/custom/git-exec/")
    assert cleaned == f"{shims}{os.pathsep}/opt/homebrew/bin"

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PATH", raw)
    monkeypatch.setenv("GIT_EXEC_PATH", "/custom/git-exec")
    seen_env: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        seen_env.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"errors": [], "warnings": []}), stderr=""
        )

    monkeypatch.setattr(doc_mod.subprocess, "run", fake_run)
    errs, _ = doc_mod.check_mise_doctor_clean()
    assert errs == []
    assert seen_env["PATH"] == f"{shims}{os.pathsep}/opt/homebrew/bin"


def test_guest_shim_first_path_herdr_roll_dep_and_workspace_roll_ownership(
    monkeypatch,
) -> None:
    import tomllib

    repo_root, doc_mod = _load_doctor_checks()

    dockerfile = (repo_root / "docker" / "ubuntu" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "PATH=/opt/mise/shims:/home/dev/.opencode/bin:/home/dev/.local/bin:" in (
        dockerfile
    )

    c_env = (
        repo_root
        / "unix"
        / "home"
        / ".config"
        / "mise"
        / "assets"
        / "container"
        / "dotfiles-v2-env.sh"
    ).read_text(encoding="utf-8")
    assert (
        "for _df_p in /home/dev/.local/bin /home/dev/.opencode/bin /opt/mise/shims;"
        in (c_env)
    )

    vm_env = (
        repo_root / "unix" / "home" / ".config" / "mise" / "assets" / "vm" / "vm-env.sh"
    ).read_text(encoding="utf-8")
    assert (
        'for _df_p in /opt/homebrew/sbin /opt/homebrew/bin /usr/local/bin "${HOME}/.local/bin" "${HOME}/.opencode/bin" "${HOME}/.local/share/mise/shims";'
        in vm_env
    )

    env_doctor = (repo_root / "tasks" / "env" / "doctor").read_text(encoding="utf-8")
    assert (
        'export PATH="${HOME}/.local/share/mise/shims:${HOME}/.opencode/bin:${HOME}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:${PATH}"'
        in env_doctor
    )

    mise_toml = tomllib.loads((repo_root / "mise.toml").read_text(encoding="utf-8"))
    assert mise_toml["doctor"]["checks"]["herdr-roll"].get("depends") == "py-deps-fresh"

    monkeypatch.delenv("CI", raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_repo = root / "workspace"
        home = root / "home"
        (fake_repo / "unix" / "home" / ".config" / "mise" / "tasks" / "roll").mkdir(
            parents=True
        )
        (fake_repo / "unix" / "home" / ".config" / "mise" / "config.toml").write_text(
            "[tools]\n", encoding="utf-8"
        )
        for sub in ("snapshot", "restart", "stop", "start", "verify", "self"):
            (
                fake_repo
                / "unix"
                / "home"
                / ".config"
                / "mise"
                / "tasks"
                / "roll"
                / sub
            ).write_text("#!/bin/sh\n", encoding="utf-8")
        mise_link = home / ".config" / "mise"
        mise_link.parent.mkdir(parents=True)
        mise_link.symlink_to(fake_repo / "unix" / "home" / ".config" / "mise")
        monkeypatch.setenv("HOME", str(home))

        tasks_payload = [
            {
                "name": f"roll:{sub}",
                "source": str(home / ".config" / "mise" / "tasks" / "roll" / sub),
            }
            for sub in ("snapshot", "restart", "stop", "start", "verify", "self")
        ]

        def fake_check_output(cmd, **kwargs):
            if "ls" in cmd:
                return "{}"
            if "tasks" in cmd:
                return json.dumps(tasks_payload)
            if "dot" in cmd:
                return "{}"
            return ""

        monkeypatch.setattr(doc_mod.subprocess, "check_output", fake_check_output)
        errs, _ = doc_mod.check_host_option_a(fake_repo, strict_host=True)
        assert not [e for e in errs if e.startswith("roll-ownership:")], errs


def test_normalize_path_for_mise_doctor_shim_and_activated_modes(monkeypatch) -> None:
    _, doc_mod = _load_doctor_checks()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo_root = root / "workspace"
        home = root / "home"
        data_dir = root / "opt" / "mise"
        installs_dir = data_dir / "installs"
        wrappers_bin = data_dir / "command-wrappers" / "bin"
        shims_dir = data_dir / "shims"
        repo_venv = repo_root / ".venv" / "bin"
        other_venv = root / "other" / ".venv" / "bin"
        local_bin = home / ".local" / "bin"
        for d in (
            installs_dir / "hk" / "2.14.0" / "bin",
            wrappers_bin,
            shims_dir,
            repo_venv,
            other_venv,
            local_bin,
        ):
            d.mkdir(parents=True)

        raw_path = os.pathsep.join(
            [
                "/usr/libexec/git-core",
                str(wrappers_bin),
                str(repo_venv),
                str(installs_dir / "hk" / "2.14.0" / "bin"),
                str(shims_dir),
                str(other_venv),
                str(local_bin),
                "/usr/bin",
            ]
        )

        # Shim mode: unset MISE_SHELL / __MISE_DIFF strips installs, command-wrappers, repo .venv, git-core
        shim_env = {"MISE_DATA_DIR": str(data_dir)}
        assert doc_mod._normalize_path_for_mise_doctor(
            raw_path, shim_env, repo_root, home
        ) == os.pathsep.join(
            [str(shims_dir), str(other_venv), str(local_bin), "/usr/bin"]
        )

        # Shim mode MUST preserve real bugs (e.g. ~/.local/bin or /opt/homebrew/bin before shims)
        bad_order = os.pathsep.join(
            [
                str(wrappers_bin),
                str(local_bin),
                "/opt/homebrew/bin",
                str(shims_dir),
                "/usr/bin",
            ]
        )
        assert doc_mod._normalize_path_for_mise_doctor(
            bad_order, shim_env, repo_root, home
        ) == os.pathsep.join(
            [str(local_bin), "/opt/homebrew/bin", str(shims_dir), "/usr/bin"]
        )

        # Activated mode (MISE_SHELL or __MISE_DIFF set): preserves installs/wrappers/venv, strips git-core
        for active_env in (
            {"MISE_DATA_DIR": str(data_dir), "MISE_SHELL": "zsh"},
            {"MISE_DATA_DIR": str(data_dir), "__MISE_DIFF": "abc"},
        ):
            assert doc_mod._normalize_path_for_mise_doctor(
                raw_path, active_env, repo_root, home
            ) == os.pathsep.join(
                [
                    str(wrappers_bin),
                    str(repo_venv),
                    str(installs_dir / "hk" / "2.14.0" / "bin"),
                    str(shims_dir),
                    str(other_venv),
                    str(local_bin),
                    "/usr/bin",
                ]
            )

        # check_mise_doctor_clean uses real repo_root passed explicitly
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("MISE_SHELL", raising=False)
        monkeypatch.delenv("__MISE_DIFF", raising=False)
        monkeypatch.setenv("MISE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("PATH", raw_path)
        seen_env: dict[str, str] = {}

        def fake_run(cmd, **kwargs):
            seen_env.update(kwargs.get("env") or {})
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"errors": [], "warnings": []}), stderr=""
            )

        monkeypatch.setattr(doc_mod.subprocess, "run", fake_run)
        errs, _ = doc_mod.check_mise_doctor_clean(repo_root)
        assert errs == []
        assert seen_env["PATH"] == os.pathsep.join(
            [str(shims_dir), str(other_venv), str(local_bin), "/usr/bin"]
        )
