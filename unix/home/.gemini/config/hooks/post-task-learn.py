#!/usr/bin/env python3
"""Antigravity Stop hook triggering a bounded post-task /learn check for PWF and general tasks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
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
TASK_CMD_RE = re.compile(
    r"\b(git\s+(add|commit|push|merge|worktree\s+remove)|repotools\s+pr-create|pr-merge"
    r"|workflowtools\s+(round-close|verify-receipt)"
    r"|mise\s+run\s+(ci|check|pins:apt-bump|worktree:rm|//repo:pr-merge)|hc\s+send)\b"
)
WORKTREE_RM_RE = re.compile(
    r"\bmise\s+(?:(?:-C|--cd)\s+\S+\s+)?run\s+worktree:rm\s+([a-z0-9][a-z0-9_-]*)"
)
LEARN_MARKER_RE = re.compile(r"(?:^|\s)/learn\b|<LEARN>|post-task-learn/SKILL\.md")

BLOCKQUOTE_RE = re.compile(r"^ ? ? ?>")
FENCE_RE = re.compile(r"^ ? ? ?(`{3,}|~{3,})(.*)$")
MALFORMED_PHASE_RE = re.compile(r"^ ? ? ?### Phase([0-9:]|-[0-9])")
PHASE_HEADING_RE = re.compile(r"^ ? ? ?### Phase([ \t]|$)")
ANY_HEADING_RE = re.compile(r"^ ? ? ?(###|##|#)([ \t]|$)")
STATUS_TOKEN_RE = re.compile(
    r"\*\*Status:\*\*[ \t]+(pending|in_progress|complete)(?![A-Za-z0-9_])"
    r"|\[(pending|in_progress|complete)\]"
)

BASE_TRIGGER_SUFFIX = (
    "Load the `post-task-learn` skill and run one bounded `/learn` review of the task just completed: "
    "inspect user corrections, failures, and reusable fixes; if nothing reusable emerged, reply in "
    "one line and stop; otherwise create/update `learning_proposal.md` with RequestFeedback=true "
    "without modifying configuration files before explicit approval."
)
TRIGGER_REASON = (
    "Automated post-task /learn check (per https://antigravity.google/docs/commands/learn/). "
    + BASE_TRIGGER_SUFFIX
)


def _emit(payload: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(payload) + "\n")
    return 0


def _load_stdin() -> dict[str, Any] | None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_state(state_path: Path, total_lines: int) -> tuple[int, dict[str, str]]:
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return 0, {}
        val = int(raw.get("last_line", 0))
        last_line = val if 0 <= val <= total_lines else 0
        raw_plans = raw.get("learned_plans")
        learned_plans: dict[str, str] = {}
        if isinstance(raw_plans, dict):
            for k, v in raw_plans.items():
                if isinstance(k, str) and isinstance(v, str):
                    learned_plans[k] = v
        return last_line, learned_plans
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0, {}


def _write_state(
    state_path: Path, last_line: int, learned_plans: dict[str, str]
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=state_path.parent, prefix=f".{state_path.name}."
    )
    replaced = False
    payload = {"last_line": last_line, "learned_plans": learned_plans}
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        os.replace(tmp_name, state_path)
        replaced = True
    except OSError:
        return
    finally:
        if not replaced:
            Path(tmp_name).unlink(missing_ok=True)


def _tool_target_str(args: Any) -> str:
    if isinstance(args, dict):
        for key in ("Path", "TargetFile", "path", "target_file"):
            val = args.get(key)
            if isinstance(val, str):
                return val
    return json.dumps(args, ensure_ascii=False)


def _path_from_arg(args: Any, base_dir: Path | None) -> Path | None:
    if not isinstance(args, dict):
        return None
    for key in ("Path", "TargetFile", "path", "target_file"):
        val = args.get(key)
        if isinstance(val, str) and val:
            candidate = Path(os.path.expanduser(val))
            if not candidate.is_absolute() and base_dir is not None:
                candidate = base_dir / candidate
            return candidate
    return None


def _parse_pwf_plan(text: str) -> tuple[int, int, int, int, bool]:
    """Return (total, complete, in_progress, pending, valid) matching check-complete.sh."""
    lines = text.splitlines()
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
            else:
                if phase_status != "in_progress":
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
                if marker == "`" and "`" in rest:
                    pass
                else:
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
    return total, counts["complete"], counts["in_progress"], counts["pending"], valid


def _plans_for_root(root: Path) -> list[Path]:
    planning = root / ".planning"
    if planning.is_dir():
        active_file = planning / ".active_plan"
        if active_file.is_file():
            try:
                active_id = active_file.read_text(encoding="utf-8").strip()
            except OSError:
                active_id = ""
            if active_id:
                plan = planning / active_id / "task_plan.md"
                return [plan] if plan.is_file() else []
        candidates = sorted(planning.glob("*/task_plan.md"))
        if len(candidates) == 1:
            return [candidates[0]]
        return []
    legacy = root / "task_plan.md"
    return [legacy] if legacy.is_file() else []


def _discover_plans(payload: dict[str, Any], touched_plans: list[Path]) -> list[Path]:
    roots: list[Path] = []
    cwd_raw = payload.get("cwd")
    if isinstance(cwd_raw, str) and cwd_raw:
        roots.append(Path(cwd_raw))
    wp_raw = payload.get("workspacePaths")
    if isinstance(wp_raw, list):
        for wp in wp_raw:
            if isinstance(wp, str) and wp:
                roots.append(Path(wp))

    found: list[Path] = []
    seen: set[str] = set()

    def add_plan(p: Path) -> None:
        if not p.is_file():
            return
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key not in seen:
            seen.add(key)
            found.append(p)

    plan_id = os.environ.get("PLAN_ID", "").strip()
    pwf_root_raw = os.environ.get("PWF_PLAN_ROOT", "").strip()
    if plan_id:
        base = Path(pwf_root_raw) if pwf_root_raw else (roots[0] if roots else None)
        if base is not None:
            add_plan(base / ".planning" / plan_id / "task_plan.md")
    else:
        seen_roots: set[str] = set()
        if pwf_root_raw:
            roots = [Path(pwf_root_raw), *roots]
        for root in roots:
            try:
                rkey = str(root.resolve())
            except OSError:
                rkey = str(root)
            if rkey in seen_roots or not root.is_dir():
                continue
            seen_roots.add(rkey)
            for p in _plans_for_root(root):
                add_plan(p)

    for touched in touched_plans:
        add_plan(touched)

    return found


def _plan_label(plan_path: Path) -> str:
    if plan_path.parent.parent.name == ".planning":
        return plan_path.parent.name
    return str(plan_path)


def _plan_sha256(plan_path: Path) -> str | None:
    try:
        return hashlib.sha256(plan_path.read_bytes()).hexdigest()
    except OSError:
        return None


def _analyze_slice(
    lines: list[str], last_user_input: str, base_dir: Path | None
) -> tuple[bool, bool, list[Path], str | None]:
    """Return (already_learned, has_task_work, touched_plans, closed_worktree_slug)."""
    if LEARN_MARKER_RE.search(last_user_input):
        return True, False, [], None

    already_learned = False
    has_task_work = False
    touched_plans: list[Path] = []
    closed_worktree_slug: str | None = None

    for raw in lines:
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            entry = None
        if not isinstance(entry, dict):
            continue

        entry_type = entry.get("type")
        content = entry.get("content")
        if (
            entry_type in ("USER_INPUT", "SYSTEM_MESSAGE")
            and isinstance(content, str)
            and LEARN_MARKER_RE.search(content)
        ):
            already_learned = True

        tool_calls = entry.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name")
            args = tc.get("args")
            if name in EDIT_TOOLS:
                target = _tool_target_str(args)
                if "learning_proposal.md" in target:
                    already_learned = True
                else:
                    has_task_work = True
                    p = _path_from_arg(args, base_dir)
                    if p is not None and p.name == "task_plan.md":
                        touched_plans.append(p)
            elif name == "read_file":
                target = _tool_target_str(args)
                if "post-task-learn/SKILL.md" in target:
                    already_learned = True
                p = _path_from_arg(args, base_dir)
                if p is not None and p.name == "task_plan.md":
                    touched_plans.append(p)
            elif name == "run_command" and isinstance(args, dict):
                cmd = args.get("CommandLine")
                if isinstance(cmd, str):
                    if TASK_CMD_RE.search(cmd):
                        has_task_work = True
                    wt_match = WORKTREE_RM_RE.search(cmd)
                    if wt_match:
                        closed_worktree_slug = wt_match.group(1)

    return already_learned, has_task_work, touched_plans, closed_worktree_slug


def main() -> int:
    if os.environ.get("AGY_DISABLE_AUTO_LEARN") == "1":
        return _emit({})

    payload = _load_stdin()
    if payload is None:
        return _emit({})

    if payload.get("terminationReason") != "NO_TOOL_CALL":
        return _emit({})
    if payload.get("fullyIdle") is not True:
        return _emit({})
    if payload.get("continuationActive") or payload.get("continuation_active"):
        return _emit({})

    transcript_raw = payload.get("transcriptPath")
    if not isinstance(transcript_raw, str) or not transcript_raw:
        return _emit({})
    transcript_path = Path(transcript_raw)
    if not transcript_path.is_file():
        return _emit({})

    try:
        lines = transcript_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return _emit({})

    artifact_dir = payload.get("artifactDirectoryPath")
    state_base = (
        Path(artifact_dir)
        if isinstance(artifact_dir, str) and artifact_dir
        else transcript_path.parent
    )
    state_path = state_base / ".post-task-learn-state.json"

    last_line, learned_plans = _read_state(state_path, len(lines))
    new_lines = lines[last_line:]

    cwd_raw = payload.get("cwd")
    base_dir = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else None

    last_user_input = payload.get("lastUserInput")
    user_text = last_user_input if isinstance(last_user_input, str) else ""
    already_learned, has_task_work, touched_plans, closed_worktree_slug = (
        _analyze_slice(new_lines, user_text, base_dir)
    )

    if already_learned:
        _write_state(state_path, len(lines), learned_plans)
        return _emit({})

    discovered_plans = _discover_plans(payload, touched_plans)
    completed_unlearned: list[tuple[Path, str, str]] = []
    has_incomplete_plan = False

    for plan_path in discovered_plans:
        try:
            text = plan_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total, complete, in_progress, pending, valid = _parse_pwf_plan(text)
        if not valid:
            continue
        if complete == total and in_progress == 0 and pending == 0:
            digest = _plan_sha256(plan_path)
            if digest is None:
                continue
            try:
                plan_key = str(plan_path.resolve())
            except OSError:
                plan_key = str(plan_path)
            if learned_plans.get(plan_key) != digest:
                completed_unlearned.append((plan_path, plan_key, digest))
        else:
            has_incomplete_plan = True

    if completed_unlearned:
        for _, plan_key, digest in completed_unlearned:
            learned_plans[plan_key] = digest
        _write_state(state_path, len(lines), learned_plans)
        labels = ", ".join(_plan_label(p) for p, _, _ in completed_unlearned)
        reason = (
            f"Automated post-task /learn check: planning-with-files task completed ({labels}). "
            + BASE_TRIGGER_SUFFIX
            + " Review the completed `task_plan.md` (`Errors Encountered`, `Decisions Made`), `findings.md`, and `progress.md` alongside user messages."
        )
        return _emit({"decision": "continue", "reason": reason})

    _write_state(state_path, len(lines), learned_plans)

    if closed_worktree_slug:
        reason = (
            f"Automated post-task /learn check: worktree PWF task '{closed_worktree_slug}' finished. "
            + BASE_TRIGGER_SUFFIX
        )
        return _emit({"decision": "continue", "reason": reason})

    if has_incomplete_plan:
        return _emit({})

    if has_task_work:
        return _emit({"decision": "continue", "reason": TRIGGER_REASON})

    return _emit({})


if __name__ == "__main__":
    raise SystemExit(main())
