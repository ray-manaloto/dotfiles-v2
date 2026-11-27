"""Classify every git worktree and local branch so stale or unpushed work is visible.

Backs `mise run worktree:audit` and the warn-only `worktree-stale` doctor check. Stdlib only.
Read-only unless `--prune-merged`, which removes only `merged` entries whose tree is clean and
whose pinned PLAN_ID (if any) has a recorded post-task-learn review, as `pr:land` requires.

Statuses, first match wins:
  base           the default branch itself (still reported local-only/behind when that applies)
  no-commits     tip is a commit of a base ref's own first-parent history: a fresh, pulled or
                 fast-forwarded branch or detached HEAD with no work of its own (never pruned)
  merged         tip is an ancestor of a base ref (it was merged with a merge commit)
  squash-merged  every commit cherry-picked into a base ref, or the branch's whole diff has the
                 same patch-id as one base commit (squash merge)
  upstream-gone  the configured upstream branch no longer exists on the remote
  local-only     commits on no remote-tracking branch (or, with no remote, not on the base)
  behind         its upstream has commits the local branch lacks, and nothing is local-only
  pushed         everything is on a remote but not merged yet (an open PR, usually)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Hook environments set these; with `git -C <repo>` they would point git at the wrong repository.
_GIT_LOCAL_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
)
_DAY_S = 86400
# Doctor mode gives up (and reports "skipped") after this long; the check's mise `timeout` is longer,
# so a slow repository can never turn the warn-only check into a failure.
_DOCTOR_BUDGET_S = 45.0
# A post-task-learn review counts once it has one of these outcomes (as in pr:land; not owing_learn).
_REVIEW_DONE = frozenset({"proposed", "no_new_proposals", "applied"})
_PLAN_ID_RE = re.compile(r'^\s*PLAN_ID\s*=\s*"([^"]*)"', re.MULTILINE)
_LOG_P = (
    "log",
    "--no-merges",
    "--no-ext-diff",
    "--no-color",
    "-p",
    "--format=commit %H",
)
_deadline: float | None = None


class GitError(RuntimeError):
    """A git command failed."""


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCAL_ENV}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    return env


def _run(repo: Path, *args: str, data: bytes | None = None) -> tuple[int, bytes, str]:
    timeout = None
    if _deadline is not None:
        timeout = _deadline - time.monotonic()
        if timeout <= 0:
            raise TimeoutError("time budget used up")
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data if data is not None else b"",
        capture_output=True,
        env=_env(),
        check=False,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace").strip()


def _git(repo: Path, *args: str, data: bytes | None = None) -> str:
    rc, out, err = _run(repo, *args, data=data)
    if rc != 0:
        raise GitError(f"git {' '.join(args)}: {err or f'exit {rc}'}")
    return out.decode("utf-8", "replace").strip()


def _ok(repo: Path, *args: str) -> bool:
    return _run(repo, *args)[0] == 0


def _patch_ids(repo: Path, patch: bytes) -> list[tuple[str, str]]:
    """(patch-id, commit) pairs, in input order; commits with an empty diff produce none."""
    if not patch.strip():
        return []
    out = _git(repo, "patch-id", "--stable", data=patch)
    return [
        (pid, commit)
        for pid, _, commit in (line.partition(" ") for line in out.splitlines())
        if pid
    ]


def list_worktrees(repo: Path) -> list[dict[str, object]]:
    """Parse `git worktree list --porcelain`; the main worktree is always first."""
    trees: list[dict[str, object]] = []
    cur: dict[str, object] = {}
    for line in [*_git(repo, "worktree", "list", "--porcelain").splitlines(), ""]:
        if not line:
            if cur:
                trees.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            cur = {"path": value, "branch": None, "locked": False, "prunable": False}
        elif key == "HEAD":
            cur["head"] = value
        elif key == "branch":
            cur["branch"] = value.removeprefix("refs/heads/")
        elif key in ("locked", "prunable", "bare"):
            cur[key] = True
    return trees


def resolve_bases(repo: Path, base: str | None) -> tuple[str, list[str]]:
    """Return (default branch name, existing base refs to test merges against)."""
    remotes = _git(repo, "remote").split()
    remote = "origin" if "origin" in remotes else (remotes[0] if remotes else "")
    if base:
        if not _ok(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"):
            raise GitError(f"--base {base} is not a commit")
        name = base.removeprefix(f"{remote}/") if remote else base
        return name, [base]
    name = ""
    if remote:
        rc, out, _ = _run(
            repo, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"
        )
        if rc == 0:
            name = out.decode().strip().removeprefix(f"{remote}/")
    if not name:
        for cand in ("main", "master"):
            if _ok(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{cand}") or (
                remote
                and _ok(
                    repo,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/remotes/{remote}/{cand}",
                )
            ):
                name = cand
                break
    if not name:
        raise GitError(
            "no default branch found (no <remote>/HEAD, main or master); pass --base"
        )
    refs = [f"refs/heads/{name}"]
    if remote:
        refs.append(f"refs/remotes/{remote}/{name}")
    return name, [r for r in refs if _ok(repo, "show-ref", "--verify", "--quiet", r)]


class Bases:
    """The base refs plus per-run caches, so base-side history is read once per run, not per branch."""

    def __init__(self, repo: Path, name: str, refs: list[str]) -> None:
        self.repo = repo
        self.name = name
        self.refs = refs
        # main and origin/main usually point at one commit; read that history once.
        by_sha: dict[str, str] = {}
        for ref in refs:
            by_sha.setdefault(_git(repo, "rev-parse", f"{ref}^{{commit}}"), ref)
        self.distinct = list(by_sha.values())
        self._mainline: set[str] | None = None
        self._merged: dict[str, str] = {}
        self._forks: dict[tuple[str, str], str] = {}
        self._patches: dict[str, dict[str, str]] = {}

    def mainline(self) -> set[str]:
        """Commits on any base ref's first-parent history: where a branch with no work can point."""
        if self._mainline is None:
            self._mainline = set()
            for ref in self.distinct:
                self._mainline.update(
                    _git(self.repo, "rev-list", "--first-parent", ref).split()
                )
        return self._mainline

    def merged_into(self, tip: str) -> str:
        """The first base ref that *tip* is an ancestor of, else ''."""
        if tip not in self._merged:
            self._merged[tip] = next(
                (
                    b
                    for b in self.refs
                    if _ok(self.repo, "merge-base", "--is-ancestor", tip, b)
                ),
                "",
            )
        return self._merged[tip]

    def fork(self, base: str, tip: str) -> str:
        if (base, tip) not in self._forks:
            rc, out, _ = _run(self.repo, "merge-base", base, tip)
            self._forks[base, tip] = out.decode().strip() if rc == 0 else ""
        return self._forks[base, tip]

    def load_patches(self, tips: list[str]) -> None:
        """Index base commits by patch-id over one range that covers every tip's fork point."""
        for base in self.distinct:
            forks = sorted({f for f in (self.fork(base, t) for t in tips) if f})
            if not forks:
                continue
            floor = forks[0]
            if len(forks) > 1:
                rc, out, _ = _run(self.repo, "merge-base", "--octopus", *forks)
                floor = out.decode().strip() if rc == 0 else ""
            log = _run(self.repo, *_LOG_P, f"{floor}..{base}" if floor else base)[1]
            index: dict[str, str] = {}
            # git log is newest first, so a reverted-and-redone change maps to the redo.
            for pid, commit in _patch_ids(self.repo, log):
                index.setdefault(pid, commit)
            self._patches[base] = index

    def _landed(self, base: str, fork: str, pid: str) -> str:
        """The *base* commit after *fork* with patch-id *pid*, else ''."""
        commit = self._patches.get(base, {}).get(pid, "")
        if commit and not _ok(self.repo, "merge-base", "--is-ancestor", commit, fork):
            return commit
        return ""

    def squash_merged(self, tip: str) -> str:
        """Detail text when *tip*'s work already landed on a base under other hashes, else ''."""
        for base in self.distinct:
            fork = self.fork(base, tip)
            if not fork or not self._patches.get(base):
                continue
            count = int(
                _git(self.repo, "rev-list", "--count", "--no-merges", f"{fork}..{tip}")
            )
            mine = _patch_ids(self.repo, _run(self.repo, *_LOG_P, f"{fork}..{tip}")[1])
            if (
                mine
                and len(mine) == count
                and all(self._landed(base, fork, pid) for pid, _ in mine)
            ):
                return f"all {count} commit(s) cherry-picked into {base}"
            whole = _patch_ids(
                self.repo,
                _run(self.repo, "diff", "--no-ext-diff", "--no-color", fork, tip)[1],
            )
            commit = self._landed(base, fork, whole[0][0]) if whole else ""
            if commit:
                return f"squash of {count} commit(s) matches {base} {commit[:9]} (patch-id)"
        return ""


