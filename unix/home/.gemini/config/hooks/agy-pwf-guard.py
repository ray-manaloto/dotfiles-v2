#!/usr/bin/env python3
"""Antigravity PreToolUse guard requiring an unambiguous active PWF plan in PWF-adopted repos."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EDIT_TOOLS = frozenset(
    {
        "edit_file",
        "write_file",
        "replace_file_content",
        "multi_replace_file_content",
        "write_to_file",
    }
)
SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PLAN_ID_LINE_RE = re.compile(r'^\s*PLAN_ID\s*=\s*"([^"\r\n]+)"')
GITHUB_ISSUE_LINE_RE = re.compile(
    r'^\s*GITHUB_ISSUE\s*=\s*"?([^"#\r\n]*#?[0-9]+|[^"\r\n]*)"?'
)
GITHUB_ISSUE_URL_LINE_RE = re.compile(r'^\s*GITHUB_ISSUE_URL\s*=\s*"([^"\r\n]+)"')
ISSUE_NUM_RE = re.compile(r"^#?([1-9][0-9]*)$")
TASK_PLAN_ISSUE_RE = re.compile(r"^#\s*Task Plan:\s*(?:.*?\s)?#([1-9][0-9]*)(?:\b|$)")
ISSUE_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/issues/([1-9][0-9]*)\b"
)
NS_TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}\.\d{9}[+-]\d{4}$")
DOCS_BYPASS_FILES = frozenset({"ROADMAP.md", "README.md", "CHANGELOG.md"})
DOCS_BYPASS_SUFFIXES = frozenset({".md", ".mdx", ".markdown", ".rst", ".adoc", ".txt"})
WORKTREE_RM_RE = re.compile(
    r"\bmise\s+(?:(?:-C|--cd)\s+\S+\s+)?run\s+worktree:rm\b([^\n;&|]*)"
)
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
BLOCKQUOTE_RE = re.compile(r"^ ? ? ?>")
MALFORMED_PHASE_RE = re.compile(r"^ ? ? ?### Phase([0-9:]|-[0-9])")
PHASE_HEADING_RE = re.compile(r"^ ? ? ?### Phase([ \t]|$)")
ANY_HEADING_RE = re.compile(r"^ ? ? ?(###|##|#)([ \t]|$)")
STATUS_TOKEN_RE = re.compile(
    r"\*\*Status:\*\*[ \t]+(pending|in_progress|complete)(?![A-Za-z0-9_])"
    r"|\[(pending|in_progress|complete)\]"
)
DENY_REASON = (
    "PWF guard: this repository enforces planning-with-files (.mode present), "
    "but no unambiguous active PWF plan was resolved. Initialize and attest "
    ".planning/<PLAN_ID>/task_plan.md (and pin PLAN_ID in mise.local.toml or use "
    "'mise run worktree:new <slug> --issue <N>') before editing repository files."
)
COMPLETE_DENY_REASON = (
    "PWF guard: the active planning-with-files plan has all phases marked "
    "complete. Start a new PLAN_ID (e.g. 'mise run worktree:new <slug> --issue <N>' or a "
    "new plan in mise.local.toml) or mark the next phase 'in_progress' before "
    "editing tracked repository files."
)
ISSUE_DENY_REASON = (
    "PWF guard: active repo edits require GitHub issue-linked metadata "
    "(GITHUB_ISSUE in mise.local.toml, or '# Task Plan: #<N>' / "
    "'## Goal: Implement https://github.com/.../issues/<N>' in task_plan.md). "
    "Use 'mise run worktree:new <slug> --issue <N>' (only ROADMAP.md / docs/** "
    "with an active plan are exempt)."
)


def _allow() -> dict[str, Any]:
    return {"decision": "allow"}


def _deny(reason: str = DENY_REASON) -> dict[str, Any]:
    return {"decision": "deny", "reason": reason}


def _resolve_target_path(args: Any, base_dir: Path | None) -> Path | None:
    if not isinstance(args, dict):
        return None
    for key in ("Path", "TargetFile", "path", "target_file"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            candidate = Path(os.path.expanduser(val.strip()))
            if not candidate.is_absolute() and base_dir is not None:
                candidate = base_dir / candidate
            try:
                return candidate.resolve(strict=False)
            except OSError:
                return candidate
    return None


def _is_within_dir(candidate: Path, parent: Path) -> bool:
    try:
        return candidate.resolve(strict=False).is_relative_to(
            parent.resolve(strict=False)
        )
    except OSError:
        return False


def _find_git_repo_root(target: Path) -> Path | None:
    start = target if target.is_dir() else target.parent
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def _read_plan_id_from_mise_local(repo_root: Path) -> tuple[bool, str]:
    local_cfg = repo_root / "mise.local.toml"
    if not local_cfg.is_file():
        return False, ""
    try:
        text = local_cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True, ""

    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        tomllib = None

    if tomllib is not None:
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return True, ""
        if isinstance(parsed, dict):
            env_tbl = parsed.get("env")
            if isinstance(env_tbl, dict):
                val = env_tbl.get("PLAN_ID")
                if isinstance(val, str):
                    return True, val.strip()
        return False, ""

    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        match = PLAN_ID_LINE_RE.match(stripped)
        if match:
            return True, match.group(1).strip()
    return False, ""


def _format_ns_timestamp(ns: int | None = None) -> str:
    raw_ns = time.time_ns() if ns is None else ns
    sec, rem = divmod(raw_ns, 1_000_000_000)
    dt = datetime.fromtimestamp(sec, timezone.utc).astimezone()
    return f"{dt:%Y%m%dT%H%M%S}.{rem:09d}{dt:%z}"


def _valid_slug(slug: str) -> bool:
    return bool(slug and SAFE_SLUG_RE.match(slug) and slug not in (".", ".."))


def _read_issue_from_mise_local(repo_root: Path) -> tuple[bool, str]:
    local_cfg = repo_root / "mise.local.toml"
    if not local_cfg.is_file():
        return False, ""
    try:
        text = local_cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True, ""

    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        tomllib = None

    raw_issue: Any = None
    raw_url: Any = None
    if tomllib is not None:
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return True, ""
        if isinstance(parsed, dict):
            env_tbl = parsed.get("env")
            if isinstance(env_tbl, dict):
                if "GITHUB_ISSUE" in env_tbl:
                    raw_issue = env_tbl.get("GITHUB_ISSUE")
                if "GITHUB_ISSUE_URL" in env_tbl:
                    raw_url = env_tbl.get("GITHUB_ISSUE_URL")
    else:
        for line in text.splitlines():
            in_quotes = False
            escaped = False
            cut = len(line)
            for idx, ch in enumerate(line):
                if escaped:
                    escaped = False
                    continue
                if ch == "\\" and in_quotes:
                    escaped = True
                    continue
                if ch == '"':
                    in_quotes = not in_quotes
                elif ch == "#" and not in_quotes:
                    cut = idx
                    break
            stripped = line[:cut]
            m_issue = GITHUB_ISSUE_LINE_RE.match(stripped)
            if m_issue:
                raw_issue = m_issue.group(1)
            m_url = GITHUB_ISSUE_URL_LINE_RE.match(stripped)
            if m_url:
                raw_url = m_url.group(1)

    has_any = raw_issue is not None or raw_url is not None
    if not has_any:
        return False, ""

    issue_num = ""
    if raw_issue is not None:
        if isinstance(raw_issue, bool):
            return True, ""
        if isinstance(raw_issue, int):
            if raw_issue <= 0:
                return True, ""
            issue_num = str(raw_issue)
        elif isinstance(raw_issue, str):
            m = ISSUE_NUM_RE.match(raw_issue.strip())
            if not m:
                return True, ""
            issue_num = m.group(1)
        else:
            return True, ""

    url_num = ""
    if raw_url is not None:
        if not isinstance(raw_url, str):
            return True, ""
        m_url = ISSUE_URL_RE.search(raw_url.strip())
        if not m_url:
            return True, ""
        url_num = m_url.group(1)

    if issue_num and url_num and issue_num != url_num:
        return True, ""
    return True, issue_num or url_num


def _extract_task_plan_issue(plan_path: Path) -> tuple[bool, str]:
    try:
        lines = plan_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False, ""
    if lines and lines[0].startswith("\ufeff"):
        lines[0] = lines[0][1:]

    heading_num = ""
    goal_num = ""
    in_goal = False
    for raw in lines:
        line = raw.rstrip("\r")
        if line.startswith("# "):
            in_goal = False
            if not heading_num:
                m_head = TASK_PLAN_ISSUE_RE.match(line)
                if m_head:
                    heading_num = m_head.group(1)
            continue
        if re.match(r"^##\s+Goal\s*$", line):
            in_goal = True
            continue
        if in_goal and re.match(r"^##?\s+", line):
            in_goal = False
        if in_goal and not goal_num:
            m_goal = ISSUE_URL_RE.search(line)
            if m_goal:
                goal_num = m_goal.group(1)

    if heading_num and goal_num and heading_num != goal_num:
        return True, ""
    if heading_num:
        return True, heading_num
    if goal_num:
        return True, goal_num
    return False, ""


def _parse_head_branch(head_path: Path) -> str:
    try:
        raw = head_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "unknown"
    if raw.startswith("ref:"):
        ref = raw[4:].strip()
        if ref.startswith("refs/heads/"):
            return ref[len("refs/heads/") :]
        return ref or "unknown"
    return "detached" if raw else "unknown"


def _check_linked_worktree(repo_root: Path) -> tuple[bool, str]:
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        head_file = dot_git / "HEAD"
        branch = _parse_head_branch(head_file) if head_file.is_file() else "main"
        return False, branch
    if dot_git.is_file():
        try:
            raw = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return False, "unknown"
        if not raw.lower().startswith("gitdir:"):
            return False, "unknown"
        gitdir = Path(raw.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = (repo_root / gitdir).resolve(strict=False)
        if not gitdir.is_dir():
            return False, "unknown"
        is_wt_dir = (
            gitdir / "commondir"
        ).is_file() or gitdir.parent.name == "worktrees"
        if not is_wt_dir:
            return False, "unknown"
        head_file = gitdir / "HEAD"
        if not head_file.is_file():
            return False, "unknown"
        branch = _parse_head_branch(head_file)
        if branch in ("main", "master", "", "unknown"):
            return False, branch or "unknown"
        return True, branch
    return False, "unknown"


def _is_docs_or_roadmap_bypass(target_path: Path, repo_root: Path) -> bool:
    try:
        rel = target_path.resolve(strict=False).relative_to(
            repo_root.resolve(strict=False)
        )
    except (ValueError, OSError):
        return False
    if rel.as_posix() in DOCS_BYPASS_FILES:
        return True
    return bool(
        len(rel.parts) >= 2
        and rel.parts[0] == "docs"
        and rel.suffix.lower() in DOCS_BYPASS_SUFFIXES
    )


def _is_plan_all_complete(plan_path: Path) -> bool:
    try:
        lines = plan_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    if lines and lines[0].startswith("\ufeff"):
        lines[0] = lines[0][1:]

    total = 0
    counts = {"complete": 0, "in_progress": 0, "pending": 0}
    in_phase = False
    status_count = 0
    phase_status = ""
    has_active_status = False
    invalid = False
    malformed_heading = False
    fence_char = ""
    fence_len = 0

    def finish_phase() -> None:
        nonlocal in_phase, status_count, phase_status, has_active_status, invalid
        if in_phase:
            if status_count != 1:
                invalid = True
            elif phase_status != "in_progress":
                counts[phase_status] += 1
            if has_active_status:
                counts["in_progress"] += 1
        in_phase = False
        status_count = 0
        phase_status = ""
        has_active_status = False

    for raw in lines:
        line = raw.rstrip("\r")
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker_run, rest = fence_match.group(1), fence_match.group(2)
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
        if fence_char or BLOCKQUOTE_RE.match(line):
            continue
        if MALFORMED_PHASE_RE.match(line):
            finish_phase()
            malformed_heading = True
            continue
        if PHASE_HEADING_RE.match(line):
            finish_phase()
            total += 1
            in_phase = True
            continue
        if ANY_HEADING_RE.match(line):
            finish_phase()
            continue
        if in_phase:
            for match in STATUS_TOKEN_RE.finditer(line):
                token = match.group(1) or match.group(2)
                if token:
                    status_count += 1
                    phase_status = token
                    if token == "in_progress":
                        has_active_status = True

    finish_phase()
    valid = not invalid and not malformed_heading and fence_char == "" and total > 0
    return (
        valid
        and counts["complete"] == total
        and counts["in_progress"] == 0
        and counts["pending"] == 0
    )


def _resolve_active_plan(repo_root: Path) -> Path | None:
    pwf_root_raw = os.environ.get("PWF_PLAN_ROOT", "").strip()
    env_plan_id = os.environ.get("PLAN_ID", "").strip()
    if pwf_root_raw:
        base = Path(os.path.expanduser(pwf_root_raw)).resolve(strict=False)
        if not base.is_dir():
            return None
        if env_plan_id:
            if not _valid_slug(env_plan_id):
                return None
            candidate = base / ".planning" / env_plan_id / "task_plan.md"
            return candidate if candidate.is_file() else None
        has_local_pin, local_plan_id = _read_plan_id_from_mise_local(repo_root)
        if has_local_pin:
            if not _valid_slug(local_plan_id):
                return None
            candidate = base / ".planning" / local_plan_id / "task_plan.md"
            return candidate if candidate.is_file() else None
    else:
        base = repo_root
        has_local_pin, local_plan_id = _read_plan_id_from_mise_local(repo_root)
        if has_local_pin:
            if not _valid_slug(local_plan_id):
                return None
            candidate = base / ".planning" / local_plan_id / "task_plan.md"
            return candidate if candidate.is_file() else None
        if env_plan_id:
            if not _valid_slug(env_plan_id):
                return None
            candidate = base / ".planning" / env_plan_id / "task_plan.md"
            return candidate if candidate.is_file() else None

    active_file = base / ".planning" / ".active_plan"
    if active_file.is_file():
        try:
            active_id = active_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            return None
        if active_id:
            if not _valid_slug(active_id):
                return None
            candidate = base / ".planning" / active_id / "task_plan.md"
            return candidate if candidate.is_file() else None

    planning_dir = base / ".planning"
    if not planning_dir.is_dir():
        return None
    candidates = [
        p
        for p in sorted(planning_dir.glob("*/task_plan.md"))
        if p.parent.name != ".archive" and p.is_file()
    ]
    return candidates[0] if len(candidates) == 1 else None


def _extract_worktree_rm_slug(command: str) -> str | None:
    match = WORKTREE_RM_RE.search(command)
    if not match:
        return None
    tail = match.group(1).strip()
    if not tail:
        return None
    try:
        tokens = shlex.split(tail)
    except ValueError:
        tokens = tail.split()
    for token in tokens:
        if token.startswith("-"):
            continue
        if _valid_slug(token):
            return token
        return None
    return None


def _git_common_parent(repo_root: Path) -> Path | None:
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        return repo_root
    if dot_git.is_file():
        try:
            raw = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if raw.lower().startswith("gitdir:"):
            gitdir = Path(raw.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (repo_root / gitdir).resolve(strict=False)
            for parent in (gitdir, *gitdir.parents):
                if parent.name == ".git":
                    return parent.parent
    return None


def _plans_for_worktree_dir(wt_dir: Path, slug: str) -> list[Path]:
    if not wt_dir.is_dir():
        return []
    found: list[Path] = []
    has_local_pin, local_plan_id = _read_plan_id_from_mise_local(wt_dir)
    if has_local_pin and _valid_slug(local_plan_id):
        found.append(wt_dir / ".planning" / local_plan_id / "task_plan.md")
    planning_dir = wt_dir / ".planning"
    active_file = planning_dir / ".active_plan"
    if active_file.is_file():
        try:
            active_id = active_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            active_id = ""
        if _valid_slug(active_id):
            found.append(planning_dir / active_id / "task_plan.md")
    found.append(planning_dir / slug / "task_plan.md")
    if planning_dir.is_dir():
        for candidate in sorted(planning_dir.glob(f"*-{slug}/task_plan.md")):
            if candidate.parent.name != ".archive":
                found.append(candidate)
    return [p for p in found if p.is_file()]


def _candidate_worktree_plans(base_dir: Path | None, slug: str) -> list[Path]:
    roots: list[Path] = []
    pwf_root_raw = os.environ.get("PWF_PLAN_ROOT", "").strip()
    if pwf_root_raw:
        roots.append(Path(os.path.expanduser(pwf_root_raw)))
    if base_dir is not None:
        roots.append(base_dir)
        repo_root = _find_git_repo_root(base_dir)
        if repo_root is not None:
            roots.append(repo_root)
            common_parent = _git_common_parent(repo_root)
            if common_parent is not None:
                roots.append(common_parent)

    plans: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        candidates: list[Path] = [r / ".planning" / slug / "task_plan.md"]
        if (r / ".planning").is_dir():
            candidates.extend(sorted((r / ".planning").glob(f"*-{slug}/task_plan.md")))
        for wt_dir in (
            r.parent / f"{r.name}-{slug}",
            r.parent / f"{r.name}-worktrees" / slug,
            r.parent / f"{r.name}-wt" / slug,
        ):
            candidates.extend(_plans_for_worktree_dir(wt_dir, slug))
        for candidate in candidates:
            if candidate.parent.name != ".archive" and candidate.is_file():
                try:
                    key = str(candidate.resolve())
                except OSError:
                    key = str(candidate)
                if key not in seen:
                    seen.add(key)
                    plans.append(candidate)
    return plans


def _shared_reviews_path() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(os.path.expanduser(xdg_state))
        if xdg_state
        else Path.home() / ".local" / "state"
    )
    return base / "agy-learn" / "reviews.jsonl"


def _read_shared_reviews(reviews_path: Path | None = None) -> dict[str, tuple[str, str, int]]:
    path = reviews_path or _shared_reviews_path()
    if not path.is_file():
        return {}
    latest: dict[str, tuple[str, str, int]] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for raw in lines:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        raw_key = rec.get("plan_key")
        sha = rec.get("plan_sha256")
        outcome = rec.get("outcome")
        ts_ns = rec.get("timestamp_unix_ns", 0)
        if not (
            isinstance(raw_key, str)
            and raw_key
            and isinstance(sha, str)
            and sha
            and isinstance(outcome, str)
            and outcome
        ):
            continue
        try:
            key = str(Path(os.path.expanduser(raw_key)).resolve(strict=False))
        except OSError:
            key = raw_key
        ns_val = ts_ns if isinstance(ts_ns, int) and not isinstance(ts_ns, bool) else 0
        prev = latest.get(key)
        if prev is None or ns_val >= prev[2]:
            latest[key] = (sha, outcome, ns_val)
    return latest


def _check_worktree_rm(
    payload: dict[str, Any], base_dir: Path | None, command: str
) -> dict[str, Any]:
    slug = _extract_worktree_rm_slug(command)
    if not slug:
        return _allow()
    plans = _candidate_worktree_plans(base_dir, slug)
    if not plans:
        return _allow()

    learned_plans: dict[str, Any] = {}
    pending_key: str | None = None
    owing_plans: dict[str, Any] = {}
    artifact_raw = payload.get("artifactDirectoryPath")
    if isinstance(artifact_raw, str) and artifact_raw.strip():
        artifact_dir = Path(os.path.expanduser(artifact_raw.strip()))
        state_path = artifact_dir / ".post-task-learn-state.json"
        if state_path.is_file():
            try:
                raw_state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                raw_state = {}
            if isinstance(raw_state, dict):
                lp = raw_state.get("learned_plans")
                if isinstance(lp, dict):
                    learned_plans = lp
                op = raw_state.get("owing_learn")
                if isinstance(op, dict):
                    owing_plans = op
                pend = raw_state.get("pending_proposal")
                if isinstance(pend, dict) and isinstance(pend.get("plan_key"), str):
                    pending_key = pend["plan_key"]

    shared_reviews = _read_shared_reviews()
    valid_outcomes = ("proposed", "no_new_proposals", "applied")

    for plan_path in plans:
        try:
            plan_key = str(plan_path.resolve())
            digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        except OSError:
            continue
        shared = shared_reviews.get(plan_key)
        if shared is not None and shared[0] == digest and shared[1] == "owing_learn":
            return _deny(
                f"PWF/learn guard: completed plan '{slug}' is marked `owing_learn` "
                "(retry-exhausted `/learn` review). Run `/learn` and record a review "
                "before removing its worktree."
            )
        if owing_plans.get(plan_key) == digest or pending_key == plan_key:
            return _deny(
                f"PWF/learn guard: run `/learn` on completed plan '{slug}' "
                "(and update `learning_proposal.md` or record a review) before removing its worktree."
            )
        shared_ok = (
            shared is not None and shared[0] == digest and shared[1] in valid_outcomes
        )
        local_ok = learned_plans.get(plan_key) == digest
        if not (local_ok or shared_ok):
            return _deny(
                f"PWF/learn guard: run `/learn` on completed plan '{slug}' "
                "(and update `learning_proposal.md` if `Errors Encountered` is "
                "non-empty) before removing its worktree."
            )
    return _allow()


def _run_hook(payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_PWF_GUARD") == "1":
        return _allow()

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return _allow()
    tool_name = tool_call.get("name")
    if not isinstance(tool_name, str):
        return _allow()

    cwd_raw = payload.get("cwd")
    base_dir: Path | None = (
        Path(os.path.expanduser(cwd_raw))
        if isinstance(cwd_raw, str) and cwd_raw.strip()
        else None
    )
    if base_dir is None:
        wp = payload.get("workspacePaths")
        if isinstance(wp, list) and wp and isinstance(wp[0], str) and wp[0].strip():
            base_dir = Path(os.path.expanduser(wp[0]))

    if tool_name == "run_command":
        args = tool_call.get("args")
        if isinstance(args, dict) and isinstance(args.get("CommandLine"), str):
            return _check_worktree_rm(payload, base_dir, args["CommandLine"])
        return _allow()

    if tool_name not in EDIT_TOOLS:
        return _allow()

    target_path = _resolve_target_path(tool_call.get("args"), base_dir)
    if target_path is None:
        return _allow()

    artifact_raw = payload.get("artifactDirectoryPath")
    if isinstance(artifact_raw, str) and artifact_raw.strip():
        artifact_dir = Path(os.path.expanduser(artifact_raw.strip()))
        if _is_within_dir(target_path, artifact_dir):
            return _allow()

    repo_root = _find_git_repo_root(target_path)
    if repo_root is None or not (repo_root / ".mode").is_file():
        return _allow()

    if _is_within_dir(target_path, repo_root / ".planning"):
        return _allow()
    if target_path == (repo_root / "mise.local.toml").resolve(strict=False):
        return _allow()

    is_linked, branch = _check_linked_worktree(repo_root)
    if not is_linked and _is_docs_or_roadmap_bypass(target_path, repo_root):
        return _allow()

    plan_path = _resolve_active_plan(repo_root)
    if plan_path is None:
        return _deny()
    if _is_plan_all_complete(plan_path):
        return _deny(COMPLETE_DENY_REASON)
    if _is_docs_or_roadmap_bypass(target_path, repo_root):
        return _allow()

    if not is_linked:
        return _deny(
            f"PWF guard: active repo edits in PWF-adopted repos must run in a "
            f"non-main linked worktree (current branch={branch}). Create one with "
            "'mise run worktree:new <slug> --issue <N>' (only ROADMAP.md / docs/** "
            "with an active plan are exempt)."
        )

    has_local_issue, local_issue = _read_issue_from_mise_local(repo_root)
    if has_local_issue and not local_issue:
        return _deny(ISSUE_DENY_REASON)
    has_plan_issue, plan_issue = _extract_task_plan_issue(plan_path)
    if has_plan_issue and not plan_issue:
        return _deny(ISSUE_DENY_REASON)
    if has_local_issue and has_plan_issue and local_issue != plan_issue:
        return _deny(
            f"PWF guard: GITHUB_ISSUE (#{local_issue}) in mise.local.toml "
            f"disagrees with task_plan.md (#{plan_issue}). Reconcile the link."
        )
    if not local_issue and not plan_issue:
        return _deny(ISSUE_DENY_REASON)
    return _allow()


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
    if not isinstance(data, dict) or "agy-pwf-guard" not in data:
        raise AssertionError(f"missing 'agy-pwf-guard' in {path}")
    block = data["agy-pwf-guard"]
    pre_handlers = block.get("PreToolUse") if isinstance(block, dict) else None
    if not isinstance(pre_handlers, list) or not pre_handlers:
        raise AssertionError(f"missing PreToolUse handlers for agy-pwf-guard in {path}")
    first = pre_handlers[0]
    matcher = first.get("matcher", "") if isinstance(first, dict) else ""
    if (
        not isinstance(matcher, str)
        or "edit_file" not in matcher
        or "run_command" not in matcher
    ):
        raise AssertionError(f"unexpected PreToolUse matcher: {matcher!r}")
    inner = first.get("hooks") if isinstance(first, dict) else None
    if not isinstance(inner, list) or not inner:
        raise AssertionError(f"missing PreToolUse hooks list in {path}")
    cmd = inner[0].get("command", "") if isinstance(inner[0], dict) else ""
    if not isinstance(cmd, str) or "agy-pwf-guard.py" not in cmd:
        raise AssertionError(f"unexpected hook command: {cmd!r}")
    if cmd.startswith("/") or "/Users/" in cmd:
        raise AssertionError(
            f"hook command must be portable relative path, got: {cmd!r}"
        )


def _run_self_check(hooks_json_path: Path | None) -> list[str]:
    import tempfile

    checks: list[str] = []
    saved_env = {
        k: os.environ.pop(k, None)
        for k in ("PLAN_ID", "PWF_PLAN_ROOT", "AGY_DISABLE_PWF_GUARD", "XDG_STATE_HOME")
    }
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # 1. Non-PWF repo allowed
            non_pwf = root / "plain-repo"
            (non_pwf / ".git").mkdir(parents=True)
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(non_pwf / "README.md")},
                    }
                }
            )
            assert res == _allow(), f"non-pwf should allow: {res}"
            checks.append("non_pwf_repo_allowed")

            # 2. Artifact dir allowed even when inside PWF repo
            pwf_repo = root / "pwf-repo"
            (pwf_repo / ".git").mkdir(parents=True)
            (pwf_repo / ".mode").write_text("autonomous\n", encoding="utf-8")
            art_dir = pwf_repo / "artifacts"
            art_dir.mkdir()
            res = _run_hook(
                {
                    "artifactDirectoryPath": str(art_dir),
                    "toolCall": {
                        "name": "write_file",
                        "args": {"Path": str(art_dir / "notes.md")},
                    },
                }
            )
            assert res == _allow(), f"artifact dir should allow: {res}"
            checks.append("artifact_dir_allowed")

            # 3. .planning/** and mise.local.toml allowed without an active plan
            for bootstrap_target in (
                pwf_repo / ".planning" / "demo" / "task_plan.md",
                pwf_repo / "mise.local.toml",
            ):
                res = _run_hook(
                    {
                        "toolCall": {
                            "name": "write_file",
                            "args": {"Path": str(bootstrap_target)},
                        }
                    }
                )
                assert res == _allow(), f"bootstrap path should allow: {res}"
            checks.append("planning_and_mise_local_allowed")

            # 4. PWF repo without plan denies edits to tracked files
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(pwf_repo / "AGENTS.md")},
                    }
                }
            )
            assert res.get("decision") == "deny", f"missing plan should deny: {res}"
            checks.append("pwf_missing_plan_denied")

            # 5. Ambiguous multiple plans without active pin denies
            for slug in ("plan-a", "plan-b"):
                pdir = pwf_repo / ".planning" / slug
                pdir.mkdir(parents=True)
                (pdir / "task_plan.md").write_text(
                    "# Plan\n\n### Phase 1: Work\n**Status:** in_progress\n",
                    encoding="utf-8",
                )
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(pwf_repo / "AGENTS.md")},
                    }
                }
            )
            assert res.get("decision") == "deny", f"ambiguous plans should deny: {res}"
            checks.append("ambiguous_plans_denied")

            # 6. Pinning PLAN_ID in mise.local.toml allows edits in an issue-linked worktree
            wt_active = root / "pwf-repo-active"
            wt_active.mkdir()
            (wt_active / ".mode").write_text("autonomous\n", encoding="utf-8")
            wt_active_gitdir = pwf_repo / ".git" / "worktrees" / "active"
            wt_active_gitdir.mkdir(parents=True)
            (wt_active_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
            (wt_active_gitdir / "HEAD").write_text(
                "ref: refs/heads/wt/active\n", encoding="utf-8"
            )
            (wt_active / ".git").write_text(
                f"gitdir: {wt_active_gitdir}\n", encoding="utf-8"
            )
            for slug, issue_num in (("plan-a", "6"), ("plan-b", "7")):
                pdir = wt_active / ".planning" / slug
                pdir.mkdir(parents=True)
                (pdir / "task_plan.md").write_text(
                    f"# Task Plan: #{issue_num} Work\n\n### Phase 1: Work\n**Status:** in_progress\n",
                    encoding="utf-8",
                )
            (wt_active / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "plan-a"\n', encoding="utf-8"
            )
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(wt_active / "AGENTS.md")},
                    }
                }
            )
            assert res == _allow(), f"mise.local.toml pin should allow: {res}"
            checks.append("mise_local_pin_allowed")

            # 7. Env PLAN_ID overrides and validates exact slug
            (wt_active / "mise.local.toml").unlink()
            os.environ["PLAN_ID"] = "plan-b"
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(wt_active / "AGENTS.md")},
                    }
                }
            )
            assert res == _allow(), f"env PLAN_ID should allow: {res}"
            os.environ["PLAN_ID"] = "missing-plan"
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(wt_active / "AGENTS.md")},
                    }
                }
            )
            assert res.get("decision") == "deny", f"bad env PLAN_ID should deny: {res}"
            checks.append("env_plan_id_enforced")

            # 8. All-complete plan denies tracked edits while still allowing .planning/** and mise.local.toml
            done_dir = pwf_repo / ".planning" / "plan-done"
            done_dir.mkdir(parents=True)
            (done_dir / "task_plan.md").write_text(
                "# Done\n\n### Phase 1: Do\n**Status:** complete\n",
                encoding="utf-8",
            )
            os.environ["PLAN_ID"] = "plan-done"
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(pwf_repo / "AGENTS.md")},
                    }
                }
            )
            assert res.get("decision") == "deny", f"complete plan should deny: {res}"
            assert (
                _run_hook(
                    {
                        "toolCall": {
                            "name": "write_file",
                            "args": {"Path": str(pwf_repo / "mise.local.toml")},
                        }
                    }
                )
                == _allow()
            )
            checks.append("complete_plan_denied")

            # 9. worktree:rm requires matching learned_plans digest in .post-task-learn-state.json
            wt_demo = root / "pwf-repo-demo"
            wt_plan_dir = wt_demo / ".planning" / "demo"
            wt_plan_dir.mkdir(parents=True)
            wt_plan = wt_plan_dir / "task_plan.md"
            wt_plan.write_text(
                "# Demo\n\n### Phase 1: Do\n**Status:** complete\n",
                encoding="utf-8",
            )
            (pwf_repo / ".git" / "worktrees" / "demo").mkdir(parents=True)
            (wt_demo / ".git").write_text(
                f"gitdir: {pwf_repo / '.git' / 'worktrees' / 'demo'}\n",
                encoding="utf-8",
            )
            assert (
                _run_hook(
                    {
                        "cwd": str(pwf_repo),
                        "artifactDirectoryPath": str(art_dir),
                        "toolCall": {
                            "name": "run_command",
                            "args": {"CommandLine": "git status"},
                        },
                    }
                )
                == _allow()
            )
            res = _run_hook(
                {
                    "cwd": str(pwf_repo),
                    "artifactDirectoryPath": str(art_dir),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "mise run worktree:rm demo"},
                    },
                }
            )
            assert res.get("decision") == "deny", (
                f"unlearned worktree:rm should deny: {res}"
            )
            (art_dir / ".post-task-learn-state.json").write_text(
                json.dumps(
                    {
                        "last_line": 1,
                        "learned_plans": {
                            str(wt_plan.resolve()): hashlib.sha256(
                                wt_plan.read_bytes()
                            ).hexdigest()
                        },
                    }
                ),
                encoding="utf-8",
            )
            assert (
                _run_hook(
                    {
                        "cwd": str(pwf_repo),
                        "artifactDirectoryPath": str(art_dir),
                        "toolCall": {
                            "name": "run_command",
                            "args": {
                                "CommandLine": "mise run worktree:rm demo --force"
                            },
                        },
                    }
                )
                == _allow()
            )
            checks.append("worktree_rm_learn_gate")

            # 10. mise.local.toml pin overrides stale completed os.environ["PLAN_ID"]
            os.environ["PLAN_ID"] = "plan-done"
            (wt_active / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "plan-a"\n', encoding="utf-8"
            )
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(wt_active / "AGENTS.md")},
                    }
                }
            )
            assert res == _allow(), (
                f"mise.local.toml should override stale env PLAN_ID: {res}"
            )
            checks.append("mise_local_overrides_stale_env_plan_id")

            # 11. Invalid or missing mise.local.toml pin fails closed despite valid env PLAN_ID
            os.environ["PLAN_ID"] = "plan-b"
            for bad_slug in ("missing-local", "../bad"):
                (pwf_repo / "mise.local.toml").write_text(
                    f'[env]\nPLAN_ID = "{bad_slug}"\n', encoding="utf-8"
                )
                res = _run_hook(
                    {
                        "toolCall": {
                            "name": "edit_file",
                            "args": {"Path": str(pwf_repo / "AGENTS.md")},
                        }
                    }
                )
                assert res.get("decision") == "deny", (
                    f"invalid mise.local.toml ({bad_slug}) must deny despite env PLAN_ID: {res}"
                )
            (pwf_repo / "mise.local.toml").unlink()
            os.environ.pop("PLAN_ID", None)
            checks.append("invalid_mise_local_fails_closed_despite_env")

            # 12. worktree:rm resolves date-prefixed .planning/YYYY-MM-DD-<slug> and -wt/ layouts
            wt_dated = root / "pwf-repo-dated"
            wt_dated_plan_dir = wt_dated / ".planning" / "2026-11-23-dated"
            wt_dated_plan_dir.mkdir(parents=True)
            wt_dated_plan = wt_dated_plan_dir / "task_plan.md"
            wt_dated_plan.write_text(
                "# Dated\n\n### Phase 1: Do\n**Status:** complete\n",
                encoding="utf-8",
            )
            (wt_dated / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "2026-11-23-dated"\n', encoding="utf-8"
            )
            res12a = _run_hook(
                {
                    "cwd": str(pwf_repo),
                    "artifactDirectoryPath": str(art_dir),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "mise run worktree:rm dated"},
                    },
                }
            )
            assert res12a.get("decision") == "deny", (
                f"unlearned dated worktree:rm should deny: {res12a}"
            )
            (art_dir / ".post-task-learn-state.json").write_text(
                json.dumps(
                    {
                        "last_line": 1,
                        "learned_plans": {
                            str(wt_dated_plan.resolve()): hashlib.sha256(
                                wt_dated_plan.read_bytes()
                            ).hexdigest()
                        },
                    }
                ),
                encoding="utf-8",
            )
            assert (
                _run_hook(
                    {
                        "cwd": str(pwf_repo),
                        "artifactDirectoryPath": str(art_dir),
                        "toolCall": {
                            "name": "run_command",
                            "args": {"CommandLine": "mise run worktree:rm dated"},
                        },
                    }
                )
                == _allow()
            )

            wt_kb = root / "pwf-repo-wt" / "kb-dated"
            wt_kb_plan_dir = wt_kb / ".planning" / "2026-11-23-kb-dated"
            wt_kb_plan_dir.mkdir(parents=True)
            wt_kb_plan = wt_kb_plan_dir / "task_plan.md"
            wt_kb_plan.write_text(
                "# KB\n\n### Phase 1: Do\n**Status:** complete\n",
                encoding="utf-8",
            )
            res12b = _run_hook(
                {
                    "cwd": str(pwf_repo),
                    "artifactDirectoryPath": str(art_dir),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "mise run worktree:rm kb-dated"},
                    },
                }
            )
            assert res12b.get("decision") == "deny", (
                f"unlearned -wt worktree:rm should deny: {res12b}"
            )
            (art_dir / ".post-task-learn-state.json").write_text(
                json.dumps(
                    {
                        "last_line": 1,
                        "learned_plans": {
                            str(wt_kb_plan.resolve()): hashlib.sha256(
                                wt_kb_plan.read_bytes()
                            ).hexdigest()
                        },
                    }
                ),
                encoding="utf-8",
            )
            assert (
                _run_hook(
                    {
                        "cwd": str(pwf_repo),
                        "artifactDirectoryPath": str(art_dir),
                        "toolCall": {
                            "name": "run_command",
                            "args": {"CommandLine": "mise run worktree:rm kb-dated"},
                        },
                    }
                )
                == _allow()
            )
            checks.append("worktree_rm_date_prefixed_and_wt_layout_gate")

            # 13. Primary main checkout denies code/AGENTS edits even with issue-linked plan,
            # while allowing narrow ROADMAP.md and docs/** bypass when active plan exists.
            (pwf_repo / ".git" / "HEAD").write_text(
                "ref: refs/heads/main\n", encoding="utf-8"
            )
            main_plan_dir = pwf_repo / ".planning" / "main-issue-6"
            main_plan_dir.mkdir(parents=True)
            (main_plan_dir / "task_plan.md").write_text(
                "# Task Plan: #6 Main\n\n### Phase 1: Do\n**Status:** in_progress\n",
                encoding="utf-8",
            )
            (pwf_repo / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "main-issue-6"\nGITHUB_ISSUE = "6"\n',
                encoding="utf-8",
            )
            res_main_code = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(pwf_repo / "tasks" / "worktree" / "new")},
                    }
                }
            )
            assert res_main_code.get("decision") == "deny", (
                f"main checkout must deny code edits: {res_main_code}"
            )
            for bypass_rel in ("ROADMAP.md", "docs/guide.md"):
                res_bypass = _run_hook(
                    {
                        "toolCall": {
                            "name": "edit_file",
                            "args": {"Path": str(pwf_repo / bypass_rel)},
                        }
                    }
                )
                assert res_bypass == _allow(), (
                    f"{bypass_rel} with active plan should allow: {res_bypass}"
                )
            checks.append("main_checkout_denied_docs_roadmap_bypass_allowed")

            # 14. Linked worktree enforces issue-linked metadata (mise.local.toml or task_plan.md)
            unlinked_dir = wt_active / ".planning" / "plan-unlinked"
            unlinked_dir.mkdir(parents=True)
            (unlinked_dir / "task_plan.md").write_text(
                "# Task Plan: Unlinked\n\n## Goal\nDo work\n\n### Phase 1: Do\n**Status:** in_progress\n",
                encoding="utf-8",
            )
            (wt_active / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "plan-unlinked"\n', encoding="utf-8"
            )
            res_unlinked = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(wt_active / "src.py")},
                    }
                }
            )
            assert res_unlinked.get("decision") == "deny", (
                f"unlinked worktree plan must deny code edit: {res_unlinked}"
            )
            assert (
                _run_hook(
                    {
                        "toolCall": {
                            "name": "edit_file",
                            "args": {"Path": str(wt_active / "ROADMAP.md")},
                        }
                    }
                )
                == _allow()
            )

            # GITHUB_ISSUE in mise.local.toml satisfies issue link
            (wt_active / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "plan-unlinked"\nGITHUB_ISSUE = "42"\n',
                encoding="utf-8",
            )
            assert (
                _run_hook(
                    {
                        "toolCall": {
                            "name": "edit_file",
                            "args": {"Path": str(wt_active / "src.py")},
                        }
                    }
                )
                == _allow()
            )

            # ## Goal URL in task_plan.md satisfies issue link
            goal_url_dir = wt_active / ".planning" / "plan-goal-url"
            goal_url_dir.mkdir(parents=True)
            (goal_url_dir / "task_plan.md").write_text(
                "# Task Plan: Via URL\n\n## Goal\nImplement https://github.com/org/repo/issues/19\n\n### Phase 1: Do\n**Status:** in_progress\n",
                encoding="utf-8",
            )
            (wt_active / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "plan-goal-url"\n', encoding="utf-8"
            )
            assert (
                _run_hook(
                    {
                        "toolCall": {
                            "name": "edit_file",
                            "args": {"Path": str(wt_active / "src.py")},
                        }
                    }
                )
                == _allow()
            )

            # Mismatched GITHUB_ISSUE vs task_plan.md fails closed
            (wt_active / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "plan-goal-url"\nGITHUB_ISSUE = "99"\n',
                encoding="utf-8",
            )
            res_mismatch = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(wt_active / "src.py")},
                    }
                }
            )
            assert res_mismatch.get("decision") == "deny", (
                f"mismatched issue must deny: {res_mismatch}"
            )
            checks.append("linked_worktree_issue_mapping_enforced")

            # 15. Nanosecond timestamp formatting (zero-padded 9 decimals)
            sample_ts = _format_ns_timestamp(1_700_000_000_000_000_007)
            assert ".000000007" in sample_ts, f"expected 9-digit zero pad: {sample_ts}"
            assert NS_TIMESTAMP_RE.match(sample_ts), (
                f"timestamp format mismatch: {sample_ts}"
            )
            checks.append("nanosecond_timestamp_format")

            # 16. Cross-session worktree:rm lookup checks $XDG_STATE_HOME/agy-learn/reviews.jsonl
            xdg_state16 = root / "xdg-state-16"
            os.environ["XDG_STATE_HOME"] = str(xdg_state16)
            wt_cross = root / "pwf-repo-cross16"
            wt_cross_plan_dir = wt_cross / ".planning" / "2026-11-23-cross16"
            wt_cross_plan_dir.mkdir(parents=True)
            wt_cross_plan = wt_cross_plan_dir / "task_plan.md"
            wt_cross_plan.write_text(
                "# Cross16\n\n### Phase 1: Do\n**Status:** complete\n",
                encoding="utf-8",
            )
            cross_sha = hashlib.sha256(wt_cross_plan.read_bytes()).hexdigest()
            reviews_path16 = xdg_state16 / "agy-learn" / "reviews.jsonl"
            reviews_path16.parent.mkdir(parents=True)
            # Without review and without artifactDirectoryPath -> still denies!
            res16a = _run_hook(
                {
                    "cwd": str(pwf_repo),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "mise run worktree:rm cross16"},
                    },
                }
            )
            assert res16a.get("decision") == "deny", f"case 16a should deny: {res16a}"
            # Owing review denies even across sessions
            reviews_path16.write_text(
                json.dumps(
                    {
                        "plan_key": str(wt_cross_plan.resolve()),
                        "plan_sha256": cross_sha,
                        "outcome": "owing_learn",
                        "timestamp_unix_ns": 100,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            res16b = _run_hook(
                {
                    "cwd": str(pwf_repo),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "mise run worktree:rm cross16"},
                    },
                }
            )
            assert res16b.get("decision") == "deny", (
                f"case 16b owing_learn should deny: {res16b}"
            )
            # Valid no_new_proposals review later clears cross-session worktree:rm
            with reviews_path16.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "plan_key": str(wt_cross_plan.resolve()),
                            "plan_sha256": cross_sha,
                            "outcome": "no_new_proposals",
                            "timestamp_unix_ns": 200,
                        }
                    )
                    + "\n"
                )
            res16c = _run_hook(
                {
                    "cwd": str(pwf_repo),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "mise run worktree:rm cross16"},
                    },
                }
            )
            assert res16c == _allow(), f"case 16c shared review should allow: {res16c}"
            checks.append("worktree_rm_cross_session_shared_reviews")

            # 17. ROADMAP.md and docs/** on main are not blocked by stale all-complete ENV PLAN_ID
            (pwf_repo / "mise.local.toml").unlink(missing_ok=True)
            os.environ["PLAN_ID"] = "plan-done"
            for bypass_rel in ("ROADMAP.md", "docs/guide.md"):
                res17 = _run_hook(
                    {
                        "toolCall": {
                            "name": "edit_file",
                            "args": {"Path": str(pwf_repo / bypass_rel)},
                        }
                    }
                )
                assert res17 == _allow(), (
                    f"case 17 {bypass_rel} on main should allow despite stale complete PLAN_ID: {res17}"
                )
            res17_code = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(pwf_repo / "AGENTS.md")},
                    }
                }
            )
            assert res17_code.get("decision") == "deny", (
                f"case 17 non-doc on main must still deny: {res17_code}"
            )
            os.environ.pop("PLAN_ID", None)
            checks.append("main_docs_roadmap_bypass_with_stale_complete_env_plan_id")

        if hooks_json_path is not None:
            _check_hooks_json(hooks_json_path)
            checks.append("hooks_json_portable")
    finally:
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run built-in regression checks in a temporary directory.",
    )
    parser.add_argument(
        "--check-hooks-json",
        type=Path,
        default=None,
        help="Verify hooks.json registers agy-pwf-guard cleanly.",
    )
    args = parser.parse_args()

    if args.self_check:
        checks = _run_self_check(args.check_hooks_json)
        sys.stdout.write(
            json.dumps(
                {
                    "status": "ok",
                    "verified_at": _format_ns_timestamp(),
                    "checks": checks,
                }
            )
            + "\n"
        )
        return 0

    if args.check_hooks_json is not None:
        _check_hooks_json(args.check_hooks_json)
        sys.stdout.write(
            json.dumps(
                {
                    "status": "ok",
                    "verified_at": _format_ns_timestamp(),
                    "checks": ["hooks_json_portable"],
                }
            )
            + "\n"
        )
        return 0

    result = _process_stdin_text(sys.stdin.read())
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
