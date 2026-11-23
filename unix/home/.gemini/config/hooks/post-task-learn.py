#!/usr/bin/env python3
"""Antigravity Stop hook triggering a bounded post-task /learn check for PWF and general tasks."""

from __future__ import annotations

import argparse
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
    r"(?:\b(?:git\s+(?:add|commit|push|merge|worktree\s+remove)|repotools\s+pr-create|pr-merge"
    r"|gh\s+pr\s+(?:create|merge)"
    r"|workflowtools\s+(?:round-close|verify-receipt)"
    r"|mise\s+(?:(?:-C|--cd)\s+\S+\s+)?run\s+(?:ci|check|pins:apt-bump|worktree:rm|//repo:pr-merge|kb:(?:add|update|fix|judge|verify-fix|register|finish|retro))"
    r"|hc\s+send)\b|(?:^|[\s/])ship\.py\s+(?:commit|merge|cleanup)\b)"
)
WORKTREE_RM_RE = re.compile(
    r"\bmise\s+(?:(?:-C|--cd)\s+\S+\s+)?run\s+worktree:rm\s+([a-z0-9][a-z0-9_-]*)"
)
SHIP_CLOSEOUT_RE = re.compile(r"(?:^|[\s/])ship\.py\s+(merge|cleanup)\b")
USER_REQUEST_RE = re.compile(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>")
USER_LEARN_CMD_RE = re.compile(r"^\s*/learn(?:\s|$)")
SYSTEM_LEARN_RE = re.compile(r"Automated post-task /learn check")


def _is_ignored_system_message(content: str) -> bool:
    return (
        "[Message] timestamp=" in content
        or "Plan mode is off:" in content
        or "[plan review]" in content
        or "[plan review due]" in content
    )


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
    "inspect user corrections, failures, and reusable fixes; prioritize extracting repeatable workflows "
    "or error checklists into Skills (`skills/<name>/SKILL.md`) over appending to always-on rules. "
    "Only fast-exit in one line if truly zero errors, retries, or reusable lessons occurred; otherwise "
    "create/update `learning_proposal.md` with RequestFeedback=true without modifying configuration files before explicit approval."
)
TRIGGER_REASON = (
    "Automated post-task /learn check (per https://antigravity.google/docs/commands/learn/). "
    + BASE_TRIGGER_SUFFIX
)
ERROR_HEADING_RE = re.compile(r"^#{1,3}\s+Errors\s+Encountered\b", re.IGNORECASE)
SECTION_HEADING_RE = re.compile(r"^#{1,2}(?!#)\s+")
PLACEHOLDER_CELLS = frozenset({"", "-", "none", "none yet", "n/a", "na", "nil"})


def _extract_user_request(text: str) -> str:
    match = USER_REQUEST_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _is_explicit_user_learn(text: str) -> bool:
    return bool(USER_LEARN_CMD_RE.match(_extract_user_request(text)))


