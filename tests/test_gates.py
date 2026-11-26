"""Unit tests for schema-first AST, schema contract, and doctor prevention gates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
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
    env["XDG_CONFIG_HOME"] = "/dev/null"
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
        # GitHub Actions sets CI=true, which makes check_host_option_a skip host checks.
        monkeypatch.delenv("CI", raising=False)

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
            "FROM ghcr.io/jdx/mise:2026.11.14@sha256:0000 AS mise\n", encoding="utf-8"
        )
        (root / "mise.toml").write_text(
            'min_version = "2026.11.12"\n', encoding="utf-8"
        )
        v_errs = doc_mod.check_mise_version_parity(root)
        assert any("version skew detected" in e for e in v_errs)

        # config-hygiene catches non-canonical short name (with zero opt-out exceptions across config*.toml),
        # missing prerelease, programs.json, and raw .. traversal
        mise_dir = root / "unix" / "home" / ".config" / "mise"
        mise_dir.mkdir(parents=True)
        (mise_dir / "config.toml").write_text(
            "[tools]\n"
            '"aqua:FiloSottile/age" = "1.3.2"\n'
            "# Keep aqua backend explicitly\n"
            '"aqua:oxc-project/oxc/oxfmt" = "0.80.0"\n'
            '"aqua:anchore/syft" = "1.57.2"\n'
            '"npm:zcode-app-cli" = "3.18.2-48"\n\n'
            "[dotfiles]\n"
            '"~/.zshenv" = { source = "../../.zshenv", mode = "symlink" }\n',
            encoding="utf-8",
        )
        (mise_dir / "config.macos.toml").write_text(
            '[tools]\n"aqua:sharkdp/bat[exe=bat]" = "0.26.1"\noxfmt = "0.80.0-rc.1"\n',
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
                "aqua:filosottile/age": ("age", ["aqua:FiloSottile/age"]),
                "npm:oxfmt": ("oxfmt", ["npm:oxfmt", "aqua:oxc-project/oxc/oxfmt"]),
                "aqua:oxc-project/oxc/oxfmt": (
                    "oxfmt",
                    ["npm:oxfmt", "aqua:oxc-project/oxc/oxfmt"],
                ),
                "aqua:anchore/syft": ("syft", ["aqua:anchore/syft"]),
                "aqua:sharkdp/bat": ("bat", ["aqua:sharkdp/bat"]),
            },
        )
        c_errs = doc_mod.check_config_hygiene(root)
        assert any(
            "config.toml: use canonical mise registry short name `age`" in e
            for e in c_errs
        )
        assert any(
            "config.toml: use canonical mise registry short name `oxfmt`" in e
            for e in c_errs
        )
        assert any(
            "config.toml: use canonical mise registry short name `syft`" in e
            for e in c_errs
        )
        assert any(
            "config.macos.toml: use canonical mise registry short name `bat`" in e
            for e in c_errs
        )
        assert any(
            "config.macos.toml: hyphenated npm tool `oxfmt` (0.80.0-rc.1) must set `prerelease = true`"
            in e
            for e in c_errs
        )
        assert any("prerelease = true" in e and "zcode-app-cli" in e for e in c_errs)
        assert any("raw parent traversal" in e for e in c_errs)
        assert any("must invoke ['codex', '--no-daemon']" in e for e in c_errs)


def test_check_mise_lock_integrity_accepts_canonical_npm_registry_tool() -> None:
    doc_mod = _load_task_module("doctor_checks")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mise_dir = root / "unix" / "home" / ".config" / "mise"
        lock_pkg_dir = mise_dir / "locks" / "oxfmt" / "0.80.0"
        lock_pkg_dir.mkdir(parents=True)
        for fname in ("package.json", "aube-lock.yaml"):
            fpath = lock_pkg_dir / fname
            fpath.write_text("{}\n", encoding="utf-8")
            fpath.chmod(0o644)

        (mise_dir / "config.toml").write_text(
            '[tools]\noxfmt = "0.80.0"\n\n'
            '[settings]\nlockfile_platforms = ["macos-arm64"]\n',
            encoding="utf-8",
        )
        (mise_dir / "mise.lock").write_text(
            "[[tools.oxfmt]]\n"
            'version = "0.80.0"\n'
            'aube = { path = "locks/oxfmt/0.80.0", digest = "sha256:deadbeef" }\n'
            'backend = "npm:oxfmt"\n'
            'specifiers = ["0.80.0"]\n',
            encoding="utf-8",
        )
        (mise_dir / "mise.lock").chmod(0o644)
        assert doc_mod.check_mise_lock_integrity(root) == []

        (lock_pkg_dir / "aube-lock.yaml").unlink()
        missing_errs = doc_mod.check_mise_lock_integrity(root)
        assert any("aube-lock.yaml" in e for e in missing_errs)


def test_live_host_gates_ci_skip_and_mise_audit_fixable(monkeypatch) -> None:
    doc_mod = _load_task_module("doctor_checks")
    repo_root = Path(__file__).resolve().parents[1]

    # CI skips live host runtime assertions cleanly
    monkeypatch.setenv("CI", "1")
    assert doc_mod.check_mise_doctor_clean()[0] == []
    assert doc_mod.check_host_hygiene(repo_root)[0] == []
    assert doc_mod.check_mise_binary_integrity(repo_root)[0] == []

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

    # mise-binary-integrity validates clean repo hooks/dcg and catches missing MISE installs write-bit
    monkeypatch.setattr(doc_mod.shutil, "which", lambda _cmd: None)
    with tempfile.TemporaryDirectory() as clean_data:
        monkeypatch.setenv("MISE_DATA_DIR", clean_data)
        bin_errs, _ = doc_mod.check_mise_binary_integrity(repo_root)
        assert bin_errs == [], bin_errs

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

    # check_mise_binary_integrity ignores writable installs when CI=1, even if VM/container markers exist
    with tempfile.TemporaryDirectory() as tmp:
        fake_home = Path(tmp) / "home"
        vm_env = fake_home / ".config" / "dotfiles-v2" / "vm-env.sh"
        vm_env.parent.mkdir(parents=True)
        vm_env.write_text("# vm\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        tmp_data = Path(tmp) / "mise-data"
        bin_dir = tmp_data / "installs" / "sample" / "1.0" / "bin"
        bin_dir.mkdir(parents=True)
        writable_elf = bin_dir / "sample"
        writable_elf.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 16)
        writable_elf.chmod(0o755)
        monkeypatch.setenv("MISE_DATA_DIR", str(tmp_data))

        monkeypatch.setenv("CI", "1")
        monkeypatch.setenv("container", "docker")
        assert doc_mod.check_mise_binary_integrity(repo_root)[0] == []

        monkeypatch.delenv("CI", raising=False)
        live_errs, _ = doc_mod.check_mise_binary_integrity(repo_root)
        assert any("compiled binary(ies) writable" in e for e in live_errs), live_errs

        monkeypatch.delenv("container", raising=False)
        monkeypatch.setenv("MISE_DATA_DIR", str(Path(tmp) / "empty-mise-data"))
        assert doc_mod.check_mise_binary_integrity(repo_root)[0] == []

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


def test_worktree_rm_common_dir_and_dcg_hardening(monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    worktree_rm = repo_root / "tasks" / "worktree" / "rm"
    harden_task = (
        repo_root
        / "unix"
        / "home"
        / ".config"
        / "mise"
        / "tasks"
        / "host"
        / "harden-bins"
    )
    agy_dcg = (
        repo_root
        / "unix"
        / "home"
        / ".gemini"
        / "config"
        / "hooks"
        / "agy-dcg-guard.py"
    )
    doc_mod = _load_task_module("doctor_checks")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # 1. agy-dcg-guard handles empty stdin, Antigravity toolCall, ask/exit-2, and allow cleanly
        empty_p = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", str(agy_dcg)],
            input="   \n",
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(empty_p.stdout) == {"decision": "allow"}

        stub_dcg = tmp_path / "dcg"
        stub_dcg.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "assert '--agent' in sys.argv and 'antigravity' in sys.argv, sys.argv\n"
            "data = json.load(sys.stdin)\n"
            "cmd = (data.get('tool_input') or {}).get('command', '')\n"
            "if 'ln -sf' in cmd:\n"
            "    print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'deny', 'permissionDecisionReason': 'blocked'}}))\n"
            "elif 'ask-cmd' in cmd:\n"
            "    print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'ask', 'permissionDecisionReason': 'confirm risky'}}))\n"
            "elif 'exit2-cmd' in cmd:\n"
            "    sys.stderr.write('blocked by exit 2')\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        stub_dcg.chmod(0o755)
        for payload in (
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "ln -sf x ~/.local/share/mise/installs/y"},
                }
            },
            {
                "tool_input": {
                    "CommandLine": "ln -sf x ~/.local/share/mise/installs/y",
                }
            },
        ):
            deny_p = subprocess.run(
                ["/usr/bin/python3", "-I", "-B", str(agy_dcg)],
                input=json.dumps(payload),
                env={"DCG_BIN": str(stub_dcg), "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                check=True,
            )
            assert json.loads(deny_p.stdout) == {
                "decision": "deny",
                "reason": "blocked",
            }

        ask_p = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", str(agy_dcg)],
            input=json.dumps(
                {
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "ask-cmd"},
                    }
                }
            ),
            env={"DCG_BIN": str(stub_dcg), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(ask_p.stdout) == {
            "decision": "deny",
            "reason": "confirm risky",
        }

        exit2_p = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", str(agy_dcg)],
            input=json.dumps(
                {
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "exit2-cmd"},
                    }
                }
            ),
            env={"DCG_BIN": str(stub_dcg), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(exit2_p.stdout) == {
            "decision": "deny",
            "reason": "blocked by exit 2",
        }

        allow_p = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", str(agy_dcg)],
            input=json.dumps(
                {
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "ls -la ~/.local/share/mise/installs"},
                    }
                }
            ),
            env={"DCG_BIN": str(stub_dcg), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(allow_p.stdout) == {"decision": "allow"}

        # 2. host:harden-bins marks Mach-O/ELF binaries 0555 and leaves scripts/site-packages 0755
        installs_dir = tmp_path / "mise_data" / "installs"
        bin_dir = installs_dir / "tool" / "1.0" / "bin"
        sp_dir = installs_dir / "pkg" / "site-packages"
        bin_dir.mkdir(parents=True)
        sp_dir.mkdir(parents=True)
        elf_bin = bin_dir / "tool"
        elf_bin.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 16)
        elf_bin.chmod(0o755)
        shell_helper = bin_dir / "helper.sh"
        shell_helper.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        shell_helper.chmod(0o755)
        so_mod = sp_dir / "mod.so"
        so_mod.write_bytes(b"\x7fELF\x02\x01\x01\x00")
        so_mod.chmod(0o755)

        monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "mise_data"))
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(doc_mod.shutil, "which", lambda _cmd: None)
        pre_errs, _ = doc_mod.check_mise_binary_integrity(repo_root)
        assert any("compiled binary(ies) writable" in e for e in pre_errs), pre_errs

        subprocess.run(
            ["bash", str(harden_task)],
            env={
                "HOME": str(tmp_path),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_dir": str(installs_dir),
            },
            capture_output=True,
            text=True,
            check=True,
        )
        assert (elf_bin.stat().st_mode & 0o777) == 0o555
        assert (shell_helper.stat().st_mode & 0o777) == 0o755
        assert (so_mod.stat().st_mode & 0o777) == 0o755
        post_errs, _ = doc_mod.check_mise_binary_integrity(repo_root)
        assert post_errs == [], post_errs

        # 3. check_config_hygiene detects bidirectional [dotfiles] mismatch
        fx_repo = tmp_path / "fx_repo"
        fx_mise = fx_repo / "unix" / "home" / ".config" / "mise"
        fx_mise.mkdir(parents=True)
        (fx_repo / "unix" / "home" / ".zshenv").write_text(
            "# zshenv\n", encoding="utf-8"
        )
        (fx_repo / "unix" / "home" / ".extra-unregistered").write_text(
            "x\n", encoding="utf-8"
        )
        fx_prog = fx_repo / "unix" / "home" / ".config" / "herdr-app"
        fx_prog.mkdir(parents=True)
        (fx_prog / "programs.json").write_text(
            json.dumps(
                {
                    "programs": {
                        "antigravity": {"run": ["agy"]},
                        "codex": {"run": ["codex", "--no-daemon"]},
                        "opencode": {"run": ["opencode", "--standalone"]},
                    }
                }
            ),
            encoding="utf-8",
        )
        (fx_mise / "config.toml").write_text(
            '[dotfiles]\n"~/.zshenv" = { main_checkout = true }\n'
            '"~/.config/herdr-app/programs.json" = { main_checkout = true }\n'
            '"~/.missing-file" = { main_checkout = true }\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(doc_mod, "_load_mise_registry_map", lambda: {})
        parity_errs = doc_mod.check_config_hygiene(fx_repo)
        assert any(
            "unregistered dotfile unix/home/.extra-unregistered" in e
            for e in parity_errs
        )
        assert any("missing unix/home/.missing-file" in e for e in parity_errs)

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
        assert res1.get("decision") == "continue", res1

        # 2. Cross-turn & same-turn worktree:rm --force clears when shared reviews.jsonl has valid review
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
        art_cleared = root / "art_cleared"
        art_cleared.mkdir()
        res1_cleared = hook_mod._run_hook(
            {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(tr),
                "artifactDirectoryPath": str(art_cleared),
                "cwd": str(ws),
            }
        )
        assert res1_cleared == {}, res1_cleared
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


def test_vm_up_normalizes_usr_local_bin_ownership_for_packslip() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    vm_up = (repo_root / "tasks" / "vm" / "up").read_text(encoding="utf-8")
    install_idx = vm_up.index('"${tmp_dir}/packslip" install github.com/jdx/mise')
    for snippet in (
        "sudo install -d -o root -g wheel -m 0755 /usr/local/bin",
        "sudo chown root:wheel /usr/local /usr/local/bin",
        "sudo chmod 0755 /usr/local /usr/local/bin",
    ):
        assert vm_up.index(snippet) < install_idx


def test_doctor_checks_registry_aliases_and_container_lock_and_daemon_health(
    monkeypatch,
) -> None:
    doc_mod = _load_task_module("doctor_checks")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mise_dir = root / "unix" / "home" / ".config" / "mise"
        mise_dir.mkdir(parents=True)
        (mise_dir / "locks" / "extra" / "1.0.0").mkdir(parents=True)
        (mise_dir / "config.toml").write_text(
            '[tools]\noxfmt = "0.80.0"\nbat = { version = "0.26.1" }\n'
            '[settings]\nlockfile_platforms = ["macos-arm64"]\n',
            encoding="utf-8",
        )
        # Missing oxfmt (canonical npm short name) should set npm_lock_unresolved and skip false unreferenced dir errors
        (mise_dir / "mise.lock").write_text(
            '[[tools.bat]]\nversion = "0.26.1"\nplatforms = { macos-arm64 = { checksum = "sha256:00" } }\n',
            encoding="utf-8",
        )
        (mise_dir / "mise.lock").chmod(0o644)
        (mise_dir / "config.container.toml").write_text(
            '[tools]\ntailscale = "1.104.7"\n', encoding="utf-8"
        )
        (mise_dir / "mise.container.lock").write_text(
            '[[tools.tailscale]]\nversion = "1.104.7"\n', encoding="utf-8"
        )
        (mise_dir / "mise.container.lock").chmod(0o644)

        monkeypatch.setattr(
            doc_mod,
            "_load_mise_registry_map",
            lambda: {
                "npm:oxfmt": [("oxfmt", ["npm:oxfmt"]), ("oxfmt-alias", ["npm:oxfmt"])],
                "aqua:sharkdp/bat": [("bat", ["aqua:sharkdp/bat"])],
            },
        )
        errs = doc_mod.check_mise_lock_integrity(root)
        assert any("mise.lock missing entry for [tools].'oxfmt'" in e for e in errs)
        assert not any("Unreferenced npm lock directory" in e for e in errs)
        assert not any("mise.container.lock" in e for e in errs)

        # Once oxfmt has valid aube.path + lockfiles in mise.lock, stale locks/extra/1.0.0 triggers unreferenced lock dir error
        (mise_dir / "config.toml").write_text(
            '[tools]\noxfmt = "0.80.0"\nbat = { version = "0.26.1" }\n'
            '[settings]\nlockfile_platforms = ["macos-arm64"]\n',
            encoding="utf-8",
        )
        (mise_dir / "mise.lock").write_text(
            '[[tools.bat]]\nversion = "0.26.1"\n'
            '[tools.bat."platforms.macos-arm64"]\nchecksum = "sha256:00"\n'
            '[[tools.oxfmt]]\nversion = "0.80.0"\nbackend = "npm:oxfmt"\n'
            '[tools.oxfmt.aube]\npath = "locks/oxfmt/0.80.0"\n',
            encoding="utf-8",
        )
        (mise_dir / "mise.lock").chmod(0o644)
        ox_lock_dir = mise_dir / "locks" / "oxfmt" / "0.80.0"
        ox_lock_dir.mkdir(parents=True)
        (ox_lock_dir / "package.json").write_text("{}\n", encoding="utf-8")
        (ox_lock_dir / "aube-lock.yaml").write_text(
            "lockfileVersion: 1\n", encoding="utf-8"
        )
        errs_resolved = doc_mod.check_mise_lock_integrity(root)
        assert any("Unreferenced npm lock directory" in e for e in errs_resolved), (
            errs_resolved
        )

        # Bracketed option short-name in config.hygiene strips [...] before ":" check
        (mise_dir / "config.toml").write_text(
            '[tools]\n"bat[exe=a:b]" = "0.26.1"\n', encoding="utf-8"
        )
        prog_dir = root / "unix" / "home" / ".config" / "herdr-app"
        prog_dir.mkdir(parents=True)
        (prog_dir / "programs.json").write_text(
            json.dumps(
                {
                    "programs": {
                        "antigravity": {"run": ["agy"]},
                        "codex": {"run": ["codex", "--no-daemon"]},
                        "opencode": {"run": ["opencode", "--standalone"]},
                    }
                }
            ),
            encoding="utf-8",
        )
        hygiene_errs = doc_mod.check_config_hygiene(root)
        assert hygiene_errs == []

        # host-hygiene catches dangling symlink and zero-byte file in ~/.cache/zsh/completions
        fake_home = root / "isolated_home"
        zcomp = fake_home / ".cache" / "zsh" / "completions"
        zcomp.mkdir(parents=True)
        (zcomp / "_dead").symlink_to(fake_home / "nonexistent-target")
        (zcomp / "_empty").write_text("", encoding="utf-8")
        (zcomp / "_valid").write_text("#compdef foo\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(doc_mod.shutil, "which", lambda _cmd: None)
        monkeypatch.setattr(
            doc_mod.subprocess,
            "check_output",
            lambda *a, **k: "",
        )
        h_errs, _ = doc_mod.check_host_hygiene(Path(__file__).resolve().parents[1])
        assert any(
            "stale-zsh-completion: dead symlink" in e and "_dead" in e for e in h_errs
        )
        assert any(
            "stale-zsh-completion: zero-byte file" in e and "_empty" in e
            for e in h_errs
        )
        assert not any("_valid" in e for e in h_errs)


def test_repo_scope_keeps_committed_file_checks(monkeypatch, tmp_path) -> None:
    """Greptile PR #32: repo scope must skip only live-host probes, not repo assertions."""
    doc_mod = _load_task_module("doctor_checks")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("DOTFILES_DOCTOR_SCOPE", "repo")
    assert doc_mod._live_host_skipped() is True
    errs, _ = doc_mod.check_mise_binary_integrity(tmp_path)
    assert any("missing unix/home/.config/dcg/config.toml" in e for e in errs)
    host_errs, msg = doc_mod.check_host_option_a(tmp_path, strict_host=True)
    assert host_errs == [] and msg.startswith("DOTFILES_DOCTOR_SCOPE=repo:")
    monkeypatch.setenv("DOTFILES_DOCTOR_SCOPE", "full")
    assert doc_mod._live_host_skipped() is False