def classify(
    repo: Path,
    tip: str,
    *,
    branch: str | None,
    upstream: str,
    track: str,
    bases: Bases,
    has_remote: bool,
) -> tuple[str, str, int]:
    """Return (status, detail, local-only commit count) for one branch tip or detached HEAD."""
    is_base = branch == bases.name
    if has_remote:
        local_only = int(_git(repo, "rev-list", "--count", tip, "--not", "--remotes"))
    elif is_base or not bases.refs:
        local_only = 0
    else:
        local_only = int(_git(repo, "rev-list", "--count", tip, "--not", *bases.refs))
    if not is_base:
        # Not the reflog: a fresh branch that pulled or fast-forwarded has several entries.
        if tip in bases.mainline():
            return "no-commits", "tip is a commit of the base's own history", 0
        merged = bases.merged_into(tip)
        if merged:
            return "merged", f"merged into {merged}", 0
        detail = bases.squash_merged(tip)
        if detail:
            return "squash-merged", detail, local_only
    if track == "[gone]":
        return "upstream-gone", f"upstream {upstream} was deleted", local_only
    if local_only:
        return "local-only", f"{local_only} commit(s) on no remote", local_only
    if upstream and track:
        behind = int(_git(repo, "rev-list", "--count", f"{tip}..{upstream}"))
        if behind:
            return "behind", f"{behind} commit(s) behind {upstream}", 0
    return ("base" if is_base else "pushed"), "", 0


