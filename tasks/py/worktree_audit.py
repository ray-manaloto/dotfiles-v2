"""Classify every git worktree and local branch so stale or unpushed work is visible.

Backs `mise run worktree:audit` and the warn-only `worktree-stale` doctor check. Stdlib only.
Read-only unless `--prune-merged`, which removes only `merged` entries whose tree is clean.

Statuses, first match wins:
  base           the default branch itself (still reported local-only/behind when that applies)
  merged         tip is an ancestor of a base ref
  no-commits     tip is an ancestor of a base ref and the branch reflog shows it never moved
                 (a fresh worktree; never pruned)
  squash-merged  every commit cherry-picked into a base ref, or the branch's whole diff has the
                 same patch-id as one base commit (squash merge)
  upstream-gone  the configured upstream branch no longer exists on the remote
  local-only     commits on no remote-tracking branch (or, with no remote, not on the base)
  behind         its upstream has commits the local branch lacks, and nothing is local-only
  pushed         everything is on a remote but not merged yet (an open PR, usually)
"""

from __future__ import annotations

import argparse
import os
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


class GitError(RuntimeError):
    """A git command failed."""


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCAL_ENV}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    return env


def _run(repo: Path, *args: str, data: bytes | None = None) -> tuple[int, bytes, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data if data is not None else b"",
        capture_output=True,
        env=_env(),
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace").strip()


def _git(repo: Path, *args: str, data: bytes | None = None) -> str:
    rc, out, err = _run(repo, *args, data=data)
    if rc != 0:
        raise GitError(f"git {' '.join(args)}: {err or f'exit {rc}'}")
    return out.decode("utf-8", "replace").strip()


def _ok(repo: Path, *args: str) -> bool:
    return _run(repo, *args)[0] == 0


def _patch_ids(repo: Path, patch: bytes) -> list[str]:
    if not patch.strip():
        return []
    out = _git(repo, "patch-id", "--stable", data=patch)
    return [line.split()[0] for line in out.splitlines() if line.strip()]


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


def _squash_merged(repo: Path, tip: str, bases: list[str]) -> str:
    """Detail text when *tip*'s work already landed on a base under other hashes, else ''."""
    for base in bases:
        cherry = _git(repo, "cherry", base, tip).splitlines()
        if cherry and all(line.startswith("- ") for line in cherry):
            return f"all {len(cherry)} commit(s) cherry-picked into {base}"
        rc, out, _ = _run(repo, "merge-base", base, tip)
        if rc != 0:
            continue
        mb = out.decode().strip()
        mine = _patch_ids(
            repo, _run(repo, "diff", "--no-ext-diff", "--no-color", mb, tip)[1]
        )
        if not mine:
            continue
        log = _run(
            repo,
            "log",
            "--no-merges",
            "--no-ext-diff",
            "--no-color",
            "-p",
            "--format=commit %H",
            f"{mb}..{base}",
        )[1]
        for line in _git(repo, "patch-id", "--stable", data=log).splitlines():
            pid, _, commit = line.partition(" ")
            if pid == mine[0]:
                return f"squash of {len(cherry)} commit(s) matches {base} {commit[:9]} (patch-id)"
    return ""


def _never_moved(repo: Path, branch: str, tip: str) -> bool:
    """True when the branch reflog has only its creation entry, still at *tip*."""
    rc, out, _ = _run(
        repo, "reflog", "show", "--format=%H", f"refs/heads/{branch}", "--"
    )
    lines = out.decode().split()
    return rc == 0 and lines == [tip]


def classify(
    repo: Path,
    tip: str,
    *,
    branch: str | None,
    upstream: str,
    track: str,
    base_name: str,
    bases: list[str],
    has_remote: bool,
) -> tuple[str, str, int]:
    """Return (status, detail, local-only commit count) for one branch tip or detached HEAD."""
    is_base = branch == base_name
    if has_remote:
        local_only = int(_git(repo, "rev-list", "--count", tip, "--not", "--remotes"))
    elif is_base or not bases:
        local_only = 0
    else:
        local_only = int(_git(repo, "rev-list", "--count", tip, "--not", *bases))
    if not is_base:
        for base in bases:
            if _ok(repo, "merge-base", "--is-ancestor", tip, base):
                if branch and _never_moved(repo, branch, tip):
                    return "no-commits", "branch never moved since it was created", 0
                return "merged", f"merged into {base}", 0
        detail = _squash_merged(repo, tip, bases)
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


def audit(repo: Path, base: str | None = None) -> list[dict[str, object]]:
    """Classify every local branch and every worktree (detached ones get their own entry)."""
    trees = list_worktrees(repo)
    base_name, bases = resolve_bases(repo, base)
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
    rows: list[tuple[str | None, str, str, str, int]] = []
    for line in _git(
        repo, "for-each-ref", f"--format={fmt}", "refs/heads"
    ).splitlines():
        ref, tip, upstream, track, when = line.split("\0")
        rows.append(
            (ref.removeprefix("refs/heads/"), tip, upstream, track, int(when or 0))
        )
    for tree in trees:
        if tree.get("branch") or tree.get("bare") or not tree.get("head"):
            continue
        head = str(tree["head"])
        when = int(_git(repo, "show", "-s", "--format=%ct", head) or 0)
        rows.append((None, head, "", "", when))
    detached = iter(
        t for t in trees if not t.get("branch") and not t.get("bare") and t.get("head")
    )
    for branch, tip, upstream, track, when in rows:
        tree = by_branch.get(branch) if branch else next(detached)
        status, detail, local_only = classify(
            repo,
            tip,
            branch=branch,
            upstream=upstream,
            track=track,
            base_name=base_name,
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
                "local_only": local_only,
                "age_days": max(0.0, (now - when) / _DAY_S),
                "dirty": _dirty(path) if path else None,
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


def prune_merged(
    repo: Path, entries: list[dict[str, object]]
) -> tuple[list[str], list[str]]:
    """Remove merged worktrees with a clean tree, then their branches (`git branch -d` only)."""
    trees = list_worktrees(repo)
    main_root = Path(str(trees[0]["path"])).resolve()
    protected = {main_root}
    for where in (repo, Path.cwd()):
        rc, out, _ = _run(where, "rev-parse", "--show-toplevel")
        if rc == 0:
            protected.add(Path(out.decode().strip()).resolve())
    done: list[str] = []
    skipped: list[str] = []
    for entry in entries:
        if entry["status"] != "merged":
            continue
        label = _label(entry)
        if entry["worktree"]:
            path = Path(str(entry["worktree"]))
            reason = ""
            if path.resolve() in protected:
                reason = "main or current worktree"
            elif entry["locked"]:
                reason = "worktree is locked"
            elif not path.is_dir():
                reason = "worktree directory is missing; run `git worktree prune`"
            elif _dirty(path) is not False:
                reason = "worktree has uncommitted or untracked files"
            if reason:
                skipped.append(f"{label}: {reason}")
                continue
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
                skipped.append(f"{label}: git worktree remove failed: {err}")
                continue
            done.append(f"removed worktree {path}")
        if entry["branch"]:
            rc, _, err = _run(main_root, "branch", "-d", str(entry["branch"]))
            if rc != 0:
                skipped.append(f"{label}: git branch -d failed: {err}")
                continue
            done.append(f"deleted branch {entry['branch']}")
    return done, skipped


def doctor(repo: Path, stale_days: float) -> int:
    """Warn-only: name local-only branches whose last commit is older than *stale_days*."""
    try:
        entries = audit(repo)
    except GitError as exc:
        sys.stdout.write(f"worktree-stale: skipped ({exc})\n")
        return 0
    stale = [
        e
        for e in entries
        if e["status"] == "local-only" and float(str(e["age_days"])) > stale_days
    ]
    for e in stale:
        where = f" (worktree {e['worktree']})" if e["worktree"] else ""
        sys.stdout.write(
            f"WARNING worktree-stale: {_label(e)}{where}: {e['local_only']} commit(s) on no remote, "
            f"last commit {int(float(str(e['age_days'])))}d ago; push it or delete it\n"
        )
    sys.stdout.write(
        f"worktree-stale: {len(stale)} local-only branch(es) older than {stale_days:g} days "
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
    done, skipped = prune_merged(repo, entries)
    for line in done:
        sys.stdout.write(f"pruned: {line}\n")
    for line in skipped:
        sys.stdout.write(f"kept: {line}\n")
    return 1 if any("failed" in line for line in skipped) else 0


if __name__ == "__main__":
    sys.exit(main())