def _reviewer_repo(tmp_path: Path, mutate=None) -> Path:
    """Copy the real reviewer-plugins manifest/lib/tasks/opencode.json into a temp repo."""
    import shutil as _sh

    src = Path(__file__).resolve().parents[1]
    rels = [
        "unix/home/.config/mise/assets/reviewer-plugins.json",
        "unix/home/.config/mise/lib/reviewer_plugins.py",
        "unix/home/.config/mise/tasks/bootstrap/reviewer-plugins",
        "unix/home/.config/mise/tasks/repowise/guard",
        "unix/home/.config/mise/tasks/repowise/init",
        "unix/home/.config/mise/tasks/host/prune-mcps",
        "unix/home/.config/opencode/opencode.json",
    ]
    for rel in rels:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        _sh.copy2(src / rel, dst)
    if mutate:
        mutate(tmp_path)
    return tmp_path


def test_reviewer_plugins_repo_scope_passes_on_real_tree(monkeypatch, tmp_path) -> None:
    doc_mod = _load_task_module("doctor_checks")
    monkeypatch.setenv("DOTFILES_DOCTOR_SCOPE", "repo")
    monkeypatch.delenv("CI", raising=False)
    errs, msg = doc_mod.check_reviewer_plugins(_reviewer_repo(tmp_path))
    assert errs == [] and msg.startswith("DOTFILES_DOCTOR_SCOPE=repo:")


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (lambda m: m["claude"]["plugins"].append("bad id"), "not name@marketplace"),
        (
            lambda m: m["codex"]["plugins"].append("x@nomkt"),
            "no declared marketplace nomkt",
        ),
        (
            lambda m: m["claude"]["connected_mcps"].append("plugin:ghost:ghost"),
            "no matching Claude plugin",
        ),
        (
            lambda m: m["claude"]["retired_mcps"].append("not-pruned"),
            "missing from host:prune-mcps",
        ),
        (
            lambda m: m["opencode"]["plugins"].append("other@1.0.0"),
            "opencode.json plugins missing",
        ),
    ],
)
def test_reviewer_plugins_repo_scope_rejects_drift(
    monkeypatch, tmp_path, edit, needle
) -> None:
    doc_mod = _load_task_module("doctor_checks")
    monkeypatch.setenv("DOTFILES_DOCTOR_SCOPE", "repo")

    def mutate(root: Path) -> None:
        mp = root / "unix/home/.config/mise/assets/reviewer-plugins.json"
        data = json.loads(mp.read_text(encoding="utf-8"))
        edit(data)
        mp.write_text(json.dumps(data), encoding="utf-8")

    errs, _ = doc_mod.check_reviewer_plugins(_reviewer_repo(tmp_path, mutate))
    assert any(needle in e for e in errs), errs