def _dirty(path: Path) -> bool | None:
    if not path.is_dir():
        return None
    rc, out, _ = _run(path, "status", "--porcelain", "--untracked-files=normal")
    return rc != 0 or bool(out.strip())


def audit(
    repo: Path, base: str | None = None, *, check_dirty: bool = True
) -> list[dict[str, object]]:
    """Classify every local branch and every worktree (detached ones get their own entry)."""
    trees = list_worktrees(repo)
    bases = Bases(repo, *resolve_bases(repo, base))
    has_remote = bool(_git(repo, "remote").split())
    by_branch = {t["branch"]: t for t in trees if t.get("branch")}
    fmt = "%00".join(
        (
            "%(refname)",
            "%(objectname)",
            "%(upstream:short)",
            "%(upstream:track)",
            "%(committerdate:unix)",
        )
    )
    now = time.time()
    entries: list[dict[str, object]] = []
    rows: list[tuple[str | None, str, str, str, int, dict[str, object] | None]] = []
    for line in _git(
        repo, "for-each-ref", f"--format={fmt}", "refs/heads"
    ).splitlines():
        ref, tip, upstream, track, when = line.split("\0")
        branch = ref.removeprefix("refs/heads/")
        rows.append(
            (branch, tip, upstream, track, int(when or 0), by_branch.get(branch))
        )
    for tree in trees:
        if tree.get("branch") or tree.get("bare") or not tree.get("head"):
            continue
        head = str(tree["head"])
        when = int(_git(repo, "show", "-s", "--format=%ct", head) or 0)
        rows.append((None, head, "", "", when, tree))
    # Only tips that are neither on the base nor merged need the (costly) squash check.
    bases.load_patches(
        [
            tip
            for branch, tip, *_ in rows
            if branch != bases.name
            and tip not in bases.mainline()
            and not bases.merged_into(tip)
        ]
    )
    for branch, tip, upstream, track, when, tree in rows:
        status, detail, local_only = classify(
            repo,
            tip,
            branch=branch,
            upstream=upstream,
            track=track,
            bases=bases,
            has_remote=has_remote,
        )
        path = Path(str(tree["path"])) if tree else None
        entries.append(
            {
                "branch": branch,
                "worktree": str(path) if path else None,
                "head": tip,
                "status": status,
                "detail": detail,
                "merged_into": bases.merged_into(tip) if status == "merged" else "",
                "local_only": local_only,
                "age_days": max(0.0, (now - when) / _DAY_S),
                "dirty": _dirty(path) if path and check_dirty else None,
                "locked": bool(tree and tree.get("locked")),
                "prunable": bool(tree and tree.get("prunable")),
            }
        )
    return entries


