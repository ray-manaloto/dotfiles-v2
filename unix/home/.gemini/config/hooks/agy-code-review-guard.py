#!/usr/bin/env python3
"""Antigravity PreToolUse guard requiring /code-review receipt before pushing or merging."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
DOC_SUFFIXES = frozenset({".md", ".mdx", ".markdown", ".rst", ".adoc", ".txt"})
RECEIPT_FILENAME = "agy-code-review-receipt.json"
MAX_STORED_RECEIPTS = 32
SEPARATORS = frozenset(
    {";", "&&", "||", "|", "&", "\n", "then", "do", "else", "elif", "{", "}", "(", ")"}
)


def _allow() -> dict[str, Any]:
    return {"decision": "allow"}


def _deny(reason: str) -> dict[str, Any]:
    return {"decision": "deny", "reason": reason}


def _split_shell_segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";|&")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.wordchars = lexer.wordchars.replace("\n", "")
    try:
        tokens = list(lexer)
    except ValueError:
        return []

    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in SEPARATORS:
            if cur:
                segments.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def _strip_env_prefix(segment: list[str]) -> list[str]:
    idx = 0
    while idx < len(segment):
        tok = segment[idx]
        if ENV_ASSIGN_RE.match(tok) or Path(tok).name == "env":
            idx += 1
            continue
        break
    return segment[idx:]


def _git_output(cwd: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout.strip()


def _resolve_repo_dirs(candidate: Path) -> tuple[Path, Path] | None:
    if not candidate.exists():
        return None
    start = candidate if candidate.is_dir() else candidate.parent
    rc1, top = _git_output(start, "rev-parse", "--show-toplevel")
    rc2, common = _git_output(start, "rev-parse", "--git-common-dir")
    if rc1 != 0 or rc2 != 0 or not top or not common:
        return None
    top_path = Path(top).resolve(strict=False)
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (start / common_path).resolve(strict=False)
    else:
        common_path = common_path.resolve(strict=False)
    return top_path, common_path


def _resolve_target_commit(repo_root: Path, ref: str) -> tuple[str, str] | None:
    rc1, commit_sha = _git_output(
        repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}"
    )
    rc2, tree_sha = _git_output(repo_root, "rev-parse", "--verify", f"{ref}^{{tree}}")
    if rc1 != 0 or rc2 != 0 or not commit_sha or not tree_sha:
        return None
    return commit_sha, tree_sha


def _changed_files(
    repo_root: Path, target_sha: str, *, base_ref: str | None = None
) -> list[str] | None:
    bases = (
        [base_ref]
        if base_ref
        else ["@{u}", "origin/HEAD", "origin/main", "origin/master"]
    )
    for candidate_base in bases:
        if not candidate_base:
            continue
        rc_b, base_sha = _git_output(repo_root, "rev-parse", "--verify", candidate_base)
        if rc_b != 0 or not base_sha:
            continue
        if base_sha == target_sha:
            return []
        rc_d, out = _git_output(
            repo_root, "diff", "--name-only", f"{base_sha}..{target_sha}"
        )
        if rc_d == 0:
            return [line for line in out.splitlines() if line.strip()]

    rc_t, out = _git_output(
        repo_root,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "--root",
        target_sha,
    )
    if rc_t != 0:
        return None
    return [line for line in out.splitlines() if line.strip()]


def _receipt_path(common_dir: Path) -> Path:
    return common_dir / RECEIPT_FILENAME


def _load_receipts(common_dir: Path) -> dict[str, dict[str, str]]:
    path = _receipt_path(common_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    receipts = raw.get("receipts")
    if not isinstance(receipts, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for k, v in receipts.items():
        if isinstance(k, str) and isinstance(v, dict):
            commit_sha = v.get("commit_sha")
            tree_sha = v.get("tree_sha")
            if isinstance(commit_sha, str) and isinstance(tree_sha, str):
                out[k] = {
                    "commit_sha": commit_sha,
                    "tree_sha": tree_sha,
                    "recorded_at": str(v.get("recorded_at", "")),
                    "summary": str(v.get("summary", "")),
                }
    return out


def _has_valid_receipt(common_dir: Path, commit_sha: str, tree_sha: str) -> bool:
    entry = _load_receipts(common_dir).get(commit_sha)
    if not entry:
        return False
    return entry.get("commit_sha") == commit_sha and entry.get("tree_sha") == tree_sha


def _record_receipt(repo: Path, ref: str, summary: str) -> dict[str, Any]:
    resolved = _resolve_repo_dirs(repo)
    if resolved is None:
        msg = f"Not inside a git repository: {repo}"
        raise RuntimeError(msg)
    repo_root, common_dir = resolved
    target = _resolve_target_commit(repo_root, ref)
    if target is None:
        msg = f"Could not resolve git commit/tree for ref '{ref}' in {repo_root}"
        raise RuntimeError(msg)
    commit_sha, tree_sha = target

    receipts = _load_receipts(common_dir)
    receipts[commit_sha] = {
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": summary.strip(),
    }
    keys = list(receipts.keys())
    if len(keys) > MAX_STORED_RECEIPTS:
        for old_key in keys[:-MAX_STORED_RECEIPTS]:
            receipts.pop(old_key, None)

    path = _receipt_path(common_dir)
    payload = {"latest": commit_sha, "receipts": receipts}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "repo_root": str(repo_root),
        "receipt_file": str(path),
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
    }


def _parse_git_segment(
    tokens: list[str], cwd: Path
) -> tuple[str, Path, str, str | None] | None:
    """Return (op, repo_dir, ref_to_check, base_ref_override) or None."""
    if not tokens or Path(tokens[0]).name != "git":
        return None
    repo_dir = cwd
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-C" and i + 1 < len(tokens):
            raw_dir = Path(os.path.expanduser(tokens[i + 1]))
            repo_dir = raw_dir if raw_dir.is_absolute() else (cwd / raw_dir)
            i += 2
            continue
        if tok.startswith("-C") and len(tok) > 2:
            raw_dir = Path(os.path.expanduser(tok[2:]))
            repo_dir = raw_dir if raw_dir.is_absolute() else (cwd / raw_dir)
            i += 1
            continue
        if tok in {"-c", "--git-dir", "--work-tree", "--namespace"} and i + 1 < len(
            tokens
        ):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break

    if i >= len(tokens):
        return None
    sub = tokens[i]
    rest = tokens[i + 1 :]

    if sub == "push":
        positionals: list[str] = []
        idx = 0
        while idx < len(rest):
            tok = rest[idx]
            if tok in {"--dry-run", "-n", "--delete", "-d"}:
                return None
            if tok in {
                "-o",
                "--push-option",
                "--receive-pack",
                "--exec",
                "--repo",
            } and idx + 1 < len(rest):
                idx += 2
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            positionals.append(tok)
            idx += 1
        refspecs = positionals[1:]
        if refspecs and all(spec.startswith(":") for spec in refspecs):
            return None
        return "git push", repo_dir, "HEAD", None

    if sub == "merge":
        has_ff_only = False
        merge_refs: list[str] = []
        idx = 0
        while idx < len(rest):
            tok = rest[idx]
            if tok in {"--abort", "--continue", "--quit"}:
                return None
            if tok == "--ff-only":
                has_ff_only = True
                idx += 1
                continue
            if tok in {
                "-m",
                "-F",
                "-s",
                "-X",
                "--strategy",
                "--strategy-option",
                "--into-name",
            } and idx + 1 < len(rest):
                idx += 2
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            merge_refs.append(tok)
            idx += 1
        if not has_ff_only or not merge_refs:
            return None
        rc_b, branch = _git_output(
            repo_dir, "symbolic-ref", "--quiet", "--short", "HEAD"
        )
        if rc_b != 0 or branch not in {"main", "master"}:
            return None
        return f"git merge --ff-only {merge_refs[0]}", repo_dir, merge_refs[0], "HEAD"

    return None


def _parse_mise_or_pr_segment(
    tokens: list[str], cwd: Path
) -> tuple[str, Path, str, str | None] | None:
    if not tokens:
        return None
    exe = Path(tokens[0]).name
    if exe == "repotools" and len(tokens) >= 2 and tokens[1] == "pr-create":
        return "repotools pr-create", cwd, "HEAD", None
    if exe == "gh" and len(tokens) >= 3 and tokens[1] == "pr" and tokens[2] == "create":
        return "gh pr create", cwd, "HEAD", None
    if exe == "mise":
        repo_dir = cwd
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok in {"-C", "--cd"} and i + 1 < len(tokens):
                raw_dir = Path(os.path.expanduser(tokens[i + 1]))
                repo_dir = raw_dir if raw_dir.is_absolute() else (cwd / raw_dir)
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            break
        if i + 1 < len(tokens) and tokens[i] in {"run", "r"}:
            j = i + 1
            while j < len(tokens) and tokens[j].startswith("-"):
                j += 1
            if j < len(tokens) and tokens[j] in {
                "ship",
                "pr-create",
                "//repo:pr-create",
            }:
                return f"mise run {tokens[j]}", repo_dir, "HEAD", None
    return None


def _check_command(command: str, initial_cwd: Path | None = None) -> dict[str, Any]:
    cwd = (initial_cwd or Path.cwd()).resolve(strict=False)
    for segment in _split_shell_segments(command):
        tokens = _strip_env_prefix(segment)
        if not tokens:
            continue
        if tokens[0] == "cd" and len(tokens) >= 2 and not tokens[1].startswith("-"):
            target = Path(os.path.expanduser(tokens[1]))
            cwd = (target if target.is_absolute() else (cwd / target)).resolve(
                strict=False
            )
            continue

        parsed = _parse_git_segment(tokens, cwd) or _parse_mise_or_pr_segment(
            tokens, cwd
        )
        if parsed is None:
            continue

        op_label, repo_candidate, target_ref, base_override = parsed
        resolved = _resolve_repo_dirs(repo_candidate)
        if resolved is None:
            continue
        repo_root, common_dir = resolved
        target = _resolve_target_commit(repo_root, target_ref)
        if target is None:
            continue
        target_sha, target_tree_sha = target

        changed = _changed_files(repo_root, target_sha, base_ref=base_override)
        if changed is not None:
            if not changed:
                continue
            if all(Path(path).suffix.lower() in DOC_SUFFIXES for path in changed):
                continue

        if _has_valid_receipt(common_dir, target_sha, target_tree_sha):
            continue

        return _deny(
            f"code-review-guard (code-review-protocol.md): '{op_label}' requires a "
            f"/code-review:code-review (+ code-review-commons) receipt for {target_sha[:7]} "
            f"in {repo_root}. Run the review, fix confirmed CRITICAL/HIGH/MEDIUM findings, "
            "then run:\n"
            f'python3 -I -B ~/.gemini/config/hooks/agy-code-review-guard.py --record --repo "{repo_root}" --summary "<summary>"'
        )
    return _allow()


def _run_hook(payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_CODE_REVIEW_GUARD") == "1":
        return _allow()

    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        tool_name = tool_call.get("name")
        args = tool_call.get("args")
    else:
        tool_name = payload.get("tool_name")
        args = payload.get("tool_input")

    if tool_name != "run_command" or not isinstance(args, dict):
        return _allow()

    cmd = args.get("CommandLine")
    if not isinstance(cmd, str) or not cmd.strip():
        return _allow()
    cwd_raw = args.get("Cwd") or payload.get("cwd")
    cwd = (
        Path(os.path.expanduser(cwd_raw))
        if isinstance(cwd_raw, str) and cwd_raw.strip()
        else None
    )
    return _check_command(cmd, initial_cwd=cwd)


def _process_stdin_text(raw_stdin: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, ValueError):
        return _allow()
    if not isinstance(payload, dict):
        return _allow()
    return _run_hook(payload)


def _check_hooks_json(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"invalid hooks.json root in {path}"
        raise TypeError(msg)
    if "agy-code-review-guard" not in data:
        raise AssertionError(f"missing 'agy-code-review-guard' in {path}")
    block = data["agy-code-review-guard"]
    if not isinstance(block, dict):
        msg = f"invalid agy-code-review-guard block in {path}"
        raise TypeError(msg)
    pre_handlers = block.get("PreToolUse")
    if not isinstance(pre_handlers, list) or not pre_handlers:
        raise AssertionError(f"missing PreToolUse handlers in {path}")
    first = pre_handlers[0]
    matcher = first.get("matcher", "") if isinstance(first, dict) else ""
    if not isinstance(matcher, str) or "run_command" not in matcher:
        raise AssertionError(f"unexpected PreToolUse matcher: {matcher!r}")
    inner = first.get("hooks") if isinstance(first, dict) else None
    if not isinstance(inner, list) or not inner:
        raise AssertionError(f"missing PreToolUse hooks list in {path}")
    cmd = inner[0].get("command", "") if isinstance(inner[0], dict) else ""
    if not isinstance(cmd, str) or "agy-code-review-guard.py" not in cmd:
        raise AssertionError(f"unexpected hook command: {cmd!r}")
    if cmd.startswith("/") or "/Users/" in cmd:
        raise AssertionError(
            f"hook command must be portable relative path, got: {cmd!r}"
        )


def _git_exec(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _run_self_check(hooks_json_path: Path | None) -> list[str]:
    checks: list[str] = []

    def call_cmd(cmd: str, cwd: Path) -> dict[str, Any]:
        return _run_hook(
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": cmd, "Cwd": str(cwd)},
                }
            }
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        non_git = root / "non_git"
        non_git.mkdir()
        assert call_cmd("git push origin main", non_git) == _allow()
        checks.append("non_git_allowed")

        remote = root / "origin.git"
        _git_exec(root, "init", "--bare", "--initial-branch=main", str(remote))
        repo = root / "repo"
        _git_exec(root, "clone", str(remote), str(repo))
        _git_exec(repo, "config", "user.name", "Test User")
        _git_exec(repo, "config", "user.email", "test@example.com")

        (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
        _git_exec(repo, "add", "README.md")
        _git_exec(repo, "commit", "-m", "init")
        _git_exec(repo, "push", "-u", "origin", "main")

        # Docs-only commit pushed without receipt -> allowed
        (repo / "NOTES.md").write_text("docs only\n", encoding="utf-8")
        _git_exec(repo, "add", "NOTES.md")
        _git_exec(repo, "commit", "-m", "docs")
        assert call_cmd("git push origin main", repo) == _allow()
        _git_exec(repo, "push", "origin", "main")
        checks.append("docs_only_push_allowed")

        # Dry-run and delete push -> allowed
        assert call_cmd("git push --dry-run origin main", repo) == _allow()
        assert call_cmd("git push origin :stale-branch", repo) == _allow()
        checks.append("dry_run_and_delete_allowed")

        # Create worktree with code change
        wt = root / "repo-wt"
        _git_exec(repo, "worktree", "add", "-b", "wt/feat", str(wt), "main")
        (wt / "app.py").write_text("print('hi')\n", encoding="utf-8")
        _git_exec(wt, "add", "app.py")
        _git_exec(wt, "commit", "-m", "add app")

        # Unreviewed merge --ff-only, push, pr-create, ship -> denied
        r_merge = call_cmd("git merge --ff-only wt/feat", repo)
        assert r_merge.get("decision") == "deny", r_merge
        r_push = call_cmd(f"cd {wt} && git push origin HEAD", repo)
        assert r_push.get("decision") == "deny", r_push
        r_pr = call_cmd("repotools pr-create --title t --body-file b", wt)
        assert r_pr.get("decision") == "deny", r_pr
        r_gh = call_cmd("gh pr create --fill", wt)
        assert r_gh.get("decision") == "deny", r_gh
        r_ship = call_cmd("mise run ship", wt)
        assert r_ship.get("decision") == "deny", r_ship
        checks.append("unreviewed_code_gates_denied")

        # Record receipt in wt -> allows merge --ff-only AND push of same SHA on main
        _record_receipt(wt, "HEAD", "self-check review")
        assert call_cmd("git merge --ff-only wt/feat", repo) == _allow()
        _git_exec(repo, "merge", "--ff-only", "wt/feat")
        assert call_cmd("git push origin main", repo) == _allow()
        checks.append("receipt_carries_across_ff_only_merge_to_push")

        # New code commit invalidates previous SHA receipt
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        _git_exec(repo, "add", "app.py")
        _git_exec(repo, "commit", "-m", "update app")
        r_stale = call_cmd("git push origin main", repo)
        assert r_stale.get("decision") == "deny", r_stale
        checks.append("new_commit_requires_new_receipt")

    if hooks_json_path is not None:
        _check_hooks_json(hooks_json_path)
        checks.append("hooks_json_registered")

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Code review PreToolUse guard")
    parser.add_argument(
        "--record", action="store_true", help="Record review receipt for target commit"
    )
    parser.add_argument(
        "--repo", default=".", help="Repository path for --record (default: .)"
    )
    parser.add_argument(
        "--ref", default="HEAD", help="Git ref to record (default: HEAD)"
    )
    parser.add_argument("--summary", default="", help="Short review summary note")
    parser.add_argument(
        "--self-check", action="store_true", help="Run built-in hook verification"
    )
    parser.add_argument(
        "--check-hooks-json", type=Path, help="Verify hooks.json registration"
    )
    args = parser.parse_args()

    if args.self_check:
        checks = _run_self_check(args.check_hooks_json)
        print(json.dumps({"ok": True, "checks": checks}))
        return 0

    if args.record:
        res = _record_receipt(Path(args.repo), args.ref, args.summary)
        print(json.dumps({"ok": True, **res}))
        return 0

    raw = sys.stdin.read()
    print(json.dumps(_process_stdin_text(raw)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