def _read_state(
    state_path: Path, total_lines: int
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return 0, {}, None
        val = int(raw.get("last_line", 0))
        last_line = val if 0 <= val <= total_lines else 0
        raw_plans = raw.get("learned_plans")
        learned_plans: dict[str, str] = {}
        if isinstance(raw_plans, dict):
            for k, v in raw_plans.items():
                if isinstance(k, str) and isinstance(v, str):
                    learned_plans[k] = v
        raw_pending = raw.get("pending_proposal")
        pending: dict[str, Any] | None = None
        if isinstance(raw_pending, dict):
            plan_key = raw_pending.get("plan_key")
            label = raw_pending.get("label")
            digest = raw_pending.get("digest")
            error_count = raw_pending.get("error_count")
            baseline = raw_pending.get("baseline_proposal_sha")
            retries = raw_pending.get("retries", 0)
            if (
                isinstance(plan_key, str)
                and isinstance(label, str)
                and isinstance(digest, str)
                and isinstance(error_count, int)
                and not isinstance(error_count, bool)
                and isinstance(baseline, str)
                and isinstance(retries, int)
                and not isinstance(retries, bool)
            ):
                pending = {
                    "plan_key": plan_key,
                    "label": label,
                    "digest": digest,
                    "error_count": error_count,
                    "baseline_proposal_sha": baseline,
                    "retries": retries,
                }
        return last_line, learned_plans, pending
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0, {}, None


def _write_state(
    state_path: Path,
    last_line: int,
    learned_plans: dict[str, str],
    pending_proposal: dict[str, Any] | None = None,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=state_path.parent, prefix=f".{state_path.name}."
    )
    replaced = False
    payload: dict[str, Any] = {
        "last_line": last_line,
        "learned_plans": learned_plans,
    }
    if pending_proposal is not None:
        payload["pending_proposal"] = pending_proposal
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


def _file_sha256(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _count_plan_errors(text: str) -> int:
    in_section = False
    fence_char = ""
    fence_len = 0
    count = 0
    for raw in text.splitlines():
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
        if fence_char:
            continue
        stripped = line.strip()
        if ERROR_HEADING_RE.match(stripped):
            in_section = True
            continue
        if in_section and SECTION_HEADING_RE.match(stripped):
            break
        if not in_section or not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells:
            continue
        if all(re.fullmatch(r":?-{1,}:?", c or "-") for c in cells):
            continue
        first_norm = cells[0].lower().rstrip(".")
        if first_norm in {"error", "issue", "errors"}:
            continue
        if first_norm in PLACEHOLDER_CELLS or all(
            c.lower().rstrip(".") in PLACEHOLDER_CELLS for c in cells
        ):
            continue
        count += 1
    return count


def _slice_wrote_learning_proposal(lines: list[str], base_dir: Path | None) -> bool:
    for raw in lines:
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        tool_calls = entry.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict) or tc.get("name") not in EDIT_TOOLS:
                continue
            args = tc.get("args")
            target = _tool_target_str(args)
            target_path = _path_from_arg(args, base_dir)
            if (
                target_path is not None and target_path.name == "learning_proposal.md"
            ) or "learning_proposal.md" in target:
                return True
    return False


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


def _is_within_dir(candidate: Path, parent: Path) -> bool:
    try:
        return candidate.resolve(strict=False).is_relative_to(
            parent.resolve(strict=False)
        )
    except OSError:
        return False


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
    lines: list[str],
    last_user_input: str,
    base_dir: Path | None,
    artifact_dir: Path | None,
) -> tuple[bool, bool, list[Path], str | None, str | None]:
    """Return (already_learned, has_task_work, touched_plans, closed_worktree_slug, ship_closeout)."""
    if last_user_input and _is_explicit_user_learn(last_user_input):
        return True, False, [], None, None

    parsed: list[dict[str, Any]] = []
    last_user_pos: int | None = None
    for raw in lines:
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "USER_INPUT":
            last_user_pos = len(parsed)
        parsed.append(entry)

    turn_entries = parsed[last_user_pos:] if last_user_pos is not None else parsed

    last_learn_idx = -1
    last_work_idx = -1
    has_task_work = False
    touched_plans: list[Path] = []
    closed_worktree_slug: str | None = None
    ship_closeout: str | None = None

    for idx, entry in enumerate(turn_entries):
        entry_type = entry.get("type")
        content = entry.get("content")
        if isinstance(content, str) and (
            (entry_type == "USER_INPUT" and _is_explicit_user_learn(content))
            or (
                entry_type == "SYSTEM_MESSAGE"
                and not _is_ignored_system_message(content)
                and SYSTEM_LEARN_RE.search(content)
            )
        ):
            last_learn_idx = idx

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
                target_path = _path_from_arg(args, base_dir)
                if (
                    target_path is not None
                    and target_path.name == "learning_proposal.md"
                ) or "learning_proposal.md" in target:
                    last_learn_idx = idx
                elif (
                    artifact_dir is not None
                    and target_path is not None
                    and _is_within_dir(target_path, artifact_dir)
                ):
                    continue
                else:
                    has_task_work = True
                    last_work_idx = idx
                    if target_path is not None and target_path.name == "task_plan.md":
                        touched_plans.append(target_path)
            elif name == "read_file":
                target = _tool_target_str(args)
                if "post-task-learn/SKILL.md" in target:
                    last_learn_idx = idx
                p = _path_from_arg(args, base_dir)
                if p is not None and p.name == "task_plan.md":
                    touched_plans.append(p)
            elif name == "run_command" and isinstance(args, dict):
                cmd = args.get("CommandLine")
                if isinstance(cmd, str):
                    if TASK_CMD_RE.search(cmd):
                        has_task_work = True
                        last_work_idx = idx
                    wt_match = WORKTREE_RM_RE.search(cmd)
                    if wt_match:
                        closed_worktree_slug = wt_match.group(1)
                        last_work_idx = idx
                    ship_match = SHIP_CLOSEOUT_RE.search(cmd)
                    if ship_match:
                        ship_closeout = ship_match.group(1)
                        last_work_idx = idx

    already_learned = last_learn_idx > last_work_idx

    return (
        already_learned,
        has_task_work,
        touched_plans,
        closed_worktree_slug,
        ship_closeout,
    )


