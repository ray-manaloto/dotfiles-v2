"""agy-history-fallback skill script: search agy conversations and brain artifacts on disk (#90)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "unix/home/.agents/skills/agy-history-fallback"
SCRIPT = SKILL / "scripts/agy-history.py"
CONV = "0f3c9a51-7d2e-4b8a-9c1e-5a6b7c8d9e0f"


def _run(*args: str, root: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-I", "-B", str(SCRIPT)]
    if root is not None:
        cmd += ["--root", str(root)]
    return subprocess.run([*cmd, *args], capture_output=True, text=True, check=False)


@pytest.fixture
def agy_home(tmp_path: Path) -> Path:
    """Fixture agy home written by the script's own build_fixture()."""
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import importlib.util, sys; from pathlib import Path;"
            "spec = importlib.util.spec_from_file_location('agy_history', sys.argv[1]);"
            "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod);"
            "mod.build_fixture(Path(sys.argv[2]))",
            str(SCRIPT),
            str(tmp_path / "agy"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return tmp_path / "agy"


def test_self_check_passes() -> None:
    proc = _run("--self-check")
    assert proc.returncode == 0, proc.stderr
    assert "self-check ok" in proc.stdout


def test_search_finds_agy_only_term_with_deduped_snippets(agy_home: Path) -> None:
    proc = _run("--json", "search", "infisical", root=agy_home)
    assert proc.returncode == 0, proc.stderr
    (row,) = json.loads(proc.stdout)
    assert row["conversation"] == CONV
    assert row["hits"] == 3  # the text repeats in the binary file...
    assert len(row["snippets"]) == 1  # ...but is reported once
    assert "Doppler over Infisical" in row["snippets"][0]
    assert row["brain"] == str(agy_home / "brain" / CONV)


def test_search_miss_exits_nonzero(agy_home: Path) -> None:
    proc = _run("search", "no-such-term-anywhere", root=agy_home)
    assert proc.returncode == 1
    assert "no matches" in proc.stderr


def test_search_reads_utf8_and_root_env(agy_home: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "search", "CAFÉ"],
        capture_output=True,
        text=True,
        check=False,
        env={"AGY_HISTORY_ROOT": str(agy_home), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "café notes" in proc.stdout


def test_brain_lists_by_updated_at_and_filters(agy_home: Path) -> None:
    proc = _run("--json", "brain", "--limit", "0", root=agy_home)
    assert proc.returncode == 0, proc.stderr
    rows = [r for r in json.loads(proc.stdout) if r["artifact"] != "task"]
    assert [(r["artifact"], r["updated_at"]) for r in rows] == [
        ("walkthrough", "2026-11-26T09:30:00Z"),
        ("implementation_plan", "2026-11-25T10:00:00Z"),
        ("learning_proposal", "2026-11-24T06:00:00Z"),
    ]
    assert rows[0]["summary"] == "Walkthrough of the keyring change"

    filtered = _run("brain", "--term", "d155", root=agy_home)
    assert filtered.returncode == 0, filtered.stderr
    assert filtered.stdout.count("\n") == 1
    assert "learning_proposal  Learn: record D155" in filtered.stdout


def test_read_prints_plan_walkthrough_and_learning_proposal(agy_home: Path) -> None:
    proc = _run("read", CONV[:8], root=agy_home)
    assert proc.returncode == 0, proc.stderr
    heads = [line for line in proc.stdout.splitlines() if line.startswith("==> ")]
    assert [Path(h[4:-4]).name for h in heads] == [
        "implementation_plan.md",
        "walkthrough.md",
        "learning_proposal.md",
    ]

    one = _run("--json", "read", CONV, "--kind", "task", root=agy_home)
    assert [r["artifact"] for r in json.loads(one.stdout)] == ["task"]

    missing = _run("read", "ffff", root=agy_home)
    assert missing.returncode == 1
    assert "expected one match" in missing.stderr

    escape = _run("read", "../conversations", root=agy_home)
    assert escape.returncode == 1
    assert "not a conversation uuid" in escape.stderr


def test_missing_root_is_a_clean_miss(tmp_path: Path) -> None:
    for args in (("search", "x"), ("brain",)):
        proc = _run(*args, root=tmp_path / "absent")
        assert proc.returncode == 1
        assert "Traceback" not in proc.stderr


def test_skill_documents_fallback_and_is_registered() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: agy-history-fallback\n")
    for needle in (
        "rg -a -l",
        "strings",
        "agentsview-finding-history",
        "implementation_plan",
        "learning_proposal",
        "scripts/agy-history.py",
    ):
        assert needle in text
    config = (ROOT / "unix/home/.config/mise/config.toml").read_text(encoding="utf-8")
    assert (
        '"~/.agents/skills/agy-history-fallback" = { source = "agy-history-fallback"'
        in config
    )