def _label(entry: dict[str, object]) -> str:
    return str(entry["branch"] or f"(detached {str(entry['head'])[:9]})")


def _render(entry: dict[str, object]) -> str:
    notes = [str(entry["detail"])] if entry["detail"] else []
    if entry["worktree"]:
        notes.append(f"worktree {entry['worktree']}")
    if entry["dirty"]:
        notes.append("dirty")
    if entry["locked"]:
        notes.append("locked")
    if entry["prunable"]:
        notes.append("prunable (directory missing; `git worktree prune`)")
    age = f"{int(float(str(entry['age_days'])))}d"
    return f"{entry['status']:<14} {_label(entry):<40} {age:>5}  {'; '.join(notes)}"


def _plan_id(worktree: Path) -> str:
    """The PLAN_ID a worktree pins in its git-ignored mise.local.toml (as worktree:new writes it)."""
    try:
        match = _PLAN_ID_RE.search(
            (worktree / "mise.local.toml").read_text(encoding="utf-8", errors="replace")
        )
    except OSError:
        return ""
    return match.group(1) if match else ""


def _learn_reviewed(plan_id: str) -> bool:
    """True when the agy-learn ledger has a finished post-task-learn review for *plan_id*."""
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    try:
        lines = (
            (Path(state) / "agy-learn" / "reviews.jsonl")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return False
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(row, dict)
            and row.get("plan_id") == plan_id
            and row.get("outcome") in _REVIEW_DONE
        ):
            return True
    return False


def _prune_blocker(
    main_root: Path,
    entry: dict[str, object],
    protected: set[Path],
    checked_out: dict[str, Path],
) -> str:
    """Why *entry* must be kept, re-checked against the repository as it is now; '' if prunable."""
    tip = str(entry["head"])
    base = str(entry["merged_into"])
    branch = str(entry["branch"]) if entry["branch"] else ""
    path = Path(str(entry["worktree"])) if entry["worktree"] else None
    if not _ok(main_root, "merge-base", "--is-ancestor", tip, base):
        return f"no longer an ancestor of {base}"
    if branch:
        out = _run(
            main_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
        )[1]
        if out.decode().strip() != tip:
            return "branch moved since the audit"
        where = checked_out.get(branch)
        if where is not None and (path is None or where != path.resolve()):
            return f"branch is checked out in {where}"
    if path is None:
        return ""
    if path.resolve() in protected:
        return "main or current worktree"
    if entry["locked"]:
        return "worktree is locked"
    if not path.is_dir():
        return "worktree directory is missing; run `git worktree prune`"
    if _dirty(path) is not False:
        return "worktree has uncommitted or untracked files"
    if _run(path, "rev-parse", "HEAD")[1].decode().strip() != tip:
        return "worktree HEAD moved since the audit"
    plan_id = _plan_id(path)
    if plan_id and not _learn_reviewed(plan_id):
        return (
            f"PLAN_ID {plan_id} has no recorded post-task-learn review; land it with "
            "`mise run pr:land` (only the owner may approve --skip-learn-check)"
        )
    return ""


def prune_merged(
    repo: Path, entries: list[dict[str, object]]
) -> tuple[list[str], list[str], list[str]]:
    """Remove merged worktrees with a clean tree, then their branches.

    Returns (done, kept, failed): kept entries were skipped on purpose, failed ones hit a git error.
    """
    trees = list_worktrees(repo)
    main_root = Path(str(trees[0]["path"])).resolve()
    protected = {main_root}
    for where in (repo, Path.cwd()):
        rc, out, _ = _run(where, "rev-parse", "--show-toplevel")
        if rc == 0:
            protected.add(Path(out.decode().strip()).resolve())
    checked_out = {
        str(t["branch"]): Path(str(t["path"])).resolve()
        for t in trees
        if t.get("branch")
    }
    done: list[str] = []
    kept: list[str] = []
    failed: list[str] = []
    for entry in entries:
        if entry["status"] != "merged":
            continue
        label = _label(entry)
        reason = _prune_blocker(main_root, entry, protected, checked_out)
        if reason:
            kept.append(f"{label}: {reason}")
            continue
        if entry["worktree"]:
            path = Path(str(entry["worktree"]))
            # Plan files are git-ignored and would be deleted with the worktree; keep them, as pr:land does.
            planning = path / ".planning"
            if planning.is_dir():
                for plan in planning.iterdir():
                    if plan.is_dir():
                        shutil.copytree(
                            plan,
                            main_root / ".planning" / plan.name,
                            dirs_exist_ok=True,
                        )
            rc, _, err = _run(main_root, "worktree", "remove", str(path))
            if rc != 0:
                failed.append(f"{label}: git worktree remove failed: {err}")
                continue
            done.append(f"removed worktree {path}")
        if entry["branch"]:
            branch = str(entry["branch"])
            # Not `git branch -d`: it checks only the upstream or HEAD, and "merged" may mean origin/main.
            # The old value makes the delete fail if the branch moved after the checks above.
            rc, _, err = _run(
                main_root,
                "update-ref",
                "-d",
                f"refs/heads/{branch}",
                str(entry["head"]),
            )
            if rc != 0:
                failed.append(f"{label}: deleting the branch failed: {err}")
                continue
            _run(main_root, "config", "--remove-section", f"branch.{branch}")
            done.append(f"deleted branch {branch}")
    return done, kept, failed