def _run_hook(payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_AUTO_LEARN") == "1":
        return {}
    if payload.get("terminationReason") != "NO_TOOL_CALL":
        return {}
    if payload.get("fullyIdle") is not True:
        return {}

    transcript_raw = payload.get("transcriptPath")
    if not isinstance(transcript_raw, str) or not transcript_raw:
        return {}
    transcript_path = Path(transcript_raw)
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
        Path(artifact_raw).expanduser()
        if isinstance(artifact_raw, str) and artifact_raw
        else None
    )
    state_base = artifact_dir if artifact_dir is not None else transcript_path.parent
    state_path = state_base / ".post-task-learn-state.json"

    last_line, learned_plans, pending_proposal = _read_state(state_path, len(lines))

    cwd_raw = payload.get("cwd")
    base_dir = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else None
    proposal_path = (
        artifact_dir / "learning_proposal.md" if artifact_dir is not None else None
    )
    cur_proposal_sha = _file_sha256(proposal_path)
    new_lines = lines[last_line:]

    if payload.get("continuationActive") or payload.get("continuation_active"):
        if pending_proposal is not None:
            wrote_proposal = (
                bool(cur_proposal_sha)
                and cur_proposal_sha != pending_proposal["baseline_proposal_sha"]
            ) or _slice_wrote_learning_proposal(new_lines, base_dir)
            if wrote_proposal or int(pending_proposal["retries"]) >= 1:
                learned_plans[pending_proposal["plan_key"]] = pending_proposal["digest"]
                _write_state(state_path, len(lines), learned_plans, None)
                return {}
            updated_pending = dict(pending_proposal)
            updated_pending["retries"] = int(pending_proposal["retries"]) + 1
            _write_state(state_path, len(lines), learned_plans, updated_pending)
            reason = (
                f"Post-task /learn hard gate: completed plan '{pending_proposal['label']}' "
                f"recorded {pending_proposal['error_count']} error(s) in `## Errors Encountered`, "
                "but `learning_proposal.md` was not created/updated. Do NOT fast-exit — write/update "
                "`learning_proposal.md` (RequestFeedback=true) evaluating Skill (`skills/<name>/SKILL.md`), "
                "Rule, or Hook extraction for those errors before stopping."
            )
            return {"decision": "continue", "reason": reason}
        _write_state(state_path, len(lines), learned_plans, None)
        return {}

    last_user_input = payload.get("lastUserInput")
    user_text = last_user_input if isinstance(last_user_input, str) else ""
    (
        already_learned,
        has_task_work,
        touched_plans,
        closed_worktree_slug,
        ship_closeout,
    ) = _analyze_slice(new_lines, user_text, base_dir, artifact_dir)

    if already_learned:
        _write_state(state_path, len(lines), learned_plans, None)
        return {}

    discovered_plans = _discover_plans(payload, touched_plans)
    completed_unlearned: list[tuple[Path, str, str, int]] = []
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
                completed_unlearned.append(
                    (plan_path, plan_key, digest, _count_plan_errors(text))
                )
        else:
            has_incomplete_plan = True

    if completed_unlearned:
        labels = ", ".join(_plan_label(p) for p, _, _, _ in completed_unlearned)
        total_errors = sum(err for _, _, _, err in completed_unlearned)
        if total_errors > 0 and proposal_path is not None:
            first_path, first_key, first_digest, _ = completed_unlearned[0]
            for _, pk, dig, _ in completed_unlearned[1:]:
                learned_plans[pk] = dig
            next_pending = {
                "plan_key": first_key,
                "label": _plan_label(first_path),
                "digest": first_digest,
                "error_count": total_errors,
                "baseline_proposal_sha": cur_proposal_sha,
                "retries": 0,
            }
            _write_state(state_path, len(lines), learned_plans, next_pending)
            reason = (
                f"Automated post-task /learn check: planning-with-files task completed ({labels}) "
                f"with {total_errors} logged error(s) in `## Errors Encountered`. "
                + BASE_TRIGGER_SUFFIX
                + " Review the completed `task_plan.md` (`Errors Encountered`, `Decisions Made`), "
                "`findings.md`, and `progress.md` alongside user messages — fast-exit is blocked until "
                "`learning_proposal.md` evaluates Skill/Rule/Hook extraction."
            )
            return {"decision": "continue", "reason": reason}

        for _, plan_key, digest, _ in completed_unlearned:
            learned_plans[plan_key] = digest
        _write_state(state_path, len(lines), learned_plans, None)
        reason = (
            f"Automated post-task /learn check: planning-with-files task completed ({labels}). "
            + BASE_TRIGGER_SUFFIX
            + " Review the completed `task_plan.md` (`Errors Encountered`, `Decisions Made`), `findings.md`, and `progress.md` alongside user messages."
        )
        return {"decision": "continue", "reason": reason}

    _write_state(state_path, len(lines), learned_plans, None)

    if closed_worktree_slug:
        reason = (
            f"Automated post-task /learn check: worktree PWF task '{closed_worktree_slug}' finished. "
            + BASE_TRIGGER_SUFFIX
        )
        return {"decision": "continue", "reason": reason}

    if ship_closeout:
        reason = (
            f"Automated post-task /learn check: prototype/ship.py {ship_closeout} completed. "
            + BASE_TRIGGER_SUFFIX
        )
        return {"decision": "continue", "reason": reason}

    if has_incomplete_plan:
        return {}

    if has_task_work:
        return {"decision": "continue", "reason": TRIGGER_REASON}

    return {}