def test_reviewer_plugins_live_not_required_off_mac(monkeypatch, tmp_path) -> None:
    doc_mod = _load_task_module("doctor_checks")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("DOTFILES_DOCTOR_SCOPE", raising=False)
    monkeypatch.setattr(doc_mod.sys, "platform", "linux")
    errs, msg = doc_mod.check_reviewer_plugins(_reviewer_repo(tmp_path))
    assert errs == [] and "not the Mac host" in msg


def test_reviewer_plugins_live_reports_unconnected_mcp(monkeypatch, tmp_path) -> None:
    doc_mod = _load_task_module("doctor_checks")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("DOTFILES_DOCTOR_SCOPE", raising=False)
    monkeypatch.setattr(doc_mod.sys, "platform", "darwin")
    root = _reviewer_repo(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    lib = doc_mod._load_reviewer_plugins_lib(root)
    monkeypatch.setattr(doc_mod, "_load_reviewer_plugins_lib", lambda _r: lib)
    manifest = lib.load_manifest(
        root / "unix/home/.config/mise/assets/reviewer-plugins.json"
    )
    monkeypatch.setattr(
        lib, "_bin", lambda name, _h: name if name == "claude" else None
    )
    monkeypatch.setattr(
        lib, "claude_state", lambda _c: (set(manifest["claude"]["plugins"]), set())
    )
    statuses = {"plugin:graphify:graphify": "! Needs authentication"}
    monkeypatch.setattr(
        lib,
        "claude_mcp_status",
        lambda _c, n: (
            None
            if n in manifest["claude"]["retired_mcps"]
            else statuses.get(n, "\u2714 Connected")
        ),
    )
    errs, _ = doc_mod.check_reviewer_plugins(root)
    assert any("plugin:graphify:graphify not Connected" in e for e in errs), errs
    assert not any("Not Connected" in e or "coderabbit" in e for e in errs)


def test_is_connected_rejects_not_connected(tmp_path) -> None:
    doc_mod = _load_task_module("doctor_checks")
    lib = doc_mod._load_reviewer_plugins_lib(_reviewer_repo(tmp_path))
    assert lib.is_connected("\u2714 Connected") and lib.is_connected("Connected")
    for bad in ("\u2718 Not Connected", "! Needs authentication", "", None):
        assert not lib.is_connected(bad)


@pytest.mark.parametrize(
    ("existing", "expect_guarded_after"),
    [
        (None, True),
        ("embedder: local\n", True),
        ("distill:\n    commands:\n        enabled: false\n", True),
        ('distill:\n  commands:\n\n    enabled: "false"\n', True),
        ("distill: {commands: {enabled: false}}\n", True),
        ("distill:\r\n  commands:\r\n    permission: off\r\n", True),
        ("distill:\n  enabled: true\n", False),
    ],
)
def test_repowise_guard_fallback_without_cli(
    monkeypatch, tmp_path, existing, expect_guarded_after
) -> None:
    """Stdlib fallback (no repowise CLI) recognises valid YAML shapes; never clobbers a distill block."""
    doc_mod = _load_task_module("doctor_checks")
    lib = doc_mod._load_reviewer_plugins_lib(_reviewer_repo(tmp_path / "repo"))
    monkeypatch.setattr(lib, "repowise_python", lambda home=None: None)
    repo = tmp_path / "dev" / "org" / "proj"
    (repo / ".repowise").mkdir(parents=True)
    if existing is not None:
        (repo / ".repowise" / "config.yaml").write_bytes(existing.encode())
    assert lib.repowise_repos([tmp_path / "dev"]) == [repo]
    result = lib.apply_guard(repo)
    assert lib.guarded(repo) is expect_guarded_after, result
    if not expect_guarded_after:
        assert result.startswith("MANUAL")


def test_repowise_guard_comment_alone_is_not_a_guard(monkeypatch, tmp_path) -> None:
    doc_mod = _load_task_module("doctor_checks")
    lib = doc_mod._load_reviewer_plugins_lib(_reviewer_repo(tmp_path / "repo"))
    monkeypatch.setattr(lib, "repowise_python", lambda home=None: None)
    repo = tmp_path / "proj"
    (repo / ".repowise").mkdir(parents=True)
    (repo / ".repowise" / "config.yaml").write_text(
        f"# {lib.GUARD_LINE}\nembedder: local\n"
    )
    assert lib.guarded(repo) is False


@pytest.mark.parametrize("with_uv_lock", [True, False])
def test_mise_lock_integrity_accepts_pypi_uv_lock_dir(
    monkeypatch, tmp_path, with_uv_lock
) -> None:
    """pypi tools lock a uv graph dir instead of per-platform URLs; missing files still fail."""
    doc_mod = _load_task_module("doctor_checks")
    monkeypatch.setattr(doc_mod, "_load_mise_registry_map", lambda: {})
    mise_dir = tmp_path / "unix" / "home" / ".config" / "mise"
    lock_dir = mise_dir / "locks" / "pypi-repowise" / "0.62.0~abc"
    lock_dir.mkdir(parents=True)
    (lock_dir / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    if with_uv_lock:
        (lock_dir / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    for f in lock_dir.iterdir():
        f.chmod(0o644)
    (mise_dir / "config.toml").write_text(
        '[tools]\n"pypi:repowise" = { version = "0.62.0", python = "3.13" }\n'
        '[settings]\nlockfile_platforms = ["linux-x64", "macos-arm64"]\n',
        encoding="utf-8",
    )
    (mise_dir / "mise.lock").write_text(
        '[[tools."pypi:repowise"]]\nversion = "0.62.0"\nbackend = "pypi:repowise"\n'
        'uv = { path = "locks/pypi-repowise/0.62.0~abc", digest = "sha256:00" }\n',
        encoding="utf-8",
    )
    (mise_dir / "mise.lock").chmod(0o644)
    errs = doc_mod.check_mise_lock_integrity(tmp_path)
    assert not any("platforms." in e or "Unreferenced" in e for e in errs), errs
    assert any("uv.lock" in e for e in errs) is (not with_uv_lock), errs


def test_config_hygiene_allows_backend_for_tools_not_in_registry(
    monkeypatch, tmp_path
) -> None:
    """Short-name rule applies only when a registry entry exists; PyPI/GitHub-only tools never block."""
    doc_mod = _load_task_module("doctor_checks")
    mise_dir = tmp_path / "unix" / "home" / ".config" / "mise"
    mise_dir.mkdir(parents=True)
    (mise_dir / "config.toml").write_text(
        '[tools]\n"pipx:repowise" = "0.62.0"\n"github:example/notinregistry" = "1.0.0"\nhk = "1.0.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        doc_mod,
        "_load_mise_registry_map",
        lambda: {"aqua:jdx/hk": ("hk", ["aqua:jdx/hk"])},
    )
    errs = doc_mod.check_config_hygiene(tmp_path)
    assert not any("canonical mise registry short name" in e for e in errs), errs


def test_repowise_fallback_rejects_values_repowise_reads_as_enabled(tmp_path) -> None:
    """`enabled: disabled` is not a repowise FALSY spelling, so it must not count as guarded."""
    doc_mod = _load_task_module("doctor_checks")
    lib = doc_mod._load_reviewer_plugins_lib(_reviewer_repo(tmp_path))
    assert not lib._guarded_fallback("distill:\n  commands:\n    enabled: disabled\n")
    assert lib._guarded_fallback("distill:\n  commands:\n    permission: deny\n")


def test_reviewer_plugins_lib_parses_on_python39() -> None:
    """Hooks/bootstrap may run under /usr/bin/python3 (3.9): no PEP 758 bare-tuple excepts."""
    import ast

    src = (
        Path(__file__).resolve().parents[1]
        / "unix/home/.config/mise/lib/reviewer_plugins.py"
    ).read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 9))


def test_repowise_telemetry_reads_persistent_consent(tmp_path) -> None:
    doc_mod = _load_task_module("doctor_checks")
    lib = doc_mod._load_reviewer_plugins_lib(_reviewer_repo(tmp_path / "repo"))
    home = tmp_path / "home"
    assert lib.repowise_telemetry_enabled(home) is True
    (home / ".repowise").mkdir(parents=True)
    (home / ".repowise" / "platform.json").write_text('{"telemetry_enabled": false}')
    assert lib.repowise_telemetry_enabled(home) is False


@pytest.mark.parametrize(
    ("rc", "failed"), [(0, False), (1, True), (-9, True), (130, True)]
)
def test_reviewer_sync_failure_detection(tmp_path, rc, failed) -> None:
    """A CLI killed by a signal (negative rc) must count as a failed sync."""
    doc_mod = _load_task_module("doctor_checks")
    lib = doc_mod._load_reviewer_plugins_lib(_reviewer_repo(tmp_path))
    line = lib._result("claude plugin install x@y", rc, "boom\nreal error")
    assert lib.sync_failed([line, "ok: /tmp/rc=1-repo"]) is failed
    if failed:
        assert line.endswith("real error")
    assert lib.sync_failed(["guarded: /home/u/dev/rc=5"]) is False


def test_reviewer_sync_keeps_retired_mcp_until_replacements_installed(
    monkeypatch, tmp_path
) -> None:
    doc_mod = _load_task_module("doctor_checks")
    lib = doc_mod._load_reviewer_plugins_lib(_reviewer_repo(tmp_path / "repo"))
    calls: list[list[str]] = []
    monkeypatch.setattr(lib, "_bin", lambda n, _h: n if n == "claude" else None)
    monkeypatch.setattr(
        lib, "claude_state", lambda _c: (set(), {"graphify", "repowise"})
    )
    monkeypatch.setattr(lib, "claude_mcp_status", lambda _c, _n: "✔ Connected")
    monkeypatch.setattr(
        lib, "_run", lambda argv, timeout=180: calls.append(argv) or (1, "nope")
    )
    monkeypatch.setattr(lib, "repowise_repos", lambda _r: [])
    manifest = {
        "claude": {
            "plugins": ["graphify@graphify"],
            "retired_mcps": ["graphify-reviews"],
        }
    }
    log = lib.sync(manifest, tmp_path)
    assert not any(a[:3] == ["claude", "mcp", "remove"] for a in calls)
    assert lib.sync_failed(log)
    assert any("keep graphify-reviews" in ln for ln in log)


@pytest.mark.parametrize("pre_existing", [False, True])
def test_repowise_init_cleans_only_its_own_parent_stub(tmp_path, pre_existing) -> None:
    """When repowise redirects to a child repo, drop the parent stub only if this run created it."""
    import shutil as _sh

    root = Path(__file__).resolve().parents[1]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "repowise"
    # Fake `repowise init <args> <dir>`: index the single child repo instead of <dir>.
    fake.write_text(
        '#!/bin/sh\nfor a; do last="$a"; done\nmkdir -p "$last/app/.repowise" && touch "$last/app/.repowise/wiki.db"\n'
    )
    fake.chmod(0o755)
    work = tmp_path / "work"
    (work / "app").mkdir(parents=True)
    if pre_existing:
        (work / ".repowise").mkdir()
        (work / ".repowise" / "config.yaml").write_text("embedder: local\n")
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
    }
    task = root / "unix/home/.config/mise/tasks/repowise/init"
    subprocess.run(
        [_sh.which("bash") or "bash", str(task), str(work)],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    assert (work / "app" / ".repowise" / "config.yaml").is_file()
    if pre_existing:
        assert "embedder: local" in (work / ".repowise" / "config.yaml").read_text()
    else:
        assert not (work / ".repowise").exists()


def test_config_hygiene_rejects_global_flag_fetch_killers(
    tmp_path, monkeypatch
) -> None:
    """DO_NOT_TRACK et al. in global mise [env] drop /ship and /steward from every new Claude session."""
    doc_mod = _load_task_module("doctor_checks")
    mise_dir = tmp_path / "unix" / "home" / ".config" / "mise"
    mise_dir.mkdir(parents=True)
    (mise_dir / "config.toml").write_text(
        '[env]\nDO_NOT_TRACK = "1"\nREPOWISE_TELEMETRY_DISABLED = "1"\n'
    )
    monkeypatch.setattr(doc_mod, "_load_mise_registry_map", lambda: {})
    errors = doc_mod.check_config_hygiene(tmp_path)
    assert any("DO_NOT_TRACK" in e and "/ship" in e for e in errors), errors
    assert not any("REPOWISE_TELEMETRY_DISABLED" in e for e in errors), errors
    frag = mise_dir / "conf.d" / "x" / "mise.toml"
    frag.parent.mkdir(parents=True)
    frag.write_text('[env]\nDISABLE_TELEMETRY = "1"\n')
    (tmp_path / "mise.toml").write_text('[env]\nCLAUDE_CODE_DISABLE_SHIP = "1"\n')
    errors = doc_mod.check_config_hygiene(tmp_path)
    assert any(
        "conf.d/x/mise.toml" in e and "DISABLE_TELEMETRY" in e for e in errors
    ), errors
    assert any(
        e.startswith("config-hygiene: mise.toml") and "CLAUDE_CODE_DISABLE_SHIP" in e
        for e in errors
    ), errors


def test_config_hygiene_flag_fetch_edge_cases(tmp_path, monkeypatch) -> None:
    """`= false` unsets (allowed); FEATURE_FLAGS, Dockerfile ENV and unparsable fragments are reported."""
    doc_mod = _load_task_module("doctor_checks")
    mise_dir = tmp_path / "unix" / "home" / ".config" / "mise"
    mise_dir.mkdir(parents=True)
    (mise_dir / "config.toml").write_text(
        '[env]\nDO_NOT_TRACK = false\nCLAUDE_CODE_DISABLE_FEATURE_FLAGS = "1"\n'
    )
    bad = mise_dir / "conf.d" / "bad" / "mise.toml"
    bad.parent.mkdir(parents=True)
    bad.write_text("[env\n")
    dockerfile = tmp_path / "docker" / "ubuntu" / "Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        '# DO_NOT_TRACK=1 is fine in a comment\nRUN DO_NOT_TRACK=1 true\nENV container=docker \\\n    DISABLE_TELEMETRY=1\nENV DISABLE_GROWTHBOOK 1\nENV\tCLAUDE_CODE_DISABLE_SHIP=1\nENV EXAMPLE="env DO_NOT_TRACK=1 api_runner"\n'
    )
    monkeypatch.setattr(
        doc_mod,
        "_load_mise_registry_map",
        lambda: (_ for _ in ()).throw(OSError("no mise")),
    )
    errors = doc_mod.check_config_hygiene(tmp_path)
    assert not any("DO_NOT_TRACK" in e for e in errors), errors
    assert any("CLAUDE_CODE_DISABLE_FEATURE_FLAGS" in e for e in errors), errors
    assert any("conf.d/bad/mise.toml" in e and "cannot parse" in e for e in errors), (
        errors
    )
    assert any(
        "docker/ubuntu/Dockerfile" in e and "DISABLE_TELEMETRY" in e for e in errors
    ), errors
    assert any("failed to load `mise registry`" in e for e in errors), errors
    assert any(
        "docker/ubuntu/Dockerfile" in e and "DISABLE_GROWTHBOOK" in e for e in errors
    ), errors
    assert not any("Dockerfile" in e and "DO_NOT_TRACK" in e for e in errors), errors
    assert any("Dockerfile" in e and "CLAUDE_CODE_DISABLE_SHIP" in e for e in errors), (
        errors
    )