def _unpushed(entry: dict[str, object]) -> bool:
    """Commits that exist on no remote, whether or not the branch still has an upstream."""
    return entry["status"] == "local-only" or (
        entry["status"] == "upstream-gone" and int(str(entry["local_only"])) > 0
    )


def doctor(repo: Path, stale_days: float, budget_s: float = _DOCTOR_BUDGET_S) -> int:
    """Warn-only: name unpushed branches whose last commit is older than *stale_days*.

    Always returns 0: any error, including running out of *budget_s*, is reported as skipped.
    """
    global _deadline
    _deadline = time.monotonic() + budget_s
    try:
        entries = audit(repo, check_dirty=False)
    except Exception as exc:  # any failure is a skip: this check must never fail doctor
        sys.stdout.write(f"worktree-stale: skipped ({type(exc).__name__}: {exc})\n")
        return 0
    finally:
        _deadline = None
    stale = [
        e for e in entries if _unpushed(e) and float(str(e["age_days"])) > stale_days
    ]
    for e in stale:
        where = f" (worktree {e['worktree']})" if e["worktree"] else ""
        gone = f" ({e['detail']})" if e["status"] == "upstream-gone" else ""
        sys.stdout.write(
            f"WARNING worktree-stale: {_label(e)}{where}: {e['local_only']} commit(s) on no remote{gone}, "
            f"last commit {int(float(str(e['age_days'])))}d ago; push it or delete it\n"
        )
    sys.stdout.write(
        f"worktree-stale: {len(stale)} unpushed branch(es) older than {stale_days:g} days "
        f"(of {len(entries)} branches/worktrees); details: mise run worktree:audit\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify git worktrees and local branches (read-only by default)"
    )
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="repository to audit"
    )
    parser.add_argument("--base", default=None, help="base ref for merge checks")
    parser.add_argument(
        "--prune-merged",
        action="store_true",
        help="remove merged worktrees with a clean tree and their merged branches",
    )
    parser.add_argument(
        "--doctor", action="store_true", help="warn-only doctor output; always exits 0"
    )
    parser.add_argument("--stale-days", type=float, default=3.0)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    if args.doctor:
        return doctor(repo, args.stale_days)
    try:
        entries = audit(repo, args.base)
    except GitError as exc:
        sys.stderr.write(f"worktree:audit: {exc}\n")
        return 2
    main_root = list_worktrees(repo)[0]["path"]
    sys.stdout.write(f"worktree:audit {main_root}\n")
    for entry in sorted(entries, key=lambda e: (str(e["status"]), _label(e))):
        sys.stdout.write(_render(entry) + "\n")
    counts: dict[str, int] = {}
    for entry in entries:
        counts[str(entry["status"])] = counts.get(str(entry["status"]), 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    sys.stdout.write(f"{len(entries)} branches/worktrees: {summary}\n")
    if not args.prune_merged:
        if counts.get("merged"):
            sys.stdout.write(
                "nothing deleted; --prune-merged removes merged entries with a clean tree\n"
            )
        return 0
    done, kept, failed = prune_merged(repo, entries)
    for line in done:
        sys.stdout.write(f"pruned: {line}\n")
    for line in kept:
        sys.stdout.write(f"kept: {line}\n")
    for line in failed:
        sys.stdout.write(f"failed: {line}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
