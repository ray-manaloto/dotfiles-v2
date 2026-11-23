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

        gen_file = root / "pkg" / "generated" / "models.py"
        gen_file.parent.mkdir(parents=True)
        gen_file.write_text(
            "from pydantic import BaseModel\nclass M(BaseModel):\n    x: int\n",
            encoding="utf-8",
        )
        assert ast_mod.check_file(gen_file, root) == []


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