def test_config_hygiene_flag_fetch_shell_and_extra_configs(tmp_path) -> None:
    """Managed shell exports, dotted/flat mise configs and `ENV<TAB>` are caught; command prefixes are not."""
    doc_mod = _load_task_module("doctor_checks")
    home = tmp_path / "unix" / "home"
    mise_dir = home / ".config" / "mise"
    (mise_dir / "conf.d").mkdir(parents=True)
    (mise_dir / "conf.d" / "flat.toml").write_text('[env]\nDO_NOT_TRACK = "1"\n')
    (tmp_path / ".mise.toml").write_text('[env]\nDISABLE_TELEMETRY = "1"\n')
    (home / ".zshenv").write_text(
        "export DO_NOT_TRACK=1\nDISABLE_GROWTHBOOK=1 some-cmd\n"
    )
    zrc = home / ".config" / "zsh" / ".zshrc.d" / "50-x.zsh"
    zrc.parent.mkdir(parents=True)
    zrc.write_text("# export CLAUDE_CODE_DISABLE_SHIP=1\nCLAUDE_CODE_DISABLE_SHIP=1\n")
    dockerfile = tmp_path / "docker" / "x" / "Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("ENV\tDO_NOT_TRACK=1\n")
    errors = doc_mod._check_flag_fetch_env(tmp_path, mise_dir)
    assert any("conf.d/flat.toml" in e and "DO_NOT_TRACK" in e for e in errors), errors
    assert any(".mise.toml" in e and "DISABLE_TELEMETRY" in e for e in errors), errors
    assert any(".zshenv exports DO_NOT_TRACK" in e for e in errors), errors
    assert not any("DISABLE_GROWTHBOOK" in e for e in errors), errors
    assert any("50-x.zsh exports CLAUDE_CODE_DISABLE_SHIP" in e for e in errors), errors
    assert any("docker/x/Dockerfile" in e and "DO_NOT_TRACK" in e for e in errors), (
        errors
    )


