"""metadata-gate-lint: no security decision keyed on forgeable commit/PR metadata (#99)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINT = ROOT / "tasks/py/metadata_gate_lint.py"


def run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-B", str(LINT), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / ".github" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_flags_author_trailer_and_bot_name_gates(tmp_path: Path) -> None:
    bad = write(
        tmp_path,
        "openai-review/review.py",
        "skip = commit.author.name == 'dependabot'\n"
        "trusted = 'Co-Authored-By: bot' in message\n"
        "if login.endswith('[bot]'):\n"
        "    skip = True\n"
        "if comment['user']['type'] == 'Bot':\n"
        "    pass\n",
    )
    proc = run(bad)
    assert proc.returncode == 1
    assert proc.stderr.count("forgeable commit/PR metadata") == 4


def test_flags_workflow_expressions(tmp_path: Path) -> None:
    bad = write(
        tmp_path,
        "workflows/x.yml",
        "    if: github.event.head_commit.author.name != 'ci'\n",
    )
    assert run(bad).returncode == 1


def test_allow_marker_needs_a_reason(tmp_path: Path) -> None:
    ok = write(
        tmp_path,
        "openai-review/ok.py",
        "# metadata-gate: ok display only, never gates anything\n"
        "label = commit.author.name\n",
    )
    assert run(ok).returncode == 0
    no_reason = write(
        tmp_path,
        "openai-review/no_reason.py",
        "# metadata-gate: ok\nlabel = commit.author.name\n",
    )
    assert run(no_reason).returncode == 1


def test_authenticated_identity_and_test_files_pass(tmp_path: Path) -> None:
    good = write(
        tmp_path,
        "workflows/good.yml",
        "      LABELER: ${{ github.event.sender.login }}\n"
        '      select(.user.login == "github-actions[bot]")\n'
        "      -c user.name='github-actions[bot]'\n",
    )
    assert run(good).returncode == 0
    fixture = write(
        tmp_path, "openai-review/test_x.py", "x = commit.author.name == 'a'\n"
    )
    assert run(fixture).returncode == 0  # test fixtures are out of scope


def test_repo_workflows_and_review_code_are_clean() -> None:
    files = [
        *sorted((ROOT / ".github/openai-review").glob("*.py")),
        *sorted((ROOT / ".github/workflows").glob("*.yml")),
    ]
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(LINT), *map(str, files)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
