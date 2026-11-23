#!/usr/bin/env python3
"""Antigravity PreToolUse & Stop guard enforcing /code-review (--record) -> post-review verification (--verify) -> observed-proof completion."""

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
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
DOC_SUFFIXES = frozenset({".md", ".mdx", ".markdown", ".rst", ".adoc", ".txt"})
EDIT_TOOLS = frozenset(
    {
        "edit_file",
        "write_file",
        "replace_file_content",
        "multi_replace_file_content",
        "write_to_file",
    }
)
RECEIPT_FILENAME = "agy-code-review-receipt.json"
VERIFY_STATE_FILENAME = ".agy-verify-gate-state.json"
MAX_STORED_RECEIPTS = 32
MAX_OUTPUT_TAIL = 800
MIN_PROOF_LEN = 15
MIN_EVIDENCE_LEN = 20
SEPARATORS = frozenset(
    {";", "&&", "||", "|", "&", "\n", "then", "do", "else", "elif", "{", "}", "(", ")"}
)
MUTATING_GIT_SUBS = frozenset(
    {"commit", "merge", "rebase", "cherry-pick", "am", "revert", "reset"}
)
NON_VERIFY_GIT_SUBS = frozenset(
    {
        "add",
        "status",
        "diff",
        "log",
        "show",
        "branch",
        "rev-parse",
        "commit",
        "merge",
        "push",
        "rebase",
        "cherry-pick",
        "stash",
        "reset",
        "checkout",
        "switch",
        "worktree",
    }
)
NON_VERIFY_UTILS = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "rg",
        "grep",
        "find",
        "which",
        "pwd",
        "echo",
        "printf",
        "mkdir",
        "rm",
        "cp",
        "mv",
        "chmod",
        "touch",
        "ln",
        "readlink",
        "stat",
        "wc",
        "sort",
        "uniq",
        "true",
        "false",
        "sleep",
    }
)
COMPLETION_CLAIM_RE = re.compile(
    r"\b(?:done|completed|complete|fixed|shipped|merged|pushed|implemented|resolved|ready|all\s+checks\s+passed|passed\s+all)\b",
    re.IGNORECASE,
)
BLOCKER_OR_QUESTION_RE = re.compile(
    r"\b(?:blocked|failed|failing|still\s+fails|cannot|unable\s+to|error|waiting\s+on)\b",
    re.IGNORECASE,
)
LEARN_FAST_EXIT_PREFIX = "post-task /learn: no reusable customization updates needed."
PROOF_METRIC_RE = re.compile(
    r"\b(?:\d+/\d+\s+(?:passed|pass|passing|checks?|tests?|ok|green|succeeded)|exit(?:\s+code)?\s+0|0\s+(?:failed|errors?|warnings?|diagnostics?)|old_pid|new_pid|pid\s+\d+|result[=:]\s*\"?ok)\b",
    re.IGNORECASE,
)
PROOF_TOOL_RE = re.compile(
    r"\b(?:unittest|pytest|cargo\s+test|mise\s+doctor|hk\s+check|ruff|ty\s+check|agnix|--self-check|roll:(?:restart|snapshot|self|verify)|shfmt|shellcheck)\b",
    re.IGNORECASE,
)
PROOF_OUTCOME_RE = re.compile(
    r"\b(?:passed|pass|verified|clean|green|succeeded|allowed|denied)\b",
    re.IGNORECASE,
)


def _make_stop_state(
    *,
    last_user_idx: int = -2,
    last_blocked_step_idx: int = -1,
    blocks_in_turn: int = 0,
) -> dict[str, int]:
    return {
        "last_user_idx": last_user_idx,
        "last_blocked_step_idx": last_blocked_step_idx,
        "blocks_in_turn": blocks_in_turn,
    }


def _allow() -> dict[str, Any]:
    return {"decision": "allow"}


def _deny(reason: str) -> dict[str, Any]:
    return {"decision": "deny", "reason": reason}


def _strip_inline_code(line: str) -> str:
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        if line[i] == "`":
            j = i
            while j < n and line[j] == "`":
                j += 1
            tick_len = j - i
            pos = j
            close_end = -1
            while pos < n:
                if line[pos] == "`":
                    k = pos
                    while k < n and line[k] == "`":
                        k += 1
                    if k - pos == tick_len:
                        close_end = k
                        break
                    pos = k
                else:
                    pos += 1
            if close_end != -1:
                out.append(" ")
                i = close_end
                continue
            out.append(line[i:j])
            i = j
        else:
            out.append(line[i])
            i += 1
    return "".join(out)


