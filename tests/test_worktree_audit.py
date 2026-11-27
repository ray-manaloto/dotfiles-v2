"""worktree:audit classification, --prune-merged, and the warn-only worktree-stale doctor check."""

from __future__ import annotations

import os
import subprocess
import time
import tomllib
from pathlib import Path

import pytest
from tasks_py_loader import load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
DAY = 86400


@pytest.fixture
def git_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    env = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.com",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.com",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    return dict(os.environ)


def _git(cwd: Path, *args: str, days_ago: int = 0) -> str:
    env = dict(os.environ)
    if days_ago:
        stamp = f"@{int(time.time()) - days_ago * DAY} +0000"
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(cwd: Path, name: str, days_ago: int = 0) -> None:
    (cwd / name).write_text(f"{name}\n", encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", f"add {name}", days_ago=days_ago)


def _branch_with(main: Path, branch: str, *files: str) -> None:
    _git(main, "checkout", "-q", "-b", branch)
    for name in files:
        _commit(main, name)
    _git(main, "checkout", "-q", "main")


@pytest.fixture
def fixture_repo(tmp_path: Path, git_env: dict[str, str]) -> dict[str, Path]:
    origin = tmp_path / "origin.git"
    main = tmp_path / "repo"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-q", "-b", "main", str(main))
    _git(main, "remote", "add", "origin", str(origin))
    (main / ".gitignore").write_text(".planning/\n", encoding="utf-8")
    _commit(main, "README")
    _git(main, "add", ".gitignore")
    _git(main, "commit", "-q", "-m", "ignore plans")
    _git(main, "push", "-q", "-u", "origin", "main")
    _git(main, "remote", "set-head", "origin", "main")

    wt = {
        name: tmp_path / f"repo-{name}"
        for name in ("merged", "dirty", "fresh", "stale")
    }
    # Merged with a merge commit: one clean worktree (prunable), one dirty, one branch only.
    _git(main, "worktree", "add", "-q", "-b", "wt/merged", str(wt["merged"]))
    _commit(wt["merged"], "merged.txt")
    (wt["merged"] / ".planning" / "plan-merged").mkdir(parents=True)
    (wt["merged"] / ".planning" / "plan-merged" / "task_plan.md").write_text(
        "# plan\n", encoding="utf-8"
    )
    _git(main, "worktree", "add", "-q", "-b", "wt/dirty", str(wt["dirty"]))
    _commit(wt["dirty"], "dirty.txt")
    _branch_with(main, "merged-no-wt", "nowt.txt")
    for branch in ("wt/merged", "wt/dirty", "merged-no-wt"):
        _git(main, "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch)
    (wt["dirty"] / "scratch.txt").write_text("unsaved\n", encoding="utf-8")
    # A worktree created from main that never got a commit.
    _git(main, "worktree", "add", "-q", "-b", "wt/fresh", str(wt["fresh"]))
    # Squash-merged (2 commits -> 1) and cherry-picked work.
    _branch_with(main, "squashed", "sq1.txt", "sq2.txt")
    _git(main, "merge", "-q", "--squash", "squashed")
    _git(main, "commit", "-q", "-m", "squash squashed")
    _branch_with(main, "picked", "picked.txt")
    # -x changes the message, so the picked commit gets a new hash even within the same second.
    _git(main, "cherry-pick", "-x", "picked")
    _git(main, "push", "-q", "origin", "main")
    # Pushed, then the remote branch was deleted without the work landing on main.
    _branch_with(main, "gone", "gone.txt")
    _git(main, "push", "-q", "-u", "origin", "gone")
    _git(main, "push", "-q", "origin", "--delete", "gone")
    # Pushed and in sync (open PR), and a local branch reset behind its upstream.
    _branch_with(main, "pushed", "pushed.txt")
    _git(main, "push", "-q", "-u", "origin", "pushed")
    _branch_with(main, "behind", "behind1.txt", "behind2.txt")
    _git(main, "push", "-q", "-u", "origin", "behind")
    _git(main, "branch", "-q", "-f", "behind", "behind~1")
    # Never pushed: one old (stale) in a worktree, one recent.
    _git(main, "worktree", "add", "-q", "-b", "wt/stale", str(wt["stale"]))
    _commit(wt["stale"], "stale.txt", days_ago=10)
    _branch_with(main, "recent-local", "recent.txt")
    return {"main": main, "origin": origin, **wt}


EXPECTED = {
    "main": "base",
    "wt/merged": "merged",
    "wt/dirty": "merged",
    "merged-no-wt": "merged",
    "wt/fresh": "no-commits",
    "squashed": "squash-merged",
    "picked": "squash-merged",
    "gone": "upstream-gone",
    "pushed": "pushed",
    "behind": "behind",
    "wt/stale": "local-only",
    "recent-local": "local-only",
}


def test_audit_classifies_every_branch_and_worktree(
    fixture_repo: dict[str, Path],
) -> None:
    mod = load_module("worktree_audit")
    entries = mod.audit(fixture_repo["main"])
    by_branch = {e["branch"]: e for e in entries}
    assert {b: e["status"] for b, e in by_branch.items()} == EXPECTED
    assert by_branch["squashed"]["detail"].startswith("squash of 2 commit(s)")
    assert "cherry-picked" in by_branch["picked"]["detail"]
    assert by_branch["wt/stale"]["local_only"] == 1
    assert by_branch["wt/stale"]["age_days"] > 9
    assert (
        Path(by_branch["wt/merged"]["worktree"]).resolve()
        == fixture_repo["merged"].resolve()
    )
    assert by_branch["merged-no-wt"]["worktree"] is None
    assert by_branch["wt/dirty"]["dirty"] is True
    assert by_branch["wt/merged"]["dirty"] is False


def test_detached_worktree_gets_its_own_entry(
    fixture_repo: dict[str, Path], tmp_path: Path
) -> None:
    mod = load_module("worktree_audit")
    detached = tmp_path / "repo-detached"
    _git(
        fixture_repo["main"], "worktree", "add", "-q", "--detach", str(detached), "gone"
    )
    entries = [e for e in mod.audit(fixture_repo["main"]) if e["branch"] is None]
    assert len(entries) == 1
    assert entries[0]["status"] == "local-only"
    assert Path(entries[0]["worktree"]).resolve() == detached.resolve()


def test_default_run_deletes_nothing(
    fixture_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load_module("worktree_audit")
    before = _git(fixture_repo["main"], "for-each-ref", "refs/heads")
    trees = _git(fixture_repo["main"], "worktree", "list", "--porcelain")
    assert mod.main(["--repo", str(fixture_repo["main"])]) == 0
    out = capsys.readouterr().out
    assert "nothing deleted" in out
    for branch, status in EXPECTED.items():
        assert any(
            line.startswith(status) and f" {branch} " in line
            for line in out.splitlines()
        ), (branch, out)
    assert _git(fixture_repo["main"], "for-each-ref", "refs/heads") == before
    assert _git(fixture_repo["main"], "worktree", "list", "--porcelain") == trees


def test_prune_merged_removes_only_clean_merged_entries(
    fixture_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load_module("worktree_audit")
    main = fixture_repo["main"]
    assert mod.main(["--repo", str(main), "--prune-merged"]) == 0
    out = capsys.readouterr().out
    branches = set(
        _git(
            main, "for-each-ref", "--format=%(refname:short)", "refs/heads"
        ).splitlines()
    )
    assert branches == set(EXPECTED) - {"wt/merged", "merged-no-wt"}
    assert not fixture_repo["merged"].exists()
    assert fixture_repo["dirty"].is_dir() and fixture_repo["fresh"].is_dir()
    assert "wt/dirty: worktree has uncommitted or untracked files" in out
    # Git-ignored plan files survive the worktree removal, as with pr:land.
    assert (main / ".planning" / "plan-merged" / "task_plan.md").is_file()


def test_prune_never_removes_the_current_worktree(
    fixture_repo: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = load_module("worktree_audit")
    monkeypatch.chdir(fixture_repo["merged"])
    assert mod.main(["--repo", str(fixture_repo["main"]), "--prune-merged"]) == 0
    assert fixture_repo["merged"].is_dir()
    assert "wt/merged" in _git(fixture_repo["main"], "branch", "--list", "wt/merged")


def test_doctor_warns_on_stale_local_only_and_never_fails(
    fixture_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load_module("worktree_audit")
    assert (
        mod.main(["--repo", str(fixture_repo["main"]), "--doctor", "--stale-days", "3"])
        == 0
    )
    out = capsys.readouterr().out
    warnings = [line for line in out.splitlines() if line.startswith("WARNING")]
    assert len(warnings) == 1, out
    assert "wt/stale" in warnings[0] and "1 commit(s) on no remote" in warnings[0]
    assert "recent-local" not in out
    assert "1 local-only branch(es) older than 3 days" in out


def test_doctor_outside_a_repo_is_a_skip(
    git_env: dict[str, str], tmp_path: Path
) -> None:
    script = REPO_ROOT / "tasks" / "py" / "worktree_audit.py"
    result = subprocess.run(
        ["python3", "-I", "-B", str(script), "--doctor", "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        env={**git_env, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)},
    )
    assert result.returncode == 0, result.stderr
    assert "worktree-stale: skipped" in result.stdout


def test_task_and_warn_only_doctor_check_are_registered() -> None:
    cfg = tomllib.loads((REPO_ROOT / "mise.toml").read_text(encoding="utf-8"))
    task = cfg["tasks"]["worktree:audit"]
    for flag in ("--repo <path>", "--base <ref>", "--prune-merged"):
        assert flag in task["usage"]
    assert "tasks/py/worktree_audit.py" in task["run"]
    check = cfg["doctor"]["checks"]["worktree-stale"]
    assert (
        check["run"]
        == "python3 -I -B tasks/py/worktree_audit.py --doctor --stale-days 3"
    )
