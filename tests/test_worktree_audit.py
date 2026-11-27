"""worktree:audit classification, --prune-merged, and the warn-only worktree-stale doctor check."""

from __future__ import annotations

import json
import os
import shutil
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
        # The post-task-learn ledger --prune-merged consults; never the real one.
        "XDG_STATE_HOME": str(tmp_path / "state"),
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


def _branch_with(main: Path, branch: str, *files: str, days_ago: int = 0) -> None:
    _git(main, "checkout", "-q", "-b", branch)
    for name in files:
        _commit(main, name, days_ago=days_ago)
    _git(main, "checkout", "-q", "main")


def _merged_worktree(main: Path, branch: str, path: Path) -> None:
    """A worktree on *branch* with one commit, merged into local main with a merge commit."""
    _git(main, "worktree", "add", "-q", "-b", branch, str(path))
    _commit(path, f"{path.name}.txt")
    _git(main, "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch)


def _branches(main: Path) -> set[str]:
    return set(
        _git(
            main, "for-each-ref", "--format=%(refname:short)", "refs/heads"
        ).splitlines()
    )


@pytest.fixture
def fixture_repo(tmp_path: Path, git_env: dict[str, str]) -> dict[str, Path]:
    origin = tmp_path / "origin.git"
    main = tmp_path / "repo"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-q", "-b", "main", str(main))
    _git(main, "remote", "add", "origin", str(origin))
    (main / ".gitignore").write_text(".planning/\nmise.local.toml\n", encoding="utf-8")
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
    # Pushed, then the remote branch was deleted without the work landing on main (a closed PR).
    _branch_with(main, "gone", "gone.txt", days_ago=5)
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
    warnings = sorted(line for line in out.splitlines() if line.startswith("WARNING"))
    assert len(warnings) == 2, out
    # A closed PR's branch: the remote copy is gone, so its commit exists nowhere else.
    assert "worktree-stale: gone:" in warnings[0]
    assert "1 commit(s) on no remote (upstream origin/gone was deleted)" in warnings[0]
    assert "wt/stale" in warnings[1] and "1 commit(s) on no remote" in warnings[1]
    assert "recent-local" not in out
    assert "2 unpushed branch(es) older than 3 days" in out


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


def test_doctor_never_fails_on_unexpected_errors_or_timeouts(
    fixture_repo: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = load_module("worktree_audit")
    assert mod.doctor(fixture_repo["main"], 3, budget_s=0) == 0
    assert "worktree-stale: skipped (TimeoutError" in capsys.readouterr().out
    assert mod._deadline is None

    def boom(*_args: object, **_kwargs: object) -> None:
        raise ValueError("unexpected")

    monkeypatch.setattr(mod, "audit", boom)
    assert mod.doctor(fixture_repo["main"], 3) == 0
    assert "worktree-stale: skipped (ValueError: unexpected)" in capsys.readouterr().out


def test_prune_keeps_worktrees_with_no_work_of_their_own(
    fixture_repo: dict[str, Path], tmp_path: Path
) -> None:
    """A detached HEAD at main, and a fresh branch that fast-forwarded (reflog > 1 entry)."""
    mod = load_module("worktree_audit")
    main = fixture_repo["main"]
    detached = tmp_path / "repo-detached-main"
    synced = tmp_path / "repo-synced"
    _git(main, "worktree", "add", "-q", "--detach", str(detached), "main")
    _git(main, "worktree", "add", "-q", "-b", "wt/synced", str(synced), "main~2")
    _git(synced, "merge", "-q", "--ff-only", "main")
    _git(synced, "pull", "-q", "--ff-only", "origin", "main")
    assert len(_git(main, "reflog", "show", "refs/heads/wt/synced").splitlines()) > 1
    entries = mod.audit(main)
    by_tree = {
        Path(str(e["worktree"])).resolve(): e["status"]
        for e in entries
        if e["worktree"]
    }
    assert by_tree[detached.resolve()] == "no-commits"
    assert by_tree[synced.resolve()] == "no-commits"
    assert mod.main(["--repo", str(main), "--prune-merged"]) == 0
    assert detached.is_dir() and synced.is_dir()
    assert "wt/synced" in _branches(main)


def test_prune_merged_when_the_merge_is_only_on_origin_main(
    fixture_repo: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The PR merged on GitHub and was fetched, but local main was not pulled yet."""
    mod = load_module("worktree_audit")
    main = fixture_repo["main"]
    wt = tmp_path / "repo-remote"
    _git(main, "worktree", "add", "-q", "-b", "wt/remote", str(wt))
    _commit(wt, "remote.txt")
    # Pushed without -u, as pr:ship does, so the branch has no upstream.
    _git(wt, "push", "-q", "origin", "HEAD:refs/heads/wt/remote")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(fixture_repo["origin"]), str(other))
    _git(other, "merge", "-q", "--no-ff", "-m", "merge PR", "origin/wt/remote")
    _git(other, "push", "-q", "origin", "main")
    _git(main, "fetch", "-q", "origin")
    tip = _git(main, "rev-parse", "wt/remote")
    assert _git(main, "branch", "--contains", tip, "--list", "main") == ""
    entry = next(e for e in mod.audit(main) if e["branch"] == "wt/remote")
    assert (entry["status"], entry["merged_into"]) == (
        "merged",
        "refs/remotes/origin/main",
    )
    assert mod.main(["--repo", str(main), "--prune-merged"]) == 0, capsys.readouterr()
    assert not wt.exists()
    assert "wt/remote" not in _branches(main)


def test_prune_keeps_locked_and_missing_worktrees(
    fixture_repo: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load_module("worktree_audit")
    main = fixture_repo["main"]
    locked = tmp_path / "repo-locked"
    missing = tmp_path / "repo-missing"
    _merged_worktree(main, "wt/locked", locked)
    _merged_worktree(main, "wt/missing", missing)
    _git(main, "worktree", "lock", str(locked))
    shutil.rmtree(missing)
    assert mod.main(["--repo", str(main), "--prune-merged"]) == 0
    out = capsys.readouterr().out
    assert "kept: wt/locked: worktree is locked" in out
    assert "kept: wt/missing: worktree directory is missing" in out
    assert locked.is_dir()
    assert {"wt/locked", "wt/missing"} <= _branches(main)


def test_prune_failure_exits_1_and_branch_names_do_not(
    fixture_repo: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load_module("worktree_audit")
    main = fixture_repo["main"]
    # A dirty merged worktree whose branch name contains "failed" is kept, and that is not an error.
    failed_wt = tmp_path / "repo-failed"
    _merged_worktree(main, "failed-experiment", failed_wt)
    (failed_wt / "scratch.txt").write_text("unsaved\n", encoding="utf-8")
    assert mod.main(["--repo", str(main)]) == 0
    assert mod.main(["--repo", str(main), "--prune-merged"]) == 0
    assert (
        "kept: failed-experiment: worktree has uncommitted" in capsys.readouterr().out
    )
    # A git error while deleting a branch is a failure: exit 1, and the branch stays.
    _branch_with(main, "merged-again", "again.txt")
    _git(main, "merge", "-q", "--no-ff", "-m", "merge again", "merged-again")
    lock = Path(_git(main, "rev-parse", "--git-common-dir"))
    lock = (
        main / lock if not lock.is_absolute() else lock
    ) / "refs/heads/merged-again.lock"
    lock.write_text("", encoding="utf-8")
    assert mod.main(["--repo", str(main), "--prune-merged"]) == 1
    assert "failed: merged-again: deleting the branch failed" in capsys.readouterr().out
    assert "merged-again" in _branches(main)


def test_prune_keeps_a_worktree_whose_plan_has_no_learn_review(
    fixture_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load_module("worktree_audit")
    main = fixture_repo["main"]
    (fixture_repo["merged"] / "mise.local.toml").write_text(
        '[env]\nPLAN_ID = "plan-merged"\n', encoding="utf-8"
    )
    ledger = Path(os.environ["XDG_STATE_HOME"]) / "agy-learn" / "reviews.jsonl"
    ledger.parent.mkdir(parents=True)
    # owing_learn is not a finished review.
    ledger.write_text(
        json.dumps({"plan_id": "plan-merged", "outcome": "owing_learn"}) + "\n",
        encoding="utf-8",
    )
    assert mod.main(["--repo", str(main), "--prune-merged"]) == 0
    assert (
        "kept: wt/merged: PLAN_ID plan-merged has no recorded post-task-learn review"
        in capsys.readouterr().out
    )
    assert fixture_repo["merged"].is_dir() and "wt/merged" in _branches(main)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"plan_id": "plan-merged", "outcome": "applied"}) + "\n")
    assert mod.main(["--repo", str(main), "--prune-merged"]) == 0
    assert not fixture_repo["merged"].exists() and "wt/merged" not in _branches(main)


def test_explicit_base(fixture_repo: dict[str, Path]) -> None:
    mod = load_module("worktree_audit")
    main = fixture_repo["main"]
    by_branch = {e["branch"]: e for e in mod.audit(main, base="pushed")}
    assert by_branch["pushed"]["status"] == "base"
    assert by_branch["main"]["status"] == "no-commits"
    assert by_branch["merged-no-wt"]["merged_into"] == "pushed"
    assert mod.main(["--repo", str(main), "--base", "no-such-ref"]) == 2


def test_repo_without_a_remote(tmp_path: Path, git_env: dict[str, str]) -> None:
    mod = load_module("worktree_audit")
    repo = tmp_path / "solo"
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _commit(repo, "README")
    _branch_with(repo, "merged", "m.txt")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "merged")
    _branch_with(repo, "work", "w1.txt", "w2.txt")
    by_branch = {e["branch"]: e for e in mod.audit(repo)}
    assert {b: e["status"] for b, e in by_branch.items()} == {
        "main": "base",
        "merged": "merged",
        "work": "local-only",
    }
    assert by_branch["work"]["local_only"] == 2


def test_master_without_remote_head(tmp_path: Path, git_env: dict[str, str]) -> None:
    mod = load_module("worktree_audit")
    origin = tmp_path / "origin.git"
    repo = tmp_path / "legacy"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    _git(tmp_path, "init", "-q", "-b", "master", str(repo))
    _git(repo, "remote", "add", "origin", str(origin))
    _commit(repo, "README")
    _git(repo, "push", "-q", "-u", "origin", "master")
    # No `remote set-head`: refs/remotes/origin/HEAD does not exist.
    assert mod.resolve_bases(repo, None) == (
        "master",
        ["refs/heads/master", "refs/remotes/origin/master"],
    )
    _git(repo, "checkout", "-q", "-b", "topic")
    _commit(repo, "t.txt")
    _git(repo, "checkout", "-q", "master")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge topic", "topic")
    _git(repo, "push", "-q", "origin", "master")
    by_branch = {e["branch"]: e["status"] for e in mod.audit(repo)}
    assert by_branch == {"master": "base", "topic": "merged"}


@pytest.mark.parametrize("flag", ["false", "true"])
def test_task_flag_wiring(fixture_repo: dict[str, Path], flag: str) -> None:
    """Run the task's real shell body: --prune-merged only when mise passes usage_prune_merged=true."""
    run = tomllib.loads((REPO_ROOT / "mise.toml").read_text(encoding="utf-8"))["tasks"][
        "worktree:audit"
    ]["run"]
    env = {
        **os.environ,
        "usage_repo": str(fixture_repo["main"]),
        "usage_prune_merged": flag,
    }
    result = subprocess.run(
        ["sh", "-c", run.replace("{{ config_root }}", str(REPO_ROOT))],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert fixture_repo["merged"].exists() == (flag == "false")