def _strip_fenced_code(text: str) -> str:
    kept: list[str] = []
    fence_char = ""
    fence_len = 0
    for line in text.splitlines():
        match = FENCE_RE.match(line.rstrip("\r"))
        if match:
            marker_run, rest = match.group(1), match.group(2)
            marker = marker_run[0]
            if not fence_char:
                if not (marker == "`" and "`" in rest):
                    fence_char = marker
                    fence_len = len(marker_run)
                    continue
            elif (
                marker == fence_char
                and len(marker_run) >= fence_len
                and rest.strip(" \t") == ""
            ):
                fence_char = ""
                fence_len = 0
                continue
        if not fence_char:
            kept.append(_strip_inline_code(line))
    return "\n".join(kept)


def _split_shell_segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";|&()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
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
        if ENV_ASSIGN_RE.match(tok):
            idx += 1
            continue
        if Path(tok).name == "env":
            idx += 1
            while idx < len(segment) and segment[idx].startswith("-"):
                flag = segment[idx]
                if flag in {
                    "-u",
                    "--unset",
                    "-C",
                    "--chdir",
                    "-S",
                    "--split-string",
                } and idx + 1 < len(segment):
                    idx += 2
                    continue
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
    except OSError, subprocess.SubprocessError:
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
    diff_op = ".." if base_ref is not None else "..."
    for candidate_base in bases:
        if not candidate_base:
            continue
        rc_b, base_sha = _git_output(repo_root, "rev-parse", "--verify", candidate_base)
        if rc_b != 0 or not base_sha:
            continue
        if base_sha == target_sha:
            return []
        rc_d, out = _git_output(
            repo_root, "diff", "--name-only", f"{base_sha}{diff_op}{target_sha}"
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


def _load_receipts(common_dir: Path) -> dict[str, dict[str, Any]]:
    path = _receipt_path(common_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    receipts = raw.get("receipts")
    if not isinstance(receipts, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in receipts.items():
        if isinstance(k, str) and isinstance(v, dict):
            commit_sha = v.get("commit_sha")
            tree_sha = v.get("tree_sha")
            if isinstance(commit_sha, str) and isinstance(tree_sha, str):
                entry: dict[str, Any] = {
                    "commit_sha": commit_sha,
                    "tree_sha": tree_sha,
                    "recorded_at": str(v.get("recorded_at", "")),
                    "summary": str(v.get("summary", "")),
                }
                verification = v.get("verification")
                if isinstance(verification, dict):
                    entry["verification"] = verification
                out[k] = entry
    return out


def _write_receipts(
    common_dir: Path, latest_sha: str, receipts: dict[str, dict[str, Any]]
) -> Path:
    keys = list(receipts.keys())
    if len(keys) > MAX_STORED_RECEIPTS:
        for old_key in keys[:-MAX_STORED_RECEIPTS]:
            receipts.pop(old_key, None)

    path = _receipt_path(common_dir)
    payload = {"latest": latest_sha, "receipts": receipts}
    data = json.dumps(payload, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{RECEIPT_FILENAME}.", dir=common_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def _has_valid_review_receipt(common_dir: Path, commit_sha: str, tree_sha: str) -> bool:
    entry = _load_receipts(common_dir).get(commit_sha)
    if not entry:
        return False
    return entry.get("commit_sha") == commit_sha and entry.get("tree_sha") == tree_sha


def _has_valid_verification_receipt(
    common_dir: Path, commit_sha: str, tree_sha: str
) -> bool:
    entry = _load_receipts(common_dir).get(commit_sha)
    if (
        not entry
        or entry.get("commit_sha") != commit_sha
        or entry.get("tree_sha") != tree_sha
    ):
        return False
    ver = entry.get("verification")
    if not isinstance(ver, dict):
        return False
    recorded_at = str(entry.get("recorded_at", ""))
    verified_at = ver.get("verified_at")
    if not isinstance(verified_at, str) or not verified_at:
        return False
    if recorded_at and verified_at < recorded_at:
        return False
    proof = ver.get("proof")
    if not isinstance(proof, str) or len(proof.strip()) < MIN_PROOF_LEN:
        return False
    commands = ver.get("commands")
    has_passing_cmd = isinstance(commands, list) and any(
        isinstance(item, dict) and item.get("exit_code") == 0 for item in commands
    )
    evidence = ver.get("evidence")
    has_evidence = (
        isinstance(evidence, str) and len(evidence.strip()) >= MIN_EVIDENCE_LEN
    )
    return has_passing_cmd or has_evidence


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
    # Stage 1 always clears any stale Stage 2 verification so verification must run AFTER review.
    receipts[commit_sha] = {
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": summary.strip(),
    }
    path = _write_receipts(common_dir, commit_sha, receipts)
    return {
        "repo_root": str(repo_root),
        "receipt_file": str(path),
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
    }


def _verify_receipt(
    repo: Path,
    ref: str,
    *,
    runs: list[str],
    evidence: str,
    proof: str,
    timeout: int = 300,
) -> dict[str, Any]:
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

    if not _has_valid_review_receipt(common_dir, commit_sha, tree_sha):
        msg = (
            f"Stage 1 /code-review receipt missing for {commit_sha[:7]} in {repo_root}; "
            "run /code-review:code-review and --record BEFORE --verify."
        )
        raise RuntimeError(msg)

    rc_st, tracked_dirty = _git_output(
        repo_root, "status", "--porcelain", "--untracked-files=no"
    )
    if rc_st != 0 or tracked_dirty:
        msg = f"Tracked working-tree changes present in {repo_root}; commit clean changes before --verify."
        raise RuntimeError(msg)

    clean_proof = proof.strip()
    if len(clean_proof) < MIN_PROOF_LEN:
        msg = f"--proof must be at least {MIN_PROOF_LEN} non-whitespace characters describing observed results."
        raise RuntimeError(msg)

    clean_runs = [cmd.strip() for cmd in runs if cmd.strip()]
    clean_evidence = evidence.strip()
    if not clean_runs and len(clean_evidence) < MIN_EVIDENCE_LEN:
        msg = (
            f"--verify requires at least one --run command or --evidence (>= {MIN_EVIDENCE_LEN} chars "
            "for live/external proof)."
        )
        raise RuntimeError(msg)

    initial_receipts = _load_receipts(common_dir)
    initial_recorded_at = str(
        (initial_receipts.get(commit_sha) or {}).get("recorded_at", "")
    )

    cmd_results: list[dict[str, Any]] = []
    for cmd in clean_runs:
        try:
            proc = subprocess.run(
                ["bash", "-c", cmd],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=max(1, timeout),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"Verification command timed out after {timeout}s: {cmd}"
            raise RuntimeError(msg) from exc
        combined = "\n".join(
            part for part in (proc.stdout.strip(), proc.stderr.strip()) if part
        )
        tail = combined[-MAX_OUTPUT_TAIL:]
        if proc.returncode != 0:
            msg = f"Verification command failed (exit {proc.returncode}): {cmd}\n{tail}".strip()
            raise RuntimeError(msg)
        cmd_results.append(
            {
                "cmd": cmd,
                "exit_code": 0,
                "output": tail,
            }
        )

    rc_st_after, dirty_after = _git_output(
        repo_root, "status", "--porcelain", "--untracked-files=no"
    )
    if rc_st_after != 0 or dirty_after:
        msg = f"Verification modified tracked files in {repo_root}; working tree must stay clean after --run."
        raise RuntimeError(msg)

    receipts = _load_receipts(common_dir)
    cur_entry = receipts.get(commit_sha)
    if (
        not cur_entry
        or cur_entry.get("tree_sha") != tree_sha
        or str(cur_entry.get("recorded_at", "")) != initial_recorded_at
    ):
        msg = (
            f"Stage 1 review receipt for {commit_sha[:7]} changed while verification ran; "
            "re-run --verify against the updated review receipt."
        )
        raise RuntimeError(msg)
    entry = dict(cur_entry)
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    recorded_at = str(entry.get("recorded_at", ""))
    verified_at = max(now_iso, recorded_at)
    entry["verification"] = {
        "verified_at": verified_at,
        "proof": clean_proof,
        "commands": cmd_results,
        "evidence": clean_evidence,
    }
    receipts[commit_sha] = entry
    path = _write_receipts(common_dir, commit_sha, receipts)
    return {
        "repo_root": str(repo_root),
        "receipt_file": str(path),
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "verified": True,
        "commands": len(cmd_results),
        "proof": clean_proof,
    }


def _push_source_refs(refspecs: list[str]) -> list[str]:
    if not refspecs:
        return ["HEAD"]
    refs: list[str] = []
    for spec in refspecs:
        if spec.startswith(":"):
            continue
        src = spec.lstrip("+").split(":", 1)[0]
        refs.append(src or "HEAD")
    return refs


def _parse_git_segment(
    tokens: list[str], cwd: Path
) -> tuple[str, Path, list[str], str | None, bool] | None:
    """Return (op, repo_dir, refs_to_check, base_ref_override, is_mutator) or None."""
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
        source_refs = _push_source_refs(positionals[1:])
        if not source_refs:
            return None
        return "git push", repo_dir, source_refs, None, False

    if sub == "merge":
        if any(tok in {"--abort", "--continue", "--quit", "--dry-run"} for tok in rest):
            return None
        has_ff_only = "--ff-only" in rest
        merge_refs: list[str] = []
        idx = 0
        while idx < len(rest):
            tok = rest[idx]
            if tok == "--ff-only":
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
        if has_ff_only and merge_refs:
            rc_b, branch = _git_output(
                repo_dir, "symbolic-ref", "--quiet", "--short", "HEAD"
            )
            if rc_b == 0 and branch in {"main", "master"}:
                return (
                    f"git merge --ff-only {merge_refs[0]}",
                    repo_dir,
                    [merge_refs[0]],
                    "HEAD",
                    False,
                )
        return "git merge", repo_dir, [], None, True

    if sub in MUTATING_GIT_SUBS:
        if any(tok in {"--dry-run", "--abort", "--quit", "-n"} for tok in rest):
            return None
        return f"git {sub}", repo_dir, [], None, True

    return None


def _parse_mise_or_pr_segment(
    tokens: list[str], cwd: Path
) -> tuple[str, Path, list[str], str | None, bool] | None:
    if not tokens:
        return None
    exe = Path(tokens[0]).name
    if exe == "repotools" and len(tokens) >= 2 and tokens[1] == "pr-create":
        return "repotools pr-create", cwd, ["HEAD"], None, False
    if exe == "gh" and len(tokens) >= 3 and tokens[1] == "pr" and tokens[2] == "create":
        return "gh pr create", cwd, ["HEAD"], None, False
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
            if tok.startswith("--cd="):
                raw_dir = Path(os.path.expanduser(tok.split("=", 1)[1]))
                repo_dir = raw_dir if raw_dir.is_absolute() else (cwd / raw_dir)
                i += 1
                continue
            if tok.startswith("-C") and len(tok) > 2:
                raw_dir = Path(os.path.expanduser(tok[2:]))
                repo_dir = raw_dir if raw_dir.is_absolute() else (cwd / raw_dir)
                i += 1
                continue
            if tok in {"-E", "--env", "-j", "--jobs"} and i + 1 < len(tokens):
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
                return f"mise run {tokens[j]}", repo_dir, ["HEAD"], None, False
    return None


def _check_command(command: str, initial_cwd: Path | None = None) -> dict[str, Any]:
    cwd = (initial_cwd or Path.cwd()).resolve(strict=False)
    mutated_repos: set[Path] = set()
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

        op_label, repo_candidate, target_refs, base_override, is_mutator = parsed
        resolved = _resolve_repo_dirs(repo_candidate)
        if resolved is None:
            continue
        repo_root, common_dir = resolved

        if not target_refs:
            if is_mutator:
                mutated_repos.add(repo_root)
            continue

        for target_ref in target_refs:
            if repo_root in mutated_repos:
                return _deny(
                    f"code-review-guard (code-review-protocol.md): '{op_label}' follows an earlier "
                    f"commit/history mutation in the same command for {repo_root}. Commit first, run "
                    "/code-review:code-review (--record), then run post-review verification (--verify) "
                    "before pushing or creating a PR."
                )
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

            if not _has_valid_review_receipt(common_dir, target_sha, target_tree_sha):
                return _deny(
                    f"code-review-guard (code-review-protocol.md): '{op_label}' requires Stage 1 "
                    f"/code-review:code-review (+ code-review-commons) receipt for {target_sha[:7]} "
                    f"in {repo_root}. Run review, fix CRITICAL/HIGH/MEDIUM findings, record with:\n"
                    f'python3 -I -B ~/.gemini/config/hooks/agy-code-review-guard.py --record --repo "{repo_root}" --summary "<summary>"\n'
                    "then run Stage 2 --verify before shipping."
                )

            if not _has_valid_verification_receipt(
                common_dir, target_sha, target_tree_sha
            ):
                return _deny(
                    f"code-review-guard (code-review-protocol.md): '{op_label}' has a review receipt for "
                    f"{target_sha[:7]} in {repo_root}, but lacks Stage 2 post-review verification! "
                    "Run concrete behavioral/test verification AFTER code review and record proof with:\n"
                    f'python3 -I -B ~/.gemini/config/hooks/agy-code-review-guard.py --verify --repo "{repo_root}" --run "<verify-cmd>" --proof "<observed result>"'
                )

        if is_mutator:
            mutated_repos.add(repo_root)
    return _allow()


def _resolve_target_path(target: str, base_cwd: Path) -> Path:
    raw = Path(os.path.expanduser(target))
    return (raw if raw.is_absolute() else (base_cwd / raw)).resolve(strict=False)


def _is_non_doc_repo_edit(
    target: str, artifact_dir: Path | None, base_cwd: Path
) -> bool:
    if not target:
        return False
    p = _resolve_target_path(target, base_cwd)
    if p.suffix.lower() in DOC_SUFFIXES:
        return False
    norm = str(p).replace("\\", "/")
    if "/.planning/" in norm or norm.startswith(".planning/"):
        return False
    if artifact_dir is not None:
        try:
            if p.resolve(strict=False).is_relative_to(
                artifact_dir.resolve(strict=False)
            ):
                return False
        except OSError:
            pass
    return True


def _is_verification_command(command: str) -> bool:
    segments = _split_shell_segments(command)
    for seg in segments:
        tokens = _strip_env_prefix(seg)
        if not tokens:
            continue
        exe = Path(tokens[0]).name
        if exe == "cd" or exe in NON_VERIFY_UTILS:
            continue
        if (
            exe in {"python", "python3"}
            and any(Path(tok).name == "agy-code-review-guard.py" for tok in tokens[1:])
            and "--verify" not in tokens
            and "--self-check" not in tokens
        ):
            # Recording Stage 1 review or running hooks-json lint alone is not post-edit verification.
            continue
        if exe == "git":
            idx = 1
            while idx < len(tokens):
                tok = tokens[idx]
                if tok in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
                    idx += 2
                    continue
                if tok.startswith("-"):
                    idx += 1
                    continue
                break
            sub = tokens[idx] if idx < len(tokens) else ""
            if sub in NON_VERIFY_GIT_SUBS:
                continue
        return True
    return False


def _has_observed_proof_cues(text: str) -> bool:
    stripped = _strip_fenced_code(text)
    if PROOF_METRIC_RE.search(stripped):
        return True
    has_tool = bool(PROOF_TOOL_RE.search(_strip_inline_code_keep_text(text)))
    has_outcome = bool(PROOF_OUTCOME_RE.search(stripped))
    return has_tool and has_outcome


def _strip_inline_code_keep_text(text: str) -> str:
    kept: list[str] = []
    fence_char = ""
    fence_len = 0
    for line in text.splitlines():
        match = FENCE_RE.match(line.rstrip("\r"))
        if match:
            marker_run, rest = match.group(1), match.group(2)
            marker = marker_run[0]
            if not fence_char:
                if not (marker == "`" and "`" in rest):
                    fence_char = marker
                    fence_len = len(marker_run)
                    continue
            elif (
                marker == fence_char
                and len(marker_run) >= fence_len
                and rest.strip(" \t") == ""
            ):
                fence_char = ""
                fence_len = 0
                continue
        if not fence_char:
            kept.append(line)
    return "\n".join(kept)


def _looks_like_completion_claim(text: str) -> bool:
    stripped = _strip_fenced_code(text).strip()
    if not stripped:
        return False
    if stripped.lower().startswith(LEARN_FAST_EXIT_PREFIX):
        return False
    if BLOCKER_OR_QUESTION_RE.search(stripped) or stripped.endswith("?"):
        return False
    return bool(COMPLETION_CLAIM_RE.search(stripped))


def _unquote_truncated(val: Any) -> str:
    if not isinstance(val, str):
        return ""
    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
        try:
            decoded = json.loads(val)
            if isinstance(decoded, str):
                return decoded
        except json.JSONDecodeError:
            pass
    if val.startswith('"') and not val.endswith('"'):
        return val[1:]
    return val


def _read_stop_state(path: Path) -> dict[str, int]:
    out = _make_stop_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError, TypeError, json.JSONDecodeError:
        return out
    if not isinstance(raw, dict):
        return out
    for key in ("last_user_idx", "last_blocked_step_idx", "blocks_in_turn"):
        val = raw.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            out[key] = val
    return out


def _write_stop_state(path: Path, state: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state) + "\n")
        os.replace(tmp_name, path)
        replaced = True
    except OSError:
        return
    finally:
        if not replaced:
            Path(tmp_name).unlink(missing_ok=True)


def _load_full_records(
    transcript_path: Path, needed_indices: set[int]
) -> dict[int, dict[str, Any]]:
    if not needed_indices:
        return {}
    full_path = transcript_path.with_name("transcript_full.jsonl")
    if not full_path.is_file():
        return {}
    out: dict[int, dict[str, Any]] = {}
    max_idx = max(needed_indices)
    try:
        with full_path.open("r", encoding="utf-8", errors="replace") as handle:
            for idx, raw in enumerate(handle):
                if idx > max_idx:
                    break
                if idx not in needed_indices:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out[idx] = obj
    except OSError:
        return {}
    return out


def _run_stop_hook(payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_CODE_REVIEW_GUARD") == "1":
        return {}
    if payload.get("terminationReason") != "NO_TOOL_CALL" or not payload.get(
        "fullyIdle"
    ):
        return {}
    transcript_raw = payload.get("transcriptPath")
    if not isinstance(transcript_raw, str) or not transcript_raw.strip():
        return {}
    transcript_path = Path(os.path.expanduser(transcript_raw))
    if not transcript_path.is_file():
        return {}
    try:
        lines = transcript_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return {}

    artifact_raw = payload.get("artifactDirectoryPath")
    artifact_dir = (
        Path(os.path.expanduser(artifact_raw))
        if isinstance(artifact_raw, str) and artifact_raw.strip()
        else None
    )
    payload_cwd_raw = payload.get("cwd")
    base_cwd = (
        Path(os.path.expanduser(payload_cwd_raw)).resolve(strict=False)
        if isinstance(payload_cwd_raw, str) and payload_cwd_raw.strip()
        else Path.cwd().resolve(strict=False)
    )

    last_user_idx = -1
    entries: list[tuple[int, dict[str, Any]]] = []
    for idx, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "USER_INPUT":
            last_user_idx = idx
            entries = []
            continue
        entries.append((idx, obj))

    if not entries:
        return {}

    needed_full = {
        line_idx
        for line_idx, obj in entries
        if isinstance(obj.get("truncated_fields"), list)
        and (
            "tool_calls" in obj["truncated_fields"]
            or "content" in obj["truncated_fields"]
        )
    }
    full_records = _load_full_records(transcript_path, needed_full)

    final_planner: tuple[int, dict[str, Any]] | None = None
    last_edit_pos: tuple[int, int] | None = None
    last_verify_pos: tuple[int, int] | None = None
    touched_repos: set[Path] = set()
    committed_non_doc_repos: set[Path] = set()

    for line_idx, raw_obj in entries:
        obj = full_records.get(line_idx, raw_obj)
        if obj.get("type") != "PLANNER_RESPONSE":
            continue
        final_planner = (line_idx, obj)
        tool_calls = obj.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for tc_idx, tc in enumerate(tool_calls):
            if not isinstance(tc, dict):
                continue
            tname = tc.get("name")
            args = tc.get("args")
            if not isinstance(args, dict):
                continue
            pos = (line_idx, tc_idx)
            if tname in EDIT_TOOLS:
                target = _unquote_truncated(
                    args.get("Path") or args.get("TargetFile") or ""
                )
                if _is_non_doc_repo_edit(target, artifact_dir, base_cwd):
                    last_edit_pos = pos
                    resolved = _resolve_repo_dirs(
                        _resolve_target_path(target, base_cwd)
                    )
                    if resolved is not None:
                        touched_repos.add(resolved[0])
            elif tname == "run_command":
                cmd = _unquote_truncated(args.get("CommandLine"))
                if not cmd.strip():
                    continue
                cwd_str = _unquote_truncated(args.get("Cwd")) or ""
                cmd_cwd = (
                    _resolve_target_path(cwd_str, base_cwd)
                    if cwd_str.strip()
                    else base_cwd
                )
                seg_cwd = cmd_cwd
                for seg in _split_shell_segments(cmd):
                    tokens = _strip_env_prefix(seg)
                    if not tokens:
                        continue
                    if (
                        tokens[0] == "cd"
                        and len(tokens) >= 2
                        and not tokens[1].startswith("-")
                    ):
                        seg_cwd = _resolve_target_path(tokens[1], seg_cwd)
                        continue
                    parsed = _parse_git_segment(tokens, seg_cwd)
                    if parsed and parsed[4]:
                        resolved = _resolve_repo_dirs(parsed[1])
                        if resolved is not None:
                            committed_non_doc_repos.add(resolved[0])
                if _is_verification_command(cmd):
                    last_verify_pos = pos

    if last_edit_pos is None and not committed_non_doc_repos:
        return {}
    if final_planner is None:
        return {}
    _, final_obj = final_planner
    final_tools = final_obj.get("tool_calls") or []
    if isinstance(final_tools, list) and final_tools:
        return {}
    last_resp_text = final_obj.get("content")
    if not isinstance(last_resp_text, str) or not last_resp_text.strip():
        return {}
    step_val = final_obj.get("step_index")
    last_resp_step = (
        step_val
        if isinstance(step_val, int) and not isinstance(step_val, bool)
        else final_planner[0]
    )

    if not _looks_like_completion_claim(last_resp_text):
        return {}

    reasons: list[str] = []
    if last_edit_pos is not None and (
        last_verify_pos is None or last_verify_pos < last_edit_pos
    ):
        reasons.append(
            "Non-documentation files were edited in this turn with no verification command run after the final edit."
        )

    for repo_root in sorted(touched_repos | committed_non_doc_repos):
        resolved = _resolve_repo_dirs(repo_root)
        if resolved is None:
            continue
        r_root, common_dir = resolved
        target = _resolve_target_commit(r_root, "HEAD")
        if target is None:
            continue
        sha, tree_sha = target
        changed = _changed_files(r_root, sha)
        if changed and not all(Path(p).suffix.lower() in DOC_SUFFIXES for p in changed):
            if not _has_valid_review_receipt(common_dir, sha, tree_sha):
                reasons.append(
                    f"Unpushed commit {sha[:7]} in {r_root} has non-doc changes without Stage 1 --record."
                )
            elif not _has_valid_verification_receipt(common_dir, sha, tree_sha):
                reasons.append(
                    f"Unpushed commit {sha[:7]} in {r_root} lacks Stage 2 post-review `--verify --run ... --proof ...` receipt."
                )

    if not _has_observed_proof_cues(last_resp_text):
        reasons.append(
            "Final response claims completion without citing concrete observed verification proof (passing test/command counts, exit 0, or runtime output)."
        )

    if not reasons:
        return {}

    state_base = artifact_dir if artifact_dir is not None else transcript_path.parent
    state_path = state_base / VERIFY_STATE_FILENAME
    state = _read_stop_state(state_path)
    if state["last_user_idx"] != last_user_idx:
        state = _make_stop_state(
            last_user_idx=last_user_idx,
            last_blocked_step_idx=-1,
            blocks_in_turn=0,
        )
    if state["last_blocked_step_idx"] == last_resp_step or state["blocks_in_turn"] >= 2:
        return {}

    _write_stop_state(
        state_path,
        _make_stop_state(
            last_user_idx=last_user_idx,
            last_blocked_step_idx=last_resp_step,
            blocks_in_turn=state["blocks_in_turn"] + 1,
        ),
    )
    return {
        "decision": "continue",
        "reason": "Verification gate (code-review-protocol.md): "
        + " ".join(reasons)
        + " Run behavioral/runtime verification, run `--verify --run ... --proof ...` for committed changes, and cite observed output before claiming completion.",
    }


def _run_hook(payload: dict[str, Any], event: str = "auto") -> dict[str, Any]:
    if event == "stop" or (
        event == "auto"
        and "toolCall" not in payload
        and "tool_input" not in payload
        and ("terminationReason" in payload or "transcriptPath" in payload)
    ):
        return _run_stop_hook(payload)

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


def _process_stdin_text(raw_stdin: str, event: str = "auto") -> dict[str, Any]:
    fallback = {} if event == "stop" else _allow()
    try:
        payload = json.loads(raw_stdin)
    except json.JSONDecodeError, ValueError:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    return _run_hook(payload, event=event)


def _check_hooks_json(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "agy-code-review-guard" not in data:
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
        raise AssertionError(f"unexpected PreToolUse hook command: {cmd!r}")
    if cmd.startswith("/") or "/Users/" in cmd:
        raise AssertionError(
            f"PreToolUse hook command must be portable relative path, got: {cmd!r}"
        )

    stop_handlers = block.get("Stop")
    if not isinstance(stop_handlers, list) or not stop_handlers:
        raise AssertionError(
            f"missing Stop handlers for agy-code-review-guard in {path}"
        )
    stop_cmd = (
        stop_handlers[0].get("command", "")
        if isinstance(stop_handlers[0], dict)
        else ""
    )
    if (
        not isinstance(stop_cmd, str)
        or "agy-code-review-guard.py" not in stop_cmd
        or "--event stop" not in stop_cmd
    ):
        raise AssertionError(f"unexpected Stop hook command: {stop_cmd!r}")
    if stop_cmd.startswith("/") or "/Users/" in stop_cmd:
        raise AssertionError(
            f"Stop hook command must be portable relative path, got: {stop_cmd!r}"
        )


def _local_git_env_vars() -> tuple[str, ...]:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return tuple(
                line.strip() for line in proc.stdout.splitlines() if line.strip()
            )
    except OSError, subprocess.SubprocessError:
        pass
    return (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_COMMON_DIR",
    )


def _git_exec(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    for var_name in _local_git_env_vars():
        env.pop(var_name, None)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "HK": "0",
        }
    )
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _run_self_check(hooks_json_path: Path | None) -> list[str]:
    """Fast (<50ms) zero-git-subprocess static and parser self-check for `mise doctor project`."""
    checks: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        non_git = Path(tmp) / "non_git"
        non_git.mkdir()
        assert (
            _run_hook(
                {
                    "toolCall": {
                        "name": "run_command",
                        "args": {
                            "CommandLine": "git push origin main",
                            "Cwd": str(non_git),
                        },
                    }
                },
                event="pre-tool-use",
            )
            == _allow()
        )
        checks.append("non_git_allowed")

    assert _split_shell_segments("cd /tmp && env -u GIT_DIR git push origin HEAD") == [
        ["cd", "/tmp"],
        ["env", "-u", "GIT_DIR", "git", "push", "origin", "HEAD"],
    ]
    assert _strip_env_prefix(["FOO=1", "env", "-u", "BAR", "git", "push"]) == [
        "git",
        "push",
    ]
    assert (
        _parse_git_segment(["git", "push", "--dry-run", "origin", "main"], Path.cwd())
        is None
    )
    assert _parse_git_segment(["git", "push", "origin", ":stale"], Path.cwd()) is None
    parsed_push = _parse_git_segment(
        ["git", "-C", ".", "push", "origin", "HEAD"], Path.cwd()
    )
    assert parsed_push is not None and parsed_push[0] == "git push"
    parsed_mise = _parse_mise_or_pr_segment(["mise", "run", "ship"], Path.cwd())
    assert parsed_mise is not None and parsed_mise[0] == "mise run ship"
    checks.append("command_parsers_ok")

    assert not _is_non_doc_repo_edit("README.md", None, Path.cwd())
    assert not _is_non_doc_repo_edit(".planning/demo/task_plan.md", None, Path.cwd())
    assert _is_non_doc_repo_edit("tasks/py/check.py", None, Path.cwd())
    assert not _is_verification_command(
        "ls -la && python3 -I -B ~/.gemini/config/hooks/agy-code-review-guard.py --record --summary x"
    )
    assert _is_verification_command("pytest -n auto")
    assert _has_observed_proof_cues("27/27 passed with pytest exit code 0")
    assert not _has_observed_proof_cues("Done on 11/23 cleanly")
    assert _looks_like_completion_claim("Done! Fixed app.py.")
    assert not _looks_like_completion_claim(
        "I implemented the draft, but pytest failed and I am blocked."
    )
    assert _unquote_truncated('"app.py"') == "app.py"
    assert _process_stdin_text("{bad json", event="pre-tool-use") == _allow()
    assert _process_stdin_text("{bad json", event="stop") == {}
    checks.append("stop_classifiers_ok")

    if hooks_json_path is not None:
        _check_hooks_json(hooks_json_path)
        checks.append("hooks_json_registered")

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Code review + post-review verification PreToolUse & Stop guard"
    )
    parser.add_argument(
        "--event",
        choices=("auto", "pre-tool-use", "stop"),
        default="auto",
        help="Hook lifecycle event mode when reading stdin",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Stage 1: Record /code-review receipt for target commit",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Stage 2: Run post-review verification commands/evidence and record proof receipt",
    )
    parser.add_argument(
        "--run",
        action="append",
        default=[],
        help="Shell verification command to run in --verify mode (repeatable)",
    )
    parser.add_argument(
        "--evidence",
        default="",
        help="Supplemental verified live/external output evidence for --verify when no single command captures it",
    )
    parser.add_argument(
        "--proof",
        default="",
        help="Concise summary of concrete observed verification proof (required for --verify)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-command timeout seconds for --verify (default: 300)",
    )
    parser.add_argument(
        "--repo", default=".", help="Repository path for --record/--verify (default: .)"
    )
    parser.add_argument(
        "--ref", default="HEAD", help="Git ref for --record/--verify (default: HEAD)"
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
        sys.stdout.write(json.dumps({"ok": True, "checks": checks}) + "\n")
        return 0

    if args.record and args.verify:
        sys.stderr.write(
            json.dumps({"ok": False, "error": "Pass --record first, then --verify."})
            + "\n"
        )
        return 1

    if args.record:
        res = _record_receipt(Path(args.repo), args.ref, args.summary)
        sys.stdout.write(json.dumps({"ok": True, **res}) + "\n")
        return 0

    if args.verify:
        try:
            res = _verify_receipt(
                Path(args.repo),
                args.ref,
                runs=args.run,
                evidence=args.evidence,
                proof=args.proof,
                timeout=args.timeout,
            )
        except RuntimeError as exc:
            sys.stderr.write(json.dumps({"ok": False, "error": str(exc)}) + "\n")
            return 1
        sys.stdout.write(json.dumps({"ok": True, **res}) + "\n")
        return 0

    raw = sys.stdin.read()
    sys.stdout.write(json.dumps(_process_stdin_text(raw, event=args.event)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