def test_real_global_env_keeps_ship_available() -> None:
    root = Path(__file__).resolve().parents[1]
    doc_mod = _load_task_module("doctor_checks")
    mise_dir = root / "unix" / "home" / ".config" / "mise"
    assert doc_mod._check_flag_fetch_env(root, mise_dir) == []


def test_config_hygiene_rejects_auto_venv_on_path(tmp_path, monkeypatch) -> None:
    """uv_venv_auto / [env] _.python.venv put .venv/bin on PATH, which blocks the /ship watch."""
    doc_mod = _load_task_module("doctor_checks")
    mise_dir = tmp_path / "unix" / "home" / ".config" / "mise"
    mise_dir.mkdir(parents=True)
    (mise_dir / "config.toml").write_text('[env]\n_.python.venv = { path = ".venv" }\n')
    (tmp_path / "mise.toml").write_text('[settings]\npython.uv_venv_auto = "source"\n')
    monkeypatch.setattr(doc_mod, "_load_mise_registry_map", lambda: {})
    errors = doc_mod.check_config_hygiene(tmp_path)
    assert any(
        e.startswith("config-hygiene: mise.toml") and "uv_venv_auto" in e
        for e in errors
    ), errors
    assert any("config.toml [env] _.python.venv" in e for e in errors), errors
    (tmp_path / "mise.toml").write_text("[settings]\npython.uv_venv_auto = false\n")
    (mise_dir / "config.toml").write_text("[env]\nX = '1'\n")
    assert not any("venv" in e for e in doc_mod.check_config_hygiene(tmp_path))


