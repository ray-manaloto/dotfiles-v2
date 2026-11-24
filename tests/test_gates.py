"""Unit tests for schema-first AST, schema contract, and doctor prevention gates."""

from __future__ import annotations

import json
import os
import subprocess
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
