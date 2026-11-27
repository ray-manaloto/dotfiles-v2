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
CONV = "6d1e2f30-aaaa-4bbb-8ccc-0123456789ab"
OTHER = "9e8d7c6b-1111-4222-8333-fedcba987654"
# The script's contract: macOS /usr/bin/python3 (3.9.6). py39-compat only checks syntax.
PY39 = Path("/usr/bin/python3")


def _run(*args: str, root: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-I", "-B", str(SCRIPT)]
    if root is not None:
        cmd += ["--root", str(root)]
    return subprocess.run(
        [*cmd, *args], capture_output=True, text=True, check=False, timeout=30
    )


def _meta(summary: str, updated: str) -> str:
    return json.dumps(
        {"artifactType": "ARTIFACT", "summary": summary, "updatedAt": updated}
    )


@pytest.fixture
def agy_home(tmp_path: Path) -> Path:
    """Independent agy home (not the script's build_fixture), shaped like a live one."""
    root = tmp_path / "agy"
    conv = root / "conversations"
    conv.mkdir(parents=True)
    text = b"we chose Doppler over Infisical; D155 keeps the OS keyring"
    # Binary SQLite-ish page: the message text repeats in several cells.
    page = b"SQLite format 3\x00\x10\x00" + (b"\x00\x81\x07" + text + b"\x00") * 3
    (conv / f"{CONV}.db").write_bytes(page + b"\xfe\x01caf\xc3\xa9 notes\x00")
    # WAL sidecar of the same conversation: one more copy, merged into the .db row.
    (conv / f"{CONV}.db-wal").write_bytes(b"\x37\x7f\x06\x82\x00" + text + b"\x00")
    (conv / f"{CONV}.db-shm").write_bytes(b"\x00" * 64)
    (conv / f"{OTHER}.db").write_bytes(b"\x00\x02phase Z wave3 notes\x00")

    brain = root / "brain" / CONV
    (brain / ".system_generated" / "logs").mkdir(parents=True)
    (brain / ".system_generated" / "logs" / "overview.txt").write_text(
        "Infisical log noise\n", encoding="utf-8"
    )
    artifacts = {
        "implementation_plan": (
            "Plan: Doppler pc config",
            "2026-11-25T10:00:00.123456789Z",
        ),
        "walkthrough": ("Walkthrough of the keyring change", "2026-11-26T09:30:00Z"),
        "learning_proposal": ("Learn: record D155", "2026-11-24T08:00:00+0200"),
    }
    for kind, (summary, updated) in artifacts.items():
        (brain / f"{kind}.md").write_text(
            f"# {summary}\n\nDoppler body\n", encoding="utf-8"
        )
        (brain / f"{kind}.md.metadata.json").write_text(
            _meta(summary, updated), encoding="utf-8"
        )
    (brain / "task.md").write_text("# task without metadata\n", encoding="utf-8")
    (brain / "scratch.md").mkdir()  # a directory matching *.md
    (brain / "gone.md").symlink_to(brain / "missing-target.md")  # dangling symlink
    return root


def test_self_check_passes() -> None:
    proc = _run("--self-check")
    assert proc.returncode == 0, proc.stderr
    assert "self-check ok" in proc.stdout


@pytest.mark.skipif(not PY39.is_file(), reason="no /usr/bin/python3 on this host")
def test_runs_under_system_python(agy_home: Path) -> None:
    for args in (
        ("--self-check",),
        ("search", "infisical", "--json", "--root", str(agy_home)),
        ("brain", "--limit", "0", "--json", "--root", str(agy_home)),
    ):
        proc = subprocess.run(
            [str(PY39), "-I", "-B", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert proc.returncode == 0, (args, proc.stderr)
    rows = json.loads(proc.stdout)
    assert ("learning_proposal", "2026-11-24T06:00:00Z") in [
        (r["artifact"], r["updated_at"]) for r in rows
    ]


def test_search_finds_agy_only_term_with_deduped_snippets(agy_home: Path) -> None:
    proc = _run("--json", "search", "infisical", root=agy_home)
    assert proc.returncode == 0, proc.stderr
    (row,) = json.loads(proc.stdout)  # .db, .db-wal (and .db-shm) give one row
    assert row["conversation"] == CONV
    assert row["hits"] == 4  # 3 copies in the .db plus 1 in the WAL...
    assert len(row["snippets"]) == 1  # ...reported once
    assert "Doppler over Infisical" in row["snippets"][0]
    assert [Path(p).name for p in row["paths"]] == [f"{CONV}.db", f"{CONV}.db-wal"]
    assert row["path"] == str(agy_home / "conversations" / f"{CONV}.db")
    assert row["brain"] == str(agy_home / "brain" / CONV)


def test_flags_work_after_the_subcommand(agy_home: Path) -> None:
    after = _run("search", "D155", "--json", "--root", str(agy_home))
    assert after.returncode == 0, after.stderr
    assert [r["conversation"] for r in json.loads(after.stdout)] == [CONV]

    before = _run("--json", "--root", str(agy_home), "brain", "--term", "d155")
    assert before.returncode == 0, before.stderr
    assert [r["artifact"] for r in json.loads(before.stdout)] == ["learning_proposal"]

    read = _run("read", CONV[:8], "--kind", "task", "--json", root=agy_home)
    assert [r["artifact"] for r in json.loads(read.stdout)] == ["task"]


@pytest.mark.parametrize("term", ["", "   "])
def test_search_rejects_empty_term(agy_home: Path, term: str) -> None:
    proc = _run("search", term, root=agy_home)  # timeout=30 catches a hang
    assert proc.returncode == 2
    assert "term must not be empty" in proc.stderr


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
    assert "Traceback" not in proc.stderr
    all_rows = json.loads(proc.stdout)
    # scratch.md (a directory), gone.md (dangling) and .system_generated/ are skipped
    assert sorted(r["artifact"] for r in all_rows) == [
        "implementation_plan",
        "learning_proposal",
        "task",
        "walkthrough",
    ]
    rows = [r for r in all_rows if r["artifact"] != "task"]
    assert [(r["artifact"], r["updated_at"]) for r in rows] == [
        ("walkthrough", "2026-11-26T09:30:00Z"),
        ("implementation_plan", "2026-11-25T10:00:00Z"),
        ("learning_proposal", "2026-11-24T06:00:00Z"),
    ]
    assert rows[0]["summary"] == "Walkthrough of the keyring change"

    filtered = _run("brain", "--term", "d155", root=agy_home)
    assert filtered.returncode == 0, filtered.stderr
    assert "Traceback" not in filtered.stderr
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