def test_config_hygiene_venv_guard_tolerates_odd_tables(tmp_path, monkeypatch) -> None:
    doc_mod = _load_task_module("doctor_checks")
    mise_dir = tmp_path / "unix" / "home" / ".config" / "mise"
    mise_dir.mkdir(parents=True)
    (mise_dir / "config.toml").write_text(
        'settings = "x"\n[env]\nMISE_PYTHON_UV_VENV_AUTO = "source"\n'
    )
    monkeypatch.setattr(doc_mod, "_load_mise_registry_map", lambda: {})
    errors = doc_mod.check_config_hygiene(tmp_path)
    assert any("uv_venv_auto = 'source'" in e for e in errors), errors


def test_no_repo_skills_allows_claude_runtime_state(tmp_path) -> None:
    """/ship watch timers create .claude/scheduled_tasks.lock; only skill dirs may fail the check."""
    import tomllib

    root = Path(__file__).resolve().parents[1]
    cmd = tomllib.loads((root / "mise.toml").read_text(encoding="utf-8"))["doctor"][
        "checks"
    ]["no-repo-skills"]["run"]

    def run() -> int:
        return subprocess.run(["bash", "-c", cmd], cwd=tmp_path, check=False).returncode

    assert run() == 0
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "scheduled_tasks.lock").write_text("")
    assert run() == 0
    (tmp_path / ".claude" / "skills").mkdir()
    assert run() != 0
    (tmp_path / ".claude" / "skills").rmdir()
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    assert run() != 0