def _check_hooks_json(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "post-task-learn" not in data:
        raise AssertionError(f"missing 'post-task-learn' in {path}")
    block = data["post-task-learn"]
    stop_handlers = block.get("Stop") if isinstance(block, dict) else None
    if not isinstance(stop_handlers, list) or not stop_handlers:
        raise AssertionError(f"missing Stop handlers for post-task-learn in {path}")
    cmd = (
        stop_handlers[0].get("command", "")
        if isinstance(stop_handlers[0], dict)
        else ""
    )
    if not isinstance(cmd, str) or "post-task-learn.py" not in cmd:
        raise AssertionError(f"unexpected hook command: {cmd!r}")
    if cmd.startswith("/") or "/Users/" in cmd:
        raise AssertionError(
            f"hook command must be portable relative path, got: {cmd!r}"
        )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _run_self_check(hooks_json_path: Path | None) -> list[str]:
    checks: list[str] = []
    saved_env = {
        k: os.environ.pop(k, None)
        for k in ("PLAN_ID", "PWF_PLAN_ROOT", "AGY_DISABLE_AUTO_LEARN")
    }
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # 1. continuationActive advances last_line so next turn still triggers
            ws1 = root / "ws1"
            ws1.mkdir()
            t1 = root / "t1.jsonl"
            art1 = root / "art1"
            art1.mkdir()
            _write_jsonl(
                t1,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Task 1</USER_REQUEST>",
                    },
                    {
                        "type": "SYSTEM_MESSAGE",
                        "content": "Automated post-task /learn check",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "read_file",
                                "args": {"Path": "post-task-learn/SKILL.md"},
                            }
                        ],
                    },
                ],
            )
            assert (
                _run_hook(
                    {
                        "terminationReason": "NO_TOOL_CALL",
                        "fullyIdle": True,
                        "continuationActive": True,
                        "transcriptPath": str(t1),
                        "artifactDirectoryPath": str(art1),
                        "cwd": str(ws1),
                    }
                )
                == {}
            )
            _write_jsonl(
                t1,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Task 1</USER_REQUEST>",
                    },
                    {
                        "type": "SYSTEM_MESSAGE",
                        "content": "Automated post-task /learn check",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "read_file",
                                "args": {"Path": "post-task-learn/SKILL.md"},
                            }
                        ],
                    },
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Task 2</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {"name": "edit_file", "args": {"Path": str(ws1 / "foo.py")}}
                        ],
                    },
                ],
            )
            res1 = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "continuationActive": False,
                    "transcriptPath": str(t1),
                    "artifactDirectoryPath": str(art1),
                    "cwd": str(ws1),
                    "lastUserInput": "<USER_REQUEST>Task 2</USER_REQUEST>",
                }
            )
            assert res1.get("decision") == "continue", f"case 1 failed: {res1}"
            checks.append("continuation_active_advances_last_line")

            # 2. Prose /learn with <LEARN> in ADDITIONAL_METADATA does not suppress; explicit /learn does
            t2 = root / "t2.jsonl"
            art2 = root / "art2"
            art2.mkdir()
            prose = (
                "<USER_REQUEST>\nautomate running /learn after every task\n</USER_REQUEST>\n"
                "<ADDITIONAL_METADATA><LEARN>expanded skill metadata</LEARN></ADDITIONAL_METADATA>"
            )
            _write_jsonl(
                t2,
                [
                    {"type": "USER_INPUT", "content": prose},
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {"name": "edit_file", "args": {"Path": str(ws1 / "bar.py")}}
                        ],
                    },
                ],
            )
            res2a = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t2),
                    "artifactDirectoryPath": str(art2),
                    "cwd": str(ws1),
                    "lastUserInput": prose,
                }
            )
            assert res2a.get("decision") == "continue", f"case 2a failed: {res2a}"
            t2b = root / "t2b.jsonl"
            art2b = root / "art2b"
            art2b.mkdir()
            cmd_learn = "<USER_REQUEST>\n/learn\n</USER_REQUEST>\n<ADDITIONAL_METADATA><LEARN>ok</LEARN></ADDITIONAL_METADATA>"
            _write_jsonl(
                t2b,
                [
                    {"type": "USER_INPUT", "content": cmd_learn},
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {"name": "edit_file", "args": {"Path": str(ws1 / "baz.py")}}
                        ],
                    },
                ],
            )
            res2b = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t2b),
                    "artifactDirectoryPath": str(art2b),
                    "cwd": str(ws1),
                    "lastUserInput": cmd_learn,
                }
            )
            assert res2b == {}, f"case 2b failed: {res2b}"
            checks.append("prose_vs_explicit_learn_detection")

            # 3. Completed PWF plan triggers once and dedupes on SHA-256
            ws3 = root / "ws3"
            plan_dir = ws3 / ".planning" / "demo"
            plan_dir.mkdir(parents=True)
            plan_file = plan_dir / "task_plan.md"
            plan_file.write_text(
                "# Demo\n\n### Phase 1: Do\n**Status:** complete\n", encoding="utf-8"
            )
            t3 = root / "t3.jsonl"
            art3 = root / "art3"
            art3.mkdir()
            _write_jsonl(
                t3,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    }
                ],
            )
            p3 = {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(t3),
                "artifactDirectoryPath": str(art3),
                "cwd": str(ws3),
            }
            assert _run_hook(p3).get("decision") == "continue"
            _write_jsonl(
                t3,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    },
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Next</USER_REQUEST>",
                    },
                ],
            )
            assert _run_hook(p3) == {}
            checks.append("pwf_completion_digest_dedup")

            # 4. ship.py merge triggers & artifactDirectoryPath edits are ignored
            t4 = root / "t4.jsonl"
            art4 = root / "art4"
            art4.mkdir()
            _write_jsonl(
                t4,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Write plan</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "write_file",
                                "args": {"Path": str(art4 / "task.md")},
                            }
                        ],
                    },
                ],
            )
            p4 = {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(t4),
                "artifactDirectoryPath": str(art4),
                "cwd": str(ws1),
            }
            assert _run_hook(p4) == {}, "artifact dir write should not trigger"
            _write_jsonl(
                t4,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Write plan</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "write_file",
                                "args": {"Path": str(art4 / "task.md")},
                            }
                        ],
                    },
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Ship</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "run_command",
                                "args": {
                                    "CommandLine": "python3 prototype/ship.py merge --pr 1"
                                },
                            }
                        ],
                    },
                ],
            )
            res4 = _run_hook(p4)
            assert res4.get("decision") == "continue" and "ship.py merge" in res4.get(
                "reason", ""
            ), f"case 4 failed: {res4}"
            checks.append("ship_closeout_and_artifact_dir_filtering")

            # 5. Prior-turn SYSTEM_MESSAGE before latest USER_INPUT does not suppress new turn
            t5 = root / "t5.jsonl"
            art5 = root / "art5"
            art5.mkdir()
            _write_jsonl(
                t5,
                [
                    {
                        "type": "SYSTEM_MESSAGE",
                        "content": "Automated post-task /learn check",
                    },
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Edit code</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {"name": "edit_file", "args": {"Path": str(ws1 / "qux.py")}}
                        ],
                    },
                ],
            )
            assert (
                _run_hook(
                    {
                        "terminationReason": "NO_TOOL_CALL",
                        "fullyIdle": True,
                        "transcriptPath": str(t5),
                        "artifactDirectoryPath": str(art5),
                        "cwd": str(ws1),
                    }
                ).get("decision")
                == "continue"
            )
            checks.append("turn_boundary_isolation")

            # 6. Peer message & Plan mode SYSTEM_MESSAGE deliveries do not suppress /learn
            t6 = root / "t6.jsonl"
            art6 = root / "art6"
            art6.mkdir()
            _write_jsonl(
                t6,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Work</USER_REQUEST>",
                    },
                    {
                        "type": "SYSTEM_MESSAGE",
                        "content": "[Message] timestamp=2026-11-23T06:00:00Z content=Automated post-task /learn check and post-task-learn/SKILL.md",
                    },
                    {
                        "type": "SYSTEM_MESSAGE",
                        "content": "Plan mode is off: Automated post-task /learn check",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "edit_file",
                                "args": {"Path": str(ws1 / "peer.py")},
                            }
                        ],
                    },
                ],
            )
            res6 = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t6),
                    "artifactDirectoryPath": str(art6),
                    "cwd": str(ws1),
                }
            )
            assert res6.get("decision") == "continue", f"case 6 failed: {res6}"
            checks.append("peer_and_plan_system_messages_ignored")

            # 7. Reading post-task-learn/SKILL.md before later edit across ask_question still triggers
            t7 = root / "t7.jsonl"
            art7 = root / "art7"
            art7.mkdir()
            _write_jsonl(
                t7,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Fix</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "read_file",
                                "args": {"Path": "post-task-learn/SKILL.md"},
                            },
                            {"name": "ask_question", "args": {}},
                        ],
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "edit_file",
                                "args": {"Path": str(ws1 / "after.py")},
                            }
                        ],
                    },
                ],
            )
            res7 = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t7),
                    "artifactDirectoryPath": str(art7),
                    "cwd": str(ws1),
                }
            )
            assert res7.get("decision") == "continue", f"case 7 failed: {res7}"
            checks.append("order_aware_learn_after_ask_question")

            # 8. Generalized scripts/ship.py cleanup triggers closeout
            t8 = root / "t8.jsonl"
            art8 = root / "art8"
            art8.mkdir()
            _write_jsonl(
                t8,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Clean</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "run_command",
                                "args": {
                                    "CommandLine": "python3 scripts/ship.py cleanup --pr 1"
                                },
                            }
                        ],
                    },
                ],
            )
            res8 = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t8),
                    "artifactDirectoryPath": str(art8),
                    "cwd": str(ws1),
                }
            )
            assert res8.get("decision") == "continue" and "ship.py cleanup" in res8.get(
                "reason", ""
            ), f"case 8 failed: {res8}"
            checks.append("ship_cleanup_generalized_path")

            # 9. Plan with Errors Encountered blocks premature fast-exit once via hard gate
            ws9 = root / "ws9"
            pdir9 = ws9 / ".planning" / "err9"
            pdir9.mkdir(parents=True)
            (pdir9 / "task_plan.md").write_text(
                "# Err9\n\n### Phase 1: Fix\n**Status:** complete\n\n"
                "## Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| Bug A | Fixed A |\n| Bug B | Fixed B |\n",
                encoding="utf-8",
            )
            t9 = root / "t9.jsonl"
            art9 = root / "art9"
            art9.mkdir()
            _write_jsonl(
                t9,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    }
                ],
            )
            p9 = {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(t9),
                "artifactDirectoryPath": str(art9),
                "cwd": str(ws9),
            }
            res9a = _run_hook(p9)
            assert res9a.get(
                "decision"
            ) == "continue" and "2 logged error(s)" in res9a.get("reason", ""), (
                f"case 9a failed: {res9a}"
            )
            _write_jsonl(
                t9,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    },
                    {
                        "type": "SYSTEM_MESSAGE",
                        "content": "Automated post-task /learn check",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [],
                        "content": "No updates needed.",
                    },
                ],
            )
            res9b = _run_hook(dict(p9, continuationActive=True))
            assert res9b.get("decision") == "continue" and "hard gate" in res9b.get(
                "reason", ""
            ), f"case 9b failed: {res9b}"
            res9c = _run_hook(dict(p9, continuationActive=True))
            assert res9c == {}, f"case 9c bounded retry failed: {res9c}"
            checks.append("errors_encountered_hard_gate")

            # 10. Updating learning_proposal.md satisfies hard gate and marks plan learned
            ws10 = root / "ws10"
            pdir10 = ws10 / ".planning" / "err10"
            pdir10.mkdir(parents=True)
            (pdir10 / "task_plan.md").write_text(
                "# Err10\n\n### Phase 1: Fix\n**Status:** complete\n\n"
                "## Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| Bug C | Fixed C |\n",
                encoding="utf-8",
            )
            t10 = root / "t10.jsonl"
            art10 = root / "art10"
            art10.mkdir()
            _write_jsonl(
                t10,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    }
                ],
            )
            p10 = {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(t10),
                "artifactDirectoryPath": str(art10),
                "cwd": str(ws10),
            }
            res10a = _run_hook(p10)
            assert res10a.get("decision") == "continue", f"case 10a failed: {res10a}"
            (art10 / "learning_proposal.md").write_text(
                "# Proposal\n\n1. Skill added.\n", encoding="utf-8"
            )
            res10b = _run_hook(dict(p10, continuationActive=True))
            assert res10b == {}, f"case 10b failed: {res10b}"
            _write_jsonl(
                t10,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Next</USER_REQUEST>",
                    }
                ],
            )
            assert _run_hook(p10) == {}, "case 10c should keep plan deduped"
            checks.append("proposal_update_clears_hard_gate")

            # 11. Placeholder 'None yet' under Errors Encountered permits fast-exit
            ws11 = root / "ws11"
            pdir11 = ws11 / ".planning" / "clean11"
            pdir11.mkdir(parents=True)
            (pdir11 / "task_plan.md").write_text(
                "# Clean11\n\n### Phase 1: Do\n**Status:** complete\n\n"
                "## Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| None yet | - |\n",
                encoding="utf-8",
            )
            t11 = root / "t11.jsonl"
            art11 = root / "art11"
            art11.mkdir()
            _write_jsonl(
                t11,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    }
                ],
            )
            p11 = {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(t11),
                "artifactDirectoryPath": str(art11),
                "cwd": str(ws11),
            }
            res11a = _run_hook(p11)
            assert res11a.get("decision") == "continue", f"case 11a failed: {res11a}"
            assert _run_hook(dict(p11, continuationActive=True)) == {}
            checks.append("zero_error_placeholder_allows_fast_exit")

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
        help="Verify hooks.json registers post-task-learn cleanly.",
    )
    args = parser.parse_args()

    if args.self_check:
        checks = _run_self_check(args.check_hooks_json)
        sys.stdout.write(json.dumps({"status": "ok", "checks": checks}) + "\n")
        return 0

    if args.check_hooks_json is not None:
        _check_hooks_json(args.check_hooks_json)
        sys.stdout.write(
            json.dumps({"status": "ok", "checks": ["hooks_json_portable"]}) + "\n"
        )
        return 0

    try:
        raw_payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        raw_payload = None
    result = _run_hook(raw_payload) if isinstance(raw_payload, dict) else {}
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