def test_claude_ship_env_flags_managed_settings(tmp_path, monkeypatch) -> None:
    """Managed Claude settings must not disable feature-flag fetching or the ship skills."""
    doc_mod = _load_task_module("doctor_checks")
    monkeypatch.setenv("DOTFILES_DOCTOR_SCOPE", "repo")
    assets = tmp_path / "unix/home/.config/mise/assets/claude"
    assets.mkdir(parents=True)
    (assets / "env-common.json").write_text('{"env": {"CLAUDE_CODE_BRIEF": "1"}}')
    errors, msg = doc_mod.check_claude_ship_env(tmp_path)
    assert errors == [] and "not probed" in msg
    (assets / "bad.json").write_text(
        '{"env": {"DISABLE_TELEMETRY": "1"}, "skillOverrides": {"steward": "off"}, "disableBundledSkills": true}'
    )
    (assets / "env-mac.json.tera").write_text(
        '{"env": {"DO_NOT_TRACK": "{{ x }}"}, "skillOverrides": {"ship-local": "off"}, "disableBundledSkills": true}'
    )
    errors, _ = doc_mod.check_claude_ship_env(tmp_path)
    joined = "\n".join(errors)
    for needle in (
        "DISABLE_TELEMETRY",
        "turns off steward",
        "disableBundledSkills",
        "env-mac.json.tera sets DO_NOT_TRACK",
        "env-mac.json.tera skillOverrides turns off ship-local",
        "env-mac.json.tera disableBundledSkills",
    ):
        assert needle in joined, errors


def test_real_managed_claude_settings_keep_ship(monkeypatch) -> None:
    doc_mod = _load_task_module("doctor_checks")
    monkeypatch.setenv("DOTFILES_DOCTOR_SCOPE", "repo")
    errors, _ = doc_mod.check_claude_ship_env(Path(__file__).resolve().parents[1])
    assert errors == []


def _pr_ship_sandbox(tmp_path, checks: str) -> tuple[list[str], dict[str, str]]:
    """Temp wt/* repo with a bare origin and a fake gh that serves canned API answers."""
    root = Path(__file__).resolve().parents[1]
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "wt/demo", str(work)], check=True)
    git = [
        "git",
        "-C",
        str(work),
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
    ]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    subprocess.run([*git, "remote", "add", "origin", str(origin)], check=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "checks.tsv").write_text(checks)
    fake = bindir / "gh"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  "api repos/ray-manaloto/dotfiles-v2 --jq .allow_auto_merge") echo false ;;\n'
        '  "pr list"*) echo 7 ;;\n'
        "  *autoMergeRequest*) echo false ;;\n"
        '  *rules/branches/main*) [ -n "$FAKE_GH_FAIL_RULES" ] && exit 1; printf "Lint, Gates, and Tests\\nBootstrap Convergence\\n" ;;\n'
        f'  *check-runs*) cat "{tmp_path}/checks.tsv" ;;\n'
        "  *headRefOid*) git rev-parse HEAD ;;\n"
        '  *"/comments"*) : ;;\n'
        '  "event listen"*) exit 8 ;;\n'
        '  *) echo unexpected gh "$*" >&2; exit 1 ;;\n'
        "esac\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "usage_skip_local_gates": "1",
        "usage_max_waits": "2",
        # The sandbox push must not run this host's global config-based git hooks (hk, gh-doctor).
        "HK": "0",
    }
    return ["/bin/bash", str(root / "tasks/pr/ship")], {
        **env,
        "PWD": str(work),
        "_WORK": str(work),
    }


@pytest.mark.parametrize(
    ("checks", "code", "needle"),
    [
        (
            "Lint, Gates, and Tests\tcompleted\tsuccess\nBootstrap Convergence\tcompleted\tsuccess\n",
            0,
            "READY #7",
        ),
        (
            "Lint, Gates, and Tests\tcompleted\tfailure\nBootstrap Convergence\tcompleted\tsuccess\n",
            1,
            "RED",
        ),
        # A check whose name only ends in a required name must not count (exact match).
        (
            "Not Bootstrap Convergence\tcompleted\tsuccess\nLint, Gates, and Tests\tcompleted\tsuccess\n",
            2,
            "TIMEOUT",
        ),
    ],
)
def test_pr_ship_verdicts(tmp_path, checks, code, needle) -> None:
    cmd, env = _pr_ship_sandbox(tmp_path, checks)
    proc = subprocess.run(
        cmd,
        cwd=env["_WORK"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == code, proc.stdout + proc.stderr
    assert needle in proc.stdout + proc.stderr
    if code == 0:
        assert "--merge --match-head-commit" in proc.stdout


def test_pr_ship_api_failure_exits_4(tmp_path) -> None:
    """A GitHub API outage must not look like a red check, a refusal, or a moved head."""
    cmd, env = _pr_ship_sandbox(tmp_path, "")
    env |= {"FAKE_GH_FAIL_RULES": "1", "PR_SHIP_RETRY_SLEEP": "0"}
    proc = subprocess.run(
        cmd,
        cwd=env["_WORK"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "GitHub API unavailable" in proc.stderr


def test_pr_ship_final_read_after_last_wait(tmp_path) -> None:
    """A result that lands during the last wait is reported, not turned into a timeout."""
    cmd, env = _pr_ship_sandbox(tmp_path, "")
    checks = tmp_path / "checks.tsv"
    # The fake gh's `event listen` writes the final state, simulating CI finishing during the wait.
    fake = tmp_path / "bin" / "gh"
    fake.write_text(
        fake.read_text().replace(
            '"event listen"*) exit 8 ;;',
            f'"event listen"*) printf "Lint, Gates, and Tests\\tcompleted\\tfailure\\nBootstrap Convergence\\tcompleted\\tsuccess\\n" > "{checks}"; exit 8 ;;',
        )
    )
    env["usage_max_waits"] = "1"
    proc = subprocess.run(
        cmd,
        cwd=env["_WORK"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "RED" in proc.stderr
