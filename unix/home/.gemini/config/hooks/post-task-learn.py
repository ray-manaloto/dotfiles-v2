#!/usr/bin/env python3
"""Antigravity Stop hook triggering a bounded post-task /learn check for PWF and general tasks."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
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
TASK_CMD_RE = re.compile(
    r"(?:\b(?:git\s+(?:add|commit|push|merge|worktree\s+remove)|repotools\s+pr-create|pr-merge"
    r"|gh\s+pr\s+(?:create|merge)"
    r"|mise\s+(?:(?:-C|--cd)\s+\S+\s+)?run\s+(?:ci|check|pins:apt-bump|worktree:rm|kb:(?:add|update|fix|judge|verify-fix|register|finish|retro))"
    r"|hc\s+send)\b|(?:^|[\s/])ship\.py\s+(?:commit|merge|cleanup)\b)"
)
WORKTREE_RM_RE = re.compile(
    r"\bmise\s+(?:(?:-C|--cd)\s+\S+\s+)?run\s+worktree:rm\b([^\n;&|]*)"
)
SHIP_CLOSEOUT_RE = re.compile(r"(?:^|[\s/])ship\.py\s+(merge|cleanup)\b")
USER_REQUEST_RE = re.compile(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>")
USER_LEARN_CMD_RE = re.compile(r"^\s*/learn(?:\s|$)")
SYSTEM_LEARN_RE = re.compile(r"Automated post-task /learn check")
SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PLAN_ID_LINE_RE = re.compile(r'^\s*PLAN_ID\s*=\s*"([^"\r\n]+)"')
LEARN_APPLY_RE = re.compile(
    r"\b(?:apply|approve|ship|merge)\b[^\n]{0,80}\b(?:learning_proposal(?:\.md)?|proposal\s+\d+|/learn\s+proposal)\b"
    r"|\blearning_proposal\.md\b",
    re.IGNORECASE,
)
NONZERO_EXIT_RE = re.compile(r"\bCommand exited with code ([1-9]\d*)\b")
TOOL_DENY_RE = re.compile(r'"decision"\s*:\s*"deny"')


def _is_learn_doc_path(path: Path | None) -> bool:
    return path is not None and path.suffix.lower() == ".md"


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

LEARN_CMD_RE = re.compile(
    r"\b(?:mise\s+(?:(?:-C|--cd)\s+\S+\s+)?run\s+learn:(?:record-review|propose|render-proposal)|agy_learn\.cli\s+(?:record-review|propose|render-proposal))\b"
)

BASE_TRIGGER_SUFFIX = (
    "Load the `post-task-learn` skill and run one bounded `/learn` review of the task just completed: "
    "inspect user corrections, failures, and reusable fixes; prioritize extracting repeatable workflows "
    "into Skills or Tier 1-3 deterministic gates (`hk.pkl` -> `mise doctor project` -> hook) over prose rules. "
    "Record the review via `mise run learn:record-review --plan-key <plan_path> --plan-sha256 <sha256>` "
    "(`--outcome no_new_proposals --survivor-count 0` is valid when 0 survivors exist) "
    "or `mise run learn:propose` + `mise run learn:render-proposal` (<= 8 KB `learning_proposal.md`, "
    "always setting `RequestFeedback=false` during automated Stop hooks)."
)
TRIGGER_REASON = (
    "Automated post-task /learn check (per https://antigravity.google/docs/commands/learn/). "
    + BASE_TRIGGER_SUFFIX
)
ERROR_HEADING_RE = re.compile(r"^(#{1,3})\s+Errors\s+Encountered\b", re.IGNORECASE)
ANY_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+")
PLACEHOLDER_CELLS = frozenset({"", "-", "none", "none yet", "n/a", "na", "nil"})


def _extract_user_request(text: str) -> str:
    match = USER_REQUEST_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _is_explicit_user_learn(text: str) -> bool:
    return bool(USER_LEARN_CMD_RE.match(_extract_user_request(text)))


def _now_ns_pair() -> tuple[str, int]:
    epoch_ns = time.time_ns()
    sec, rem_ns = divmod(epoch_ns, 1_000_000_000)
    dt = datetime.fromtimestamp(sec, timezone.utc).astimezone()
    return f"{dt:%Y%m%dT%H%M%S}.{rem_ns:09d}{dt:%z}", epoch_ns


def _shared_state_dir() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(os.path.expanduser(xdg_state))
        if xdg_state
        else Path.home() / ".local" / "state"
    )
    return base / "agy-learn"


def _shared_reviews_path() -> Path:
    return _shared_state_dir() / "reviews.jsonl"


def _read_shared_review_details(
    reviews_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    path = reviews_path or _shared_reviews_path()
    if not path.is_file():
        return {}
    latest: dict[str, dict[str, Any]] = {}
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
        prev_ns = prev.get("_ns", -1) if prev is not None else -1
        if prev is None or ns_val >= prev_ns:
            entry = dict(rec)
            entry["_key"] = key
            entry["_ns"] = ns_val
            latest[key] = entry
    return latest


def _read_shared_reviews(
    reviews_path: Path | None = None,
) -> dict[str, tuple[str, str, int]]:
    details = _read_shared_review_details(reviews_path)
    return {
        key: (str(rec["plan_sha256"]), str(rec["outcome"]), int(rec["_ns"]))
        for key, rec in details.items()
    }


def _slug_matches_plan_key(slug: str, plan_key: str) -> bool:
    parts = Path(plan_key).parts
    suffix = f"-{slug}"
    candidates: list[str] = []
    if ".planning" in parts:
        p_idx = parts.index(".planning")
        if p_idx > 0:
            candidates.append(parts[p_idx - 1])
        if p_idx + 1 < len(parts):
            candidates.append(parts[p_idx + 1])
    elif len(parts) >= 2:
        candidates.append(parts[-2])
    return any(cand == slug or cand.endswith(suffix) for cand in candidates)


def _worktree_slug_cleared_in_reviews(
    slug: str,
    review_details: dict[str, dict[str, Any]],
    learned_plans: dict[str, str],
    owing_learn: dict[str, str],
) -> bool:
    valid_outcomes = ("proposed", "no_new_proposals", "applied")
    suffix = f"-{slug}"
    for key, rec in review_details.items():
        if rec.get("outcome") not in valid_outcomes:
            continue
        if rec.get("worktree_slug") == slug:
            return True
        plan_id = rec.get("plan_id")
        if isinstance(plan_id, str) and (plan_id == slug or plan_id.endswith(suffix)):
            return True
        if _slug_matches_plan_key(slug, key):
            return True
    for key in learned_plans:
        if key in owing_learn:
            continue
        if _slug_matches_plan_key(slug, key):
            return True
    return False


def _infer_review_metadata(
    plan_key: str,
    transcript_path: Path,
    payload: dict[str, Any] | None = None,
) -> tuple[str | None, str | None, str | None]:
    conv_id: str | None = None
    if isinstance(payload, dict):
        raw_conv = payload.get("conversationId") or payload.get("conversation_id")
        if isinstance(raw_conv, str) and raw_conv.strip():
            conv_id = raw_conv.strip()
    if conv_id is None:
        parts = transcript_path.parts
        if ".system_generated" in parts:
            idx = parts.index(".system_generated")
            if idx >= 1 and parts[idx - 1]:
                conv_id = parts[idx - 1]

    plan_path = Path(os.path.expanduser(plan_key))
    repo_root: str | None = None
    cur = plan_path.parent
    while cur != cur.parent:
        if (cur / ".git").exists():
            repo_root = str(cur)
            break
        cur = cur.parent

    worktree_slug: str | None = None
    if plan_path.parent.parent.name == ".planning" and plan_path.parent.name:
        worktree_slug = plan_path.parent.name
    elif repo_root is not None and (Path(repo_root) / ".git").is_file():
        worktree_slug = Path(repo_root).name

    return repo_root, worktree_slug, conv_id


def _append_shared_review(
    *,
    plan_key: str,
    label: str,
    digest: str,
    error_count: int,
    outcome: str,
    notes: str,
    transcript_path: Path,
    line_count: int,
    payload: dict[str, Any] | None = None,
) -> None:
    reviews_path = _shared_reviews_path()
    lock_path = reviews_path.with_suffix(".jsonl.lock")
    ts, ns = _now_ns_pair()
    rev_id = (
        "rev-" + hashlib.sha256(f"{plan_key}:{digest}:{ns}".encode()).hexdigest()[:12]
    )
    inferred_repo, inferred_slug, inferred_conv = _infer_review_metadata(
        plan_key, transcript_path, payload
    )
    record = {
        "review_id": rev_id,
        "plan_key": plan_key,
        "plan_id": label,
        "worktree_slug": inferred_slug,
        "plan_sha256": digest,
        "error_count": max(0, int(error_count)),
        "outcome": outcome,
        "survivor_count": 0,
        "proposal_ids": [],
        "provenance": {
            "transcript_step": f"{transcript_path.name}#L{line_count}",
            "plan_error_ref": f"{plan_key}#Errors Encountered",
            "transcript_path": str(transcript_path),
            "plan_path": plan_key,
        },
        "repo_root": inferred_repo,
        "conversation_id": inferred_conv,
        "notes": notes,
        "timestamp": ts,
        "timestamp_unix_ns": ns,
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        reviews_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                with reviews_path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        sys.stderr.write(
            f"post-task-learn: failed appending review to {reviews_path}: {exc}\n"
        )


def _append_owing_review(
    *,
    plan_key: str,
    label: str,
    digest: str,
    error_count: int,
    transcript_path: Path,
    line_count: int,
    payload: dict[str, Any] | None = None,
) -> None:
    _append_shared_review(
        plan_key=plan_key,
        label=label,
        digest=digest,
        error_count=error_count,
        outcome="owing_learn",
        notes="Auto-marked owing_learn after Stop-hook retry exhaustion.",
        transcript_path=transcript_path,
        line_count=line_count,
        payload=payload,
    )


def _read_state(
    state_path: Path, total_lines: int
) -> tuple[int, dict[str, str], dict[str, Any] | None, dict[str, str]]:
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return 0, {}, None, {}
        val = int(raw.get("last_line", 0))
        last_line = val if 0 <= val <= total_lines else 0
        raw_plans = raw.get("learned_plans")
        learned_plans: dict[str, str] = {}
        if isinstance(raw_plans, dict):
            for k, v in raw_plans.items():
                if isinstance(k, str) and isinstance(v, str):
                    learned_plans[k] = v
        raw_owing = raw.get("owing_learn")
        owing_learn: dict[str, str] = {}
        if isinstance(raw_owing, dict):
            for k, v in raw_owing.items():
                if isinstance(k, str) and isinstance(v, str):
                    owing_learn[k] = v
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
                raw_pp = raw_pending.get("pending_plans")
                if isinstance(raw_pp, list):
                    valid_pp = [
                        item
                        for item in raw_pp
                        if isinstance(item, dict)
                        and isinstance(item.get("plan_key"), str)
                        and isinstance(item.get("label"), str)
                        and isinstance(item.get("digest"), str)
                        and isinstance(item.get("error_count"), int)
                    ]
                    if valid_pp:
                        pending["pending_plans"] = valid_pp
        return last_line, learned_plans, pending, owing_learn
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0, {}, None, {}


def _write_state(
    state_path: Path,
    last_line: int,
    learned_plans: dict[str, str],
    pending_proposal: dict[str, Any] | None = None,
    owing_learn: dict[str, str] | None = None,
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
    if owing_learn:
        payload["owing_learn"] = owing_learn
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
    err_heading_level = 0
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
        err_match = ERROR_HEADING_RE.match(stripped)
        if err_match:
            in_section = True
            err_heading_level = len(err_match.group(1))
            continue
        if in_section:
            heading_match = ANY_MD_HEADING_RE.match(stripped)
            if heading_match and len(heading_match.group(1)) <= err_heading_level:
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
        if val.endswith('\\"'):
            return val[1:]
    if val.startswith('"') and not val.endswith('"'):
        return val[1:]
    return val


def _load_full_records(
    transcript_path: Path | None, expected_types: dict[int, tuple[Any, Any]]
) -> dict[int, dict[str, Any]]:
    if transcript_path is None or not expected_types:
        return {}
    full_path = transcript_path.with_name("transcript_full.jsonl")
    if not full_path.is_file():
        return {}
    out: dict[int, dict[str, Any]] = {}
    needed_indices = set(expected_types)
    max_idx = max(needed_indices)
    try:
        with full_path.open(
            "r", encoding="utf-8", errors="replace", newline="\n"
        ) as handle:
            for idx, raw in enumerate(handle):
                if idx > max_idx:
                    break
                if idx not in needed_indices:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                exp_type, exp_step = expected_types[idx]
                if exp_type is not None and obj.get("type") != exp_type:
                    continue
                if (
                    exp_step is not None
                    and obj.get("step_index") is not None
                    and obj.get("step_index") != exp_step
                ):
                    continue
                out[idx] = obj
    except OSError:
        return {}
    return out


def _slice_wrote_learning_proposal(
    lines: list[str],
    base_dir: Path | None,
    *,
    transcript_path: Path | None = None,
    line_offset: int = 0,
) -> bool:
    parsed_slice: list[tuple[int, dict[str, Any]]] = []
    expected_types: dict[int, tuple[Any, Any]] = {}
    for rel_idx, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        abs_idx = line_offset + rel_idx
        parsed_slice.append((abs_idx, entry))
        trunc = entry.get("truncated_fields")
        if isinstance(trunc, list) and "tool_calls" in trunc:
            expected_types[abs_idx] = (entry.get("type"), entry.get("step_index"))

    full_records = _load_full_records(transcript_path, expected_types)
    for abs_idx, raw_entry in parsed_slice:
        entry = full_records.get(abs_idx, raw_entry)
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
                    return True
    return False


def _tool_target_str(args: Any) -> str:
    if isinstance(args, dict):
        for key in ("Path", "TargetFile", "path", "target_file"):
            val = args.get(key)
            if isinstance(val, str):
                return _unquote_truncated(val)
    return json.dumps(args, ensure_ascii=False)


def _path_from_arg(args: Any, base_dir: Path | None) -> Path | None:
    if not isinstance(args, dict):
        return None
    for key in ("Path", "TargetFile", "path", "target_file"):
        val = args.get(key)
        if isinstance(val, str) and val:
            unquoted = _unquote_truncated(val).strip()
            if not unquoted:
                continue
            candidate = Path(os.path.expanduser(unquoted))
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


def _valid_slug(slug: str) -> bool:
    return bool(slug and SAFE_SLUG_RE.match(slug) and slug not in (".", ".."))


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
        candidates = [
            p
            for p in sorted(planning.glob("*/task_plan.md"))
            if p.parent.name != ".archive" and p.is_file()
        ]
        if len(candidates) == 1:
            return [candidates[0]]
        return []
    legacy = root / "task_plan.md"
    return [legacy] if legacy.is_file() else []


def _discover_plans(payload: dict[str, Any], touched_plans: list[Path]) -> list[Path]:
    roots: list[Path] = []
    cwd_raw = payload.get("cwd")
    if isinstance(cwd_raw, str) and cwd_raw.strip():
        roots.append(Path(os.path.expanduser(cwd_raw.strip())))
    wp_raw = payload.get("workspacePaths")
    if isinstance(wp_raw, list):
        for wp in wp_raw:
            if isinstance(wp, str) and wp.strip():
                roots.append(Path(os.path.expanduser(wp.strip())))

    found: list[Path] = []
    seen: set[str] = set()

    def add_plan(p: Path) -> None:
        if not p.is_file() or ".archive" in p.parts:
            return
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key not in seen:
            seen.add(key)
            found.append(p)

    env_plan_id = os.environ.get("PLAN_ID", "").strip()
    pwf_root_raw = os.environ.get("PWF_PLAN_ROOT", "").strip()
    if pwf_root_raw:
        pwf_root = Path(os.path.expanduser(pwf_root_raw))
        if env_plan_id:
            if _valid_slug(env_plan_id):
                add_plan(pwf_root / ".planning" / env_plan_id / "task_plan.md")
        else:
            seen_roots: set[str] = set()
            for root in [pwf_root, *roots]:
                try:
                    rkey = str(root.resolve())
                except OSError:
                    rkey = str(root)
                if rkey in seen_roots or not root.is_dir():
                    continue
                seen_roots.add(rkey)
                for p in _plans_for_root(root):
                    add_plan(p)
    else:
        seen_roots = set()
        for root in roots:
            try:
                rkey = str(root.resolve())
            except OSError:
                rkey = str(root)
            if rkey in seen_roots or not root.is_dir():
                continue
            seen_roots.add(rkey)
            has_local_pin, local_plan_id = _read_plan_id_from_mise_local(root)
            if has_local_pin:
                if _valid_slug(local_plan_id):
                    add_plan(root / ".planning" / local_plan_id / "task_plan.md")
            elif env_plan_id:
                if _valid_slug(env_plan_id):
                    add_plan(root / ".planning" / env_plan_id / "task_plan.md")
            else:
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


def _extract_worktree_rm_slugs(command: str) -> tuple[list[str], list[tuple[int, int]]]:
    slugs: list[str] = []
    spans: list[tuple[int, int]] = []
    for match in WORKTREE_RM_RE.finditer(command):
        tail = match.group(1).strip()
        if not tail:
            continue
        try:
            tokens = shlex.split(tail)
        except ValueError:
            tokens = tail.split()
        for token in tokens:
            if token.startswith("-"):
                continue
            if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", token):
                slugs.append(token)
                spans.append(match.span())
            break
    return slugs, spans


def _extract_worktree_rm_slug(command: str) -> str | None:
    slugs, _ = _extract_worktree_rm_slugs(command)
    return slugs[0] if slugs else None


def _strip_spans(command: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return command
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(command[cursor:start])
        cursor = end
    parts.append(command[cursor:])
    return "".join(parts)


def _match_tool_result_in_slice(
    results: list[dict[str, Any]],
    planner_obj: dict[str, Any],
    tc_idx: int,
    tc_obj: dict[str, Any],
) -> dict[str, Any] | None:
    tc_id = tc_obj.get("id") or tc_obj.get("toolCallId") or tc_obj.get("callId")
    if isinstance(tc_id, str) and tc_id:
        by_id = next(
            (
                r
                for r in results
                if (
                    r.get("tool_call_id")
                    or r.get("toolCallId")
                    or r.get("callId")
                    or r.get("id")
                )
                == tc_id
            ),
            None,
        )
        if by_id is not None:
            return by_id
    p_step = planner_obj.get("step_index")
    if isinstance(p_step, int) and not isinstance(p_step, bool):
        want_step = p_step + 1 + tc_idx
        by_step = next(
            (r for r in results if r.get("step_index") == want_step),
            None,
        )
        if by_step is not None:
            return by_step
    tool_calls = planner_obj.get("tool_calls")
    if (
        isinstance(tool_calls, list)
        and len(results) == len(tool_calls)
        and 0 <= tc_idx < len(results)
    ):
        return results[tc_idx]
    tc_name = tc_obj.get("name")
    if isinstance(tc_name, str) and tc_name:
        named = [r for r in results if (r.get("name") or r.get("toolName")) == tc_name]
        if len(named) == 1 and isinstance(tool_calls, list):
            same_named = [
                t
                for t in tool_calls
                if isinstance(t, dict) and t.get("name") == tc_name
            ]
            if len(same_named) == 1:
                return named[0]
    return None


def _tool_call_succeeded_in_slice(
    turn_entries: list[tuple[int, dict[str, Any]]],
    entry_idx: int,
    planner_obj: dict[str, Any],
    tc_idx: int,
    tc_obj: dict[str, Any],
    full_records: dict[int, dict[str, Any]],
) -> bool:
    if tc_obj.get("status") == "ERROR" or bool(tc_obj.get("error")):
        return False
    results: list[dict[str, Any]] = []
    for k in range(entry_idx + 1, len(turn_entries)):
        abs_idx, raw_rec = turn_entries[k]
        rec = full_records.get(abs_idx, raw_rec)
        rtype = rec.get("type")
        if rtype in {"PLANNER_RESPONSE", "USER_INPUT"}:
            break
        if rtype in {"GENERIC", "ERROR_MESSAGE", "TOOL_RESULT", "FUNCTION_RESPONSE"}:
            results.append(rec)
    if not results:
        return True
    matched = _match_tool_result_in_slice(results, planner_obj, tc_idx, tc_obj)
    if matched is None:
        return False
    if (
        matched.get("type") == "ERROR_MESSAGE"
        or matched.get("status") == "ERROR"
        or bool(matched.get("error"))
    ):
        return False
    content = matched.get("content")
    if isinstance(content, str):
        if TOOL_DENY_RE.search(content):
            return False
        if "Command exited with code" in content:
            return re.search(r"\bCommand exited with code 0\b", content) is not None
        if NONZERO_EXIT_RE.search(content):
            return False
    return True


def _analyze_slice(
    lines: list[str],
    last_user_input: str,
    base_dir: Path | None,
    artifact_dir: Path | None,
    *,
    transcript_path: Path | None = None,
    line_offset: int = 0,
) -> tuple[bool, bool, list[Path], list[str], str | None, bool]:
    """Return (already_learned, has_task_work, touched_plans, closed_worktree_slugs, ship_closeout, is_zero_error_learn_apply_docs_only)."""
    if last_user_input and _is_explicit_user_learn(last_user_input):
        return True, False, [], [], None, False

    parsed: list[tuple[int, dict[str, Any]]] = []
    last_user_pos: int | None = None
    for rel_idx, raw in enumerate(lines):
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
        parsed.append((line_offset + rel_idx, entry))

    turn_entries = parsed[last_user_pos:] if last_user_pos is not None else parsed
    expected_types = {
        abs_idx: (obj.get("type"), obj.get("step_index"))
        for abs_idx, obj in turn_entries
        if isinstance(obj.get("truncated_fields"), list)
        and (
            "tool_calls" in obj["truncated_fields"]
            or "content" in obj["truncated_fields"]
        )
    }
    full_records = _load_full_records(transcript_path, expected_types)

    last_learn_pos: tuple[int, int] = (-1, -1)
    last_work_pos: tuple[int, int] = (-1, -1)
    has_task_work = False
    touched_plans: list[Path] = []
    edited_work_paths: list[Path | None] = []
    read_learning_proposal = False
    has_turn_error = False
    turn_user_text = last_user_input
    closed_worktree_slugs: list[str] = []
    ship_closeout: str | None = None

    for idx, (abs_idx, raw_entry) in enumerate(turn_entries):
        entry = full_records.get(abs_idx, raw_entry)
        entry_type = entry.get("type")
        content = entry.get("content")
        if entry_type == "USER_INPUT" and isinstance(content, str):
            turn_user_text = content
        if (
            entry_type == "ERROR_MESSAGE"
            or entry.get("status") == "ERROR"
            or bool(entry.get("error"))
            or (
                entry_type == "GENERIC"
                and isinstance(content, str)
                and bool(
                    NONZERO_EXIT_RE.search(content) or TOOL_DENY_RE.search(content)
                )
            )
        ):
            has_turn_error = True
        if isinstance(content, str) and (
            (entry_type == "USER_INPUT" and _is_explicit_user_learn(content))
            or (
                entry_type == "SYSTEM_MESSAGE"
                and not _is_ignored_system_message(content)
                and SYSTEM_LEARN_RE.search(content)
            )
        ):
            last_learn_pos = (idx, -1)

        tool_calls = entry.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for tc_idx, tc in enumerate(tool_calls):
            if not isinstance(tc, dict):
                continue
            pos = (idx, tc_idx)
            name = tc.get("name")
            args = tc.get("args")
            if name in EDIT_TOOLS:
                target = _tool_target_str(args)
                target_path = _path_from_arg(args, base_dir)
                if (
                    target_path is not None
                    and target_path.name == "learning_proposal.md"
                ) or "learning_proposal.md" in target:
                    last_learn_pos = pos
                elif (
                    artifact_dir is not None
                    and target_path is not None
                    and _is_within_dir(target_path, artifact_dir)
                ):
                    continue
                else:
                    has_task_work = True
                    last_work_pos = pos
                    is_planning = target_path is not None and (
                        (
                            base_dir is not None
                            and _is_within_dir(target_path, base_dir / ".planning")
                        )
                        or ".planning" in target_path.parts
                    )
                    if not is_planning:
                        edited_work_paths.append(target_path)
                    if target_path is not None and target_path.name == "task_plan.md":
                        touched_plans.append(target_path)
            elif name == "read_file":
                target = _tool_target_str(args)
                p = _path_from_arg(args, base_dir)
                if (
                    p is not None and p.name == "learning_proposal.md"
                ) or "learning_proposal.md" in target:
                    read_learning_proposal = True
                if p is not None and p.name == "task_plan.md":
                    touched_plans.append(p)
            elif name == "run_command" and isinstance(args, dict):
                cmd = _unquote_truncated(args.get("CommandLine"))
                if cmd:
                    if LEARN_CMD_RE.search(cmd) and _tool_call_succeeded_in_slice(
                        turn_entries, idx, entry, tc_idx, tc, full_records
                    ):
                        last_learn_pos = pos
                    wt_slugs, wt_spans = _extract_worktree_rm_slugs(cmd)
                    for slug in wt_slugs:
                        if slug not in closed_worktree_slugs:
                            closed_worktree_slugs.append(slug)
                    cmd_without_wt = _strip_spans(cmd, wt_spans)
                    if TASK_CMD_RE.search(cmd_without_wt):
                        has_task_work = True
                        last_work_pos = pos
                    ship_match = SHIP_CLOSEOUT_RE.search(cmd)
                    if ship_match:
                        ship_closeout = ship_match.group(1)
                        last_work_pos = pos

    already_learned = last_learn_pos > last_work_pos
    is_zero_error_learn_apply_docs_only = (
        bool(edited_work_paths)
        and all(_is_learn_doc_path(p) for p in edited_work_paths)
        and (
            read_learning_proposal
            or bool(LEARN_APPLY_RE.search(_extract_user_request(turn_user_text)))
        )
        and not has_turn_error
    )

    return (
        already_learned,
        has_task_work,
        touched_plans,
        closed_worktree_slugs,
        ship_closeout,
        is_zero_error_learn_apply_docs_only,
    )


def _resolve_base_dir(payload: dict[str, Any]) -> Path | None:
    cwd_raw = payload.get("cwd")
    if isinstance(cwd_raw, str) and cwd_raw.strip():
        return Path(os.path.expanduser(cwd_raw.strip()))
    wp_raw = payload.get("workspacePaths")
    if isinstance(wp_raw, list):
        for wp in wp_raw:
            if isinstance(wp, str) and wp.strip():
                return Path(os.path.expanduser(wp.strip()))
    return None


def _shared_review_cleared(
    shared_reviews: dict[str, tuple[str, str, int]],
    plan_key: str,
    digest: str,
) -> bool:
    entry = shared_reviews.get(plan_key)
    return (
        entry is not None
        and entry[0] == digest
        and entry[1] in ("proposed", "no_new_proposals", "applied")
    )


def _run_hook(payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_AUTO_LEARN") == "1":
        return {}
    if payload.get("terminationReason") != "NO_TOOL_CALL":
        return {}
    if payload.get("fullyIdle") is not True:
        return {}

    transcript_raw = payload.get("transcriptPath")
    if not isinstance(transcript_raw, str) or not transcript_raw.strip():
        return {}
    transcript_path = Path(os.path.expanduser(transcript_raw.strip()))
    if not transcript_path.is_file():
        return {}

    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").split(
            "\n"
        )
    except OSError:
        return {}

    artifact_raw = payload.get("artifactDirectoryPath")
    artifact_dir = (
        Path(os.path.expanduser(artifact_raw.strip()))
        if isinstance(artifact_raw, str) and artifact_raw.strip()
        else None
    )
    state_base = artifact_dir if artifact_dir is not None else transcript_path.parent
    state_path = state_base / ".post-task-learn-state.json"

    last_line, learned_plans, pending_proposal, owing_learn = _read_state(
        state_path, len(lines)
    )

    base_dir = _resolve_base_dir(payload)
    proposal_path = (
        artifact_dir / "learning_proposal.md" if artifact_dir is not None else None
    )
    cur_proposal_sha = _file_sha256(proposal_path)
    new_lines = lines[last_line:]
    shared_reviews = _read_shared_reviews()

    if payload.get("continuationActive") or payload.get("continuation_active"):
        if pending_proposal is not None:
            wrote_proposal = (
                bool(cur_proposal_sha)
                and cur_proposal_sha != pending_proposal["baseline_proposal_sha"]
            ) or _slice_wrote_learning_proposal(
                new_lines,
                base_dir,
                transcript_path=transcript_path,
                line_offset=last_line,
            )
            pending_key_str = str(pending_proposal.get("plan_key") or "")
            if pending_key_str.startswith("worktree:"):
                pending_slugs = [
                    s.strip()
                    for s in pending_key_str.split(":", 1)[1].split(",")
                    if s.strip()
                ]
                review_details = _read_shared_review_details()
                unreviewed_slugs = (
                    []
                    if wrote_proposal
                    else [
                        s
                        for s in pending_slugs
                        if not _worktree_slug_cleared_in_reviews(
                            s, review_details, learned_plans, owing_learn
                        )
                    ]
                )
                if not unreviewed_slugs or int(pending_proposal["retries"]) >= 1:
                    _write_state(
                        state_path, len(lines), learned_plans, None, owing_learn
                    )
                    return {}
                updated_wt = {
                    **pending_proposal,
                    "retries": int(pending_proposal["retries"]) + 1,
                }
                _write_state(
                    state_path, len(lines), learned_plans, updated_wt, owing_learn
                )
                slug_list = ", ".join(unreviewed_slugs)
                return {
                    "decision": "continue",
                    "reason": (
                        f"Post-task /learn worktree gate: worktree PWF task '{slug_list}' has no recorded review in reviews.jsonl. "
                        + BASE_TRIGGER_SUFFIX
                    ),
                }

            pending_plans = pending_proposal.get("pending_plans") or [
                {
                    "plan_key": pending_proposal["plan_key"],
                    "label": pending_proposal["label"],
                    "digest": pending_proposal["digest"],
                    "error_count": pending_proposal["error_count"],
                }
            ]
            if wrote_proposal:
                for item in pending_plans:
                    learned_plans[item["plan_key"]] = item["digest"]
                    owing_learn.pop(item["plan_key"], None)
                    prev_shared = shared_reviews.get(item["plan_key"])
                    if (
                        prev_shared is not None
                        and prev_shared[0] == item["digest"]
                        and prev_shared[1] == "owing_learn"
                    ):
                        _append_shared_review(
                            plan_key=item["plan_key"],
                            label=item["label"],
                            digest=item["digest"],
                            error_count=int(item["error_count"]),
                            outcome="proposed",
                            notes="Cleared owing_learn after learning_proposal.md update.",
                            transcript_path=transcript_path,
                            line_count=len(lines),
                            payload=payload,
                        )
                _write_state(state_path, len(lines), learned_plans, None, owing_learn)
                return {}

            remaining_plans: list[dict[str, Any]] = []
            for item in pending_plans:
                if _shared_review_cleared(
                    shared_reviews, item["plan_key"], item["digest"]
                ):
                    learned_plans[item["plan_key"]] = item["digest"]
                    owing_learn.pop(item["plan_key"], None)
                else:
                    remaining_plans.append(item)

            if not remaining_plans:
                _write_state(state_path, len(lines), learned_plans, None, owing_learn)
                return {}

            # 0-error plans prompt once and fast-exit cleanly WITHOUT pre-marking learned_plans
            if int(pending_proposal["error_count"]) == 0 and all(
                int(item.get("error_count", 0)) == 0 for item in remaining_plans
            ):
                _write_state(state_path, len(lines), learned_plans, None, owing_learn)
                return {}

            if int(pending_proposal["retries"]) >= 1:
                for item in remaining_plans:
                    owing_learn[item["plan_key"]] = item["digest"]
                    _append_owing_review(
                        plan_key=item["plan_key"],
                        label=item["label"],
                        digest=item["digest"],
                        error_count=int(item["error_count"]),
                        transcript_path=transcript_path,
                        line_count=len(lines),
                        payload=payload,
                    )
                _write_state(state_path, len(lines), learned_plans, None, owing_learn)
                return {}

            first_rem = remaining_plans[0]
            updated_pending = {
                "plan_key": first_rem["plan_key"],
                "label": ", ".join(p["label"] for p in remaining_plans),
                "digest": first_rem["digest"],
                "error_count": sum(int(p["error_count"]) for p in remaining_plans),
                "baseline_proposal_sha": pending_proposal["baseline_proposal_sha"],
                "retries": int(pending_proposal["retries"]) + 1,
                "pending_plans": remaining_plans,
            }
            _write_state(
                state_path, len(lines), learned_plans, updated_pending, owing_learn
            )
            reason = (
                f"Post-task /learn hard gate: completed plan '{updated_pending['label']}' "
                f"recorded {updated_pending['error_count']} error(s) in `## Errors Encountered`, "
                "but no review or proposal was recorded. Do NOT fast-exit — record via "
                "`mise run learn:record-review --plan-key <plan_path> --plan-sha256 <sha256>` "
                "(`--outcome no_new_proposals --survivor-count 0` if 0 survivors) "
                "or update `learning_proposal.md` (`RequestFeedback=false`) evaluating Tier 1-5 extraction."
            )
            return {"decision": "continue", "reason": reason}
        _write_state(state_path, len(lines), learned_plans, None, owing_learn)
        return {}

    last_user_input = payload.get("lastUserInput")
    user_text = last_user_input if isinstance(last_user_input, str) else ""
    (
        already_learned,
        has_task_work,
        touched_plans,
        closed_worktree_slugs,
        ship_closeout,
        is_zero_error_learn_apply_docs_only,
    ) = _analyze_slice(
        new_lines,
        user_text,
        base_dir,
        artifact_dir,
        transcript_path=transcript_path,
        line_offset=last_line,
    )

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
            if _shared_review_cleared(shared_reviews, plan_key, digest):
                learned_plans[plan_key] = digest
                owing_learn.pop(plan_key, None)
                continue
            if learned_plans.get(plan_key) != digest:
                completed_unlearned.append(
                    (plan_path, plan_key, digest, _count_plan_errors(text))
                )
        else:
            has_incomplete_plan = True

    if already_learned:
        wrote_proposal = _slice_wrote_learning_proposal(
            new_lines,
            base_dir,
            transcript_path=transcript_path,
            line_offset=last_line,
        )
        explicit_learn = bool(user_text and _is_explicit_user_learn(user_text))
        still_unlearned: list[tuple[Path, str, str, int]] = []
        for plan_path, plan_key, digest, err_count in completed_unlearned:
            if (
                wrote_proposal
                or explicit_learn
                or _shared_review_cleared(shared_reviews, plan_key, digest)
            ):
                learned_plans[plan_key] = digest
                owing_learn.pop(plan_key, None)
                prev_shared = shared_reviews.get(plan_key)
                if (
                    wrote_proposal
                    and prev_shared is not None
                    and prev_shared[0] == digest
                    and prev_shared[1] == "owing_learn"
                ):
                    _append_shared_review(
                        plan_key=plan_key,
                        label=_plan_label(plan_path),
                        digest=digest,
                        error_count=err_count,
                        outcome="proposed",
                        notes="Cleared owing_learn after learning_proposal.md update.",
                        transcript_path=transcript_path,
                        line_count=len(lines),
                        payload=payload,
                    )
            else:
                still_unlearned.append((plan_path, plan_key, digest, err_count))
        completed_unlearned = still_unlearned

        unreviewed_closed: list[str] = []
        if closed_worktree_slugs:
            review_details = _read_shared_review_details()
            unreviewed_closed = [
                slug
                for slug in closed_worktree_slugs
                if not (
                    wrote_proposal
                    or explicit_learn
                    or _worktree_slug_cleared_in_reviews(
                        slug, review_details, learned_plans, owing_learn
                    )
                )
            ]
        if not completed_unlearned and not unreviewed_closed:
            _write_state(state_path, len(lines), learned_plans, None, owing_learn)
            return {}

    if is_zero_error_learn_apply_docs_only and all(
        err == 0 for _, _, _, err in completed_unlearned
    ):
        for _, plan_key, digest, _ in completed_unlearned:
            learned_plans[plan_key] = digest
            owing_learn.pop(plan_key, None)
        _write_state(state_path, len(lines), learned_plans, None, owing_learn)
        return {}

    if completed_unlearned:
        ordered = sorted(completed_unlearned, key=lambda item: (item[3] == 0, item[1]))
        error_plans = [item for item in ordered if item[3] > 0]
        if error_plans:
            # Do NOT pre-mark 0-error plans before /learn runs; enforce error plans first.
            err_labels = ", ".join(_plan_label(p) for p, _, _, _ in error_plans)
            total_errors = sum(err for _, _, _, err in error_plans)
            first_path, first_key, first_digest, _ = error_plans[0]
            pending_plans = [
                {
                    "plan_key": pk,
                    "label": _plan_label(p),
                    "digest": dig,
                    "error_count": err,
                }
                for p, pk, dig, err in error_plans
            ]
            next_pending = {
                "plan_key": first_key,
                "label": err_labels
                if len(error_plans) > 1
                else _plan_label(first_path),
                "digest": first_digest,
                "error_count": total_errors,
                "baseline_proposal_sha": cur_proposal_sha,
                "retries": 0,
                "pending_plans": pending_plans,
            }
            _write_state(
                state_path, len(lines), learned_plans, next_pending, owing_learn
            )
            reason = (
                f"Automated post-task /learn check: planning-with-files task completed ({err_labels}) "
                f"with {total_errors} logged error(s) in `## Errors Encountered`. "
                + BASE_TRIGGER_SUFFIX
                + " Review the completed `task_plan.md` (`Errors Encountered`, `Decisions Made`), "
                "`findings.md`, and `progress.md` alongside user messages — record review (`no_new_proposals` "
                "for 0 survivors or `propose` + `render-proposal` with `RequestFeedback=false`)."
            )
            return {"decision": "continue", "reason": reason}

        labels = ", ".join(_plan_label(p) for p, _, _, _ in ordered)
        first_path, first_key, first_digest, _ = ordered[0]
        clean_pending = {
            "plan_key": first_key,
            "label": labels if len(ordered) > 1 else _plan_label(first_path),
            "digest": first_digest,
            "error_count": 0,
            "baseline_proposal_sha": cur_proposal_sha,
            "retries": 0,
            "pending_plans": [
                {
                    "plan_key": pk,
                    "label": _plan_label(p),
                    "digest": dig,
                    "error_count": 0,
                }
                for p, pk, dig, _ in ordered
            ],
        }
        _write_state(state_path, len(lines), learned_plans, clean_pending, owing_learn)
        reason = (
            f"Automated post-task /learn check: planning-with-files task completed ({labels}). "
            + BASE_TRIGGER_SUFFIX
            + " Review the completed `task_plan.md` (`Errors Encountered`, `Decisions Made`), `findings.md`, and `progress.md` alongside user messages."
        )
        return {"decision": "continue", "reason": reason}

    if closed_worktree_slugs:
        review_details = _read_shared_review_details()
        unreviewed_slugs = [
            slug
            for slug in closed_worktree_slugs
            if not _worktree_slug_cleared_in_reviews(
                slug, review_details, learned_plans, owing_learn
            )
        ]
        if unreviewed_slugs:
            slug_list = ", ".join(unreviewed_slugs)
            wt_pending = {
                "plan_key": f"worktree:{slug_list}",
                "label": slug_list,
                "digest": "",
                "error_count": 0,
                "baseline_proposal_sha": cur_proposal_sha,
                "retries": 0,
            }
            _write_state(state_path, len(lines), learned_plans, wt_pending, owing_learn)
            reason = (
                f"Automated post-task /learn check: worktree PWF task '{slug_list}' finished. "
                + BASE_TRIGGER_SUFFIX
            )
            return {"decision": "continue", "reason": reason}

    _write_state(state_path, len(lines), learned_plans, None, owing_learn)

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


def _find_agy_learn_runner(
    payload: dict[str, Any],
) -> tuple[Path, Path, Path] | None:
    candidates: list[Path] = []
    base = _resolve_base_dir(payload)
    if base is not None:
        candidates.append(base)
    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if (
            parent / "unix" / "home" / ".config" / "mise" / "lib" / "agy_learn"
        ).is_dir():
            candidates.append(parent)
            break
    for cand in candidates:
        lib_dir = cand / "unix" / "home" / ".config" / "mise" / "lib"
        if not (lib_dir / "agy_learn").is_dir():
            continue
        venv_py = cand / ".venv" / "bin" / "python"
        if venv_py.is_file():
            return venv_py, lib_dir, cand
        try:
            proc = subprocess.run(
                ["git", "-C", str(cand), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            common_raw = proc.stdout.strip() if proc.returncode == 0 else ""
            if common_raw:
                common_path = Path(common_raw)
                if not common_path.is_absolute():
                    common_path = (cand / common_path).resolve()
                common_venv = common_path.parent / ".venv" / "bin" / "python"
                if common_venv.is_file():
                    return common_venv, lib_dir, cand
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _stdlib_heal_summary() -> dict[str, Any]:
    state_dir = _shared_state_dir()
    reviews_path = state_dir / "reviews.jsonl"
    reviews: dict[str, tuple[str, str, int]] = {}
    parse_errors = 0
    hook_drift_keys: list[str] = []
    if reviews_path.is_file():
        try:
            for raw in reviews_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue
                if not isinstance(rec, dict) or not isinstance(
                    rec.get("plan_key"), str
                ):
                    parse_errors += 1
                    continue
                raw_k = rec["plan_key"]
                ts_ns = rec.get("timestamp_unix_ns")
                ns_val = (
                    ts_ns
                    if isinstance(ts_ns, int) and not isinstance(ts_ns, bool)
                    else 0
                )
                norm_k = (
                    str(Path(os.path.expanduser(raw_k)).resolve(strict=False))
                    if ("/" in raw_k or "\\" in raw_k or raw_k.endswith("task_plan.md"))
                    else raw_k
                )
                prev = reviews.get(norm_k)
                if prev is None or ns_val >= prev[2]:
                    reviews[norm_k] = (
                        str(rec.get("plan_sha256") or ""),
                        str(rec.get("outcome") or ""),
                        ns_val,
                    )
        except OSError:
            parse_errors += 1

    for rk in sorted(reviews):
        if "/" not in rk and "\\" not in rk and not rk.endswith("task_plan.md"):
            hook_drift_keys.append(rk)

    owing = [
        k for k, (_, outcome, _) in sorted(reviews.items()) if outcome == "owing_learn"
    ]
    proposals_path = state_dir / "proposals.jsonl"
    active_count = 0
    escalated_ids: list[str] = []
    declined_expired_ids: list[str] = []
    now_ns = time.time_ns()
    if proposals_path.is_file():
        latest_props: dict[str, tuple[int, dict[str, Any]]] = {}
        try:
            for raw in proposals_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("proposal_id"), str):
                    pid = obj["proposal_id"]
                    ts_ns = obj.get("timestamp_unix_ns")
                    ns_val = (
                        ts_ns
                        if isinstance(ts_ns, int) and not isinstance(ts_ns, bool)
                        else 0
                    )
                    prev = latest_props.get(pid)
                    if prev is None or ns_val >= prev[0]:
                        latest_props[pid] = (ns_val, obj)
                else:
                    parse_errors += 1
        except OSError:
            parse_errors += 1
        for pid, (_, prop) in sorted(latest_props.items()):
            status = prop.get("status")
            if status == "declined":
                until = prop.get("declined_until_unix_ns")
                if (
                    isinstance(until, int)
                    and not isinstance(until, bool)
                    and now_ns >= until
                ):
                    declined_expired_ids.append(pid)
            elif status in ("pending", "approved"):
                active_count += 1
                try:
                    occ = int(prop.get("occurrences") or 1)
                except (TypeError, ValueError):
                    occ = 1
                orig_tier = prop.get("tier")
                rec_tier = prop.get("recommended_tier")
                if occ >= 2 and (
                    rec_tier in ("skill", "rule")
                    or (orig_tier in ("skill", "rule") and rec_tier != orig_tier)
                ):
                    escalated_ids.append(pid)
    findings = [{"kind": "hook_drift", "subject": k} for k in hook_drift_keys] + [
        {"kind": "declined_expired", "subject": pid} for pid in declined_expired_ids
    ]
    errors = (
        [
            {
                "code": "ERR_PARSE_FAILURE",
                "message": f"{parse_errors} corrupt JSONL line(s)",
            }
        ]
        if parse_errors
        else []
    )
    return {
        "healthy": len(owing) == 0 and parse_errors == 0,
        "owing_plans": owing,
        "active_proposals_count": active_count,
        "auto_escalated_ids": escalated_ids,
        "already_applied_ids": [],
        "proposal_md_within_budget": True,
        "findings": findings,
        "errors": errors,
    }


def _run_session_start(payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_AUTO_LEARN") == "1":
        return {}
    report: dict[str, Any] | None = None
    runner = _find_agy_learn_runner(payload)
    if runner is not None:
        venv_py, lib_dir, repo_root = runner
        repo_arg = str(repo_root)
        cmd = [
            str(venv_py),
            "-I",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); from agy_learn.cli import main; raise SystemExit(main(sys.argv[2:]))",
            str(lib_dir),
            "heal",
            "--check",
            "--json",
            "--repo",
            repo_arg,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            if proc.stdout.strip():
                parsed = json.loads(proc.stdout.strip().splitlines()[-1])
                if isinstance(parsed, dict):
                    report = parsed
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            sys.stderr.write(
                f"post-task-learn: session-start heal runner failed ({exc}); using stdlib fallback\n"
            )
            report = None

    if report is None:
        report = _stdlib_heal_summary()

    owing = report.get("owing_plans") or []
    active_raw = report.get("active_proposals")
    active_count = (
        len(active_raw)
        if isinstance(active_raw, list)
        else int(report.get("active_proposals_count") or 0)
    )
    escalated = report.get("auto_escalated_ids") or []
    applied = report.get("already_applied_ids") or []
    within_budget = report.get("proposal_md_within_budget", True)
    findings = report.get("findings") or []
    hook_drift = [
        f for f in findings if isinstance(f, dict) and f.get("kind") == "hook_drift"
    ]
    declined_expired = [
        f
        for f in findings
        if isinstance(f, dict) and f.get("kind") == "declined_expired"
    ]
    other_errors = [
        e
        for e in (report.get("errors") or [])
        if isinstance(e, dict) and e.get("code") != "ERR_OWING_LEARN"
    ]

    parts: list[str] = []
    if owing:
        parts.append(
            f"owing_learn={len(owing)} ({', '.join(str(x) for x in owing[:3])})"
        )
    if hook_drift:
        parts.append(f"hook_drift={len(hook_drift)}")
    if other_errors:
        parts.append(f"errors={len(other_errors)}")
    if escalated:
        parts.append(f"auto_escalate={len(escalated)}")
    if declined_expired:
        parts.append(f"declined_expired={len(declined_expired)}")
    if active_count:
        parts.append(f"active_proposals={active_count}")
    if applied:
        parts.append(f"already_applied={len(applied)}")
    if not within_budget:
        parts.append("learning_proposal.md>8KB")

    if not parts:
        return {}

    msg = (
        "[agy-learn] Read-only heal summary: "
        + "; ".join(parts)
        + ". Run `mise run learn:heal --check` for details (read-only; no files modified)."
    )
    return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}


def _check_hooks_json(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "post-task-learn" not in data:
        raise AssertionError(f"missing 'post-task-learn' in {path}")
    block = data["post-task-learn"]
    if not isinstance(block, dict):
        raise AssertionError(f"invalid post-task-learn block in {path}")

    for event_key in ("SessionStart", "Stop"):
        handlers = block.get(event_key)
        if not isinstance(handlers, list) or not handlers:
            raise AssertionError(
                f"missing {event_key} handlers for post-task-learn in {path}"
            )
        cmd = handlers[0].get("command", "") if isinstance(handlers[0], dict) else ""
        if not isinstance(cmd, str) or "post-task-learn.py" not in cmd:
            raise AssertionError(f"unexpected {event_key} hook command: {cmd!r}")
        if event_key == "SessionStart" and "--session-start" not in cmd:
            raise AssertionError(
                f"SessionStart handler must pass --session-start, got: {cmd!r}"
            )
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
        for k in (
            "PLAN_ID",
            "PWF_PLAN_ROOT",
            "AGY_DISABLE_AUTO_LEARN",
            "XDG_STATE_HOME",
        )
    }
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["XDG_STATE_HOME"] = str(root / "xdg-state")

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
            # 0-error plans are NOT pre-marked before /learn runs; turn 2 still prompts until recorded
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
            assert _run_hook(p3).get("decision") == "continue"
            # Once recorded in reviews.jsonl, digest dedupes cleanly
            rev3 = Path(os.environ["XDG_STATE_HOME"]) / "agy-learn" / "reviews.jsonl"
            rev3.parent.mkdir(parents=True, exist_ok=True)
            rev3.write_text(
                json.dumps(
                    {
                        "plan_key": str(plan_file.resolve()),
                        "plan_sha256": hashlib.sha256(
                            plan_file.read_bytes()
                        ).hexdigest(),
                        "outcome": "no_new_proposals",
                        "timestamp_unix_ns": 100,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            _write_jsonl(
                t3,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Next2</USER_REQUEST>",
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
            assert "RequestFeedback=true" not in res9b.get("reason", ""), (
                "hard gate reason must not request RequestFeedback=true"
            )
            res9c = _run_hook(dict(p9, continuationActive=True))
            assert res9c == {}, f"case 9c bounded retry failed: {res9c}"
            state9 = json.loads(
                (art9 / ".post-task-learn-state.json").read_text(encoding="utf-8")
            )
            p9_key = str((pdir9 / "task_plan.md").resolve())
            assert p9_key not in state9["learned_plans"], (
                f"retry exhaustion must NOT silently mark learned: {state9}"
            )
            assert p9_key in state9.get("owing_learn", {}), (
                f"retry exhaustion must mark owing_learn: {state9}"
            )
            shared9 = _read_shared_reviews()
            assert shared9.get(p9_key, ("", "", 0))[1] == "owing_learn", (
                f"retry exhaustion must append owing_learn to reviews.jsonl: {shared9}"
            )
            checks.append("errors_encountered_hard_gate_marks_owing_learn")

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

            # 12. already_learned persists completed plan digest on proactive learning_proposal.md edit,
            #     while skill-read-only on an error-logging plan does NOT persist digest
            ws12a = root / "ws12a"
            pdir12a = ws12a / ".planning" / "proactive12"
            pdir12a.mkdir(parents=True)
            plan12a = pdir12a / "task_plan.md"
            plan12a.write_text(
                "# Proactive12\n\n### Phase 1: Do\n**Status:** complete\n\n"
                "## Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| Bug D | Fixed D |\n",
                encoding="utf-8",
            )
            t12a = root / "t12a.jsonl"
            art12a = root / "art12a"
            art12a.mkdir()
            _write_jsonl(
                t12a,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {"name": "edit_file", "args": {"Path": str(plan12a)}}
                        ],
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "write_file",
                                "args": {"Path": str(art12a / "learning_proposal.md")},
                            }
                        ],
                    },
                ],
            )
            p12a = {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(t12a),
                "artifactDirectoryPath": str(art12a),
                "cwd": str(ws12a),
            }
            assert _run_hook(p12a) == {}, (
                "case 12a initial proactive learn should allow"
            )
            state12a = json.loads(
                (art12a / ".post-task-learn-state.json").read_text(encoding="utf-8")
            )
            assert (
                state12a["learned_plans"].get(str(plan12a.resolve()))
                == hashlib.sha256(plan12a.read_bytes()).hexdigest()
            ), f"case 12a failed to persist digest: {state12a}"
            _write_jsonl(
                t12a,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Next</USER_REQUEST>",
                    }
                ],
            )
            assert _run_hook(p12a) == {}, "case 12a next turn should remain deduped"

            ws12b = root / "ws12b"
            pdir12b = ws12b / ".planning" / "skillread12"
            pdir12b.mkdir(parents=True)
            plan12b = pdir12b / "task_plan.md"
            plan12b.write_text(
                "# SkillRead12\n\n### Phase 1: Do\n**Status:** complete\n\n"
                "## Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| Bug E | Fixed E |\n",
                encoding="utf-8",
            )
            t12b = root / "t12b.jsonl"
            art12b = root / "art12b"
            art12b.mkdir()
            _write_jsonl(
                t12b,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
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
            p12b = {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(t12b),
                "artifactDirectoryPath": str(art12b),
                "cwd": str(ws12b),
            }
            # Reading SKILL.md alone MUST trigger the hard gate immediately on turn 1
            res12b_turn1 = _run_hook(p12b)
            assert res12b_turn1.get("decision") == "continue" and (
                "1 logged error(s)" in res12b_turn1.get("reason", "")
            ), f"case 12b skill-read must trigger hard gate immediately: {res12b_turn1}"
            state12b = json.loads(
                (art12b / ".post-task-learn-state.json").read_text(encoding="utf-8")
            )
            assert str(plan12b.resolve()) not in state12b["learned_plans"], (
                f"case 12b skill-read must not persist error plan digest: {state12b}"
            )
            # Failing CLI learn:record-review with Command exited with code 1 must NOT bypass
            t12c = root / "t12c.jsonl"
            art12c = root / "art12c"
            art12c.mkdir()
            _write_jsonl(
                t12c,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "step_index": 1,
                        "tool_calls": [
                            {
                                "name": "run_command",
                                "args": {
                                    "CommandLine": "mise run learn:record-review --plan-key foo --plan-sha256 bar --outcome no_new_proposals"
                                },
                            }
                        ],
                    },
                    {
                        "type": "GENERIC",
                        "step_index": 2,
                        "status": "DONE",
                        "content": "Command exited with code 1.",
                    },
                ],
            )
            res12c = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t12c),
                    "artifactDirectoryPath": str(art12c),
                    "cwd": str(ws12b),
                }
            )
            assert res12c.get("decision") == "continue", (
                f"case 12c failed learn CLI must not satisfy already_learned: {res12c}"
            )
            checks.append("already_learned_persists_completed_plan_digest")

            # 13. mise.local.toml PLAN_ID pin overrides stale inherited os.environ["PLAN_ID"]
            ws13 = root / "ws13"
            stale_dir13 = ws13 / ".planning" / "stale-env"
            stale_dir13.mkdir(parents=True)
            (stale_dir13 / "task_plan.md").write_text(
                "# Stale\n\n### Phase 1: Do\n**Status:** in_progress\n",
                encoding="utf-8",
            )
            local_dir13 = ws13 / ".planning" / "local-pin"
            local_dir13.mkdir(parents=True)
            (local_dir13 / "task_plan.md").write_text(
                "# Local\n\n### Phase 1: Do\n**Status:** complete\n",
                encoding="utf-8",
            )
            (ws13 / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "local-pin"\n', encoding="utf-8"
            )
            t13 = root / "t13.jsonl"
            art13 = root / "art13"
            art13.mkdir()
            _write_jsonl(
                t13,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    }
                ],
            )
            os.environ["PLAN_ID"] = "stale-env"
            try:
                res13 = _run_hook(
                    {
                        "terminationReason": "NO_TOOL_CALL",
                        "fullyIdle": True,
                        "transcriptPath": str(t13),
                        "artifactDirectoryPath": str(art13),
                        "cwd": str(ws13),
                    }
                )
            finally:
                os.environ.pop("PLAN_ID", None)
            assert (
                res13.get("decision") == "continue"
                and "local-pin" in res13.get("reason", "")
                and "stale-env" not in res13.get("reason", "")
            ), f"case 13 failed: {res13}"
            checks.append("mise_local_overrides_stale_env_plan_id")

            # 14. Zero-error docs-only /learn-apply PR skips & persists zero-error plan digest
            ws14 = root / "ws14"
            pdir14 = ws14 / ".planning" / "2026-11-23-learn-apply"
            pdir14.mkdir(parents=True)
            plan14 = pdir14 / "task_plan.md"
            plan14.write_text(
                "# Apply14\n\n### Phase 1: Apply\n**Status:** complete\n\n"
                "## Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| None yet | - |\n",
                encoding="utf-8",
            )
            (ws14 / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "2026-11-23-learn-apply"\n', encoding="utf-8"
            )
            t14 = root / "t14.jsonl"
            art14 = root / "art14"
            art14.mkdir()
            _write_jsonl(
                t14,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Apply approved Proposal 7 from learning_proposal.md</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "read_file",
                                "args": {"Path": str(art14 / "learning_proposal.md")},
                            },
                            {
                                "name": "edit_file",
                                "args": {"Path": str(ws14 / "skills/demo/SKILL.md")},
                            },
                            {
                                "name": "edit_file",
                                "args": {"Path": str(ws14 / "rules/demo.md")},
                            },
                            {
                                "name": "edit_file",
                                "args": {"Path": str(plan14)},
                            },
                            {
                                "name": "run_command",
                                "args": {
                                    "CommandLine": "git commit -m 'docs: apply learn'"
                                },
                            },
                        ],
                    },
                    {
                        "type": "GENERIC",
                        "content": "Command exited with code 0.\n",
                    },
                ],
            )
            res14 = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t14),
                    "artifactDirectoryPath": str(art14),
                    "cwd": str(ws14),
                    "lastUserInput": "<USER_REQUEST>Apply approved Proposal 7 from learning_proposal.md</USER_REQUEST>",
                }
            )
            assert res14 == {}, f"case 14 failed: {res14}"
            state14 = json.loads(
                (art14 / ".post-task-learn-state.json").read_text(encoding="utf-8")
            )
            assert (
                state14["learned_plans"].get(str(plan14.resolve()))
                == hashlib.sha256(plan14.read_bytes()).hexdigest()
            ), f"case 14 failed to persist zero-error plan digest: {state14}"
            checks.append("zero_error_docs_only_learn_apply_skips")

            # 15. Docs-only /learn-apply PR with turn error OR logged plan error still triggers
            t15a = root / "t15a.jsonl"
            art15a = root / "art15a"
            art15a.mkdir()
            _write_jsonl(
                t15a,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Apply approved Proposal 7 from learning_proposal.md</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "read_file",
                                "args": {"Path": str(art15a / "learning_proposal.md")},
                            },
                            {
                                "name": "edit_file",
                                "args": {"Path": str(ws1 / "skills/demo/SKILL.md")},
                            },
                        ],
                    },
                    {
                        "type": "GENERIC",
                        "content": "Command exited with code 1.\nError: agnix failed",
                    },
                ],
            )
            res15a = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t15a),
                    "artifactDirectoryPath": str(art15a),
                    "cwd": str(ws1),
                }
            )
            assert res15a.get("decision") == "continue", (
                f"case 15a turn error should trigger: {res15a}"
            )

            ws15b = root / "ws15b"
            pdir15b = ws15b / ".planning" / "2026-11-23-learn-err"
            pdir15b.mkdir(parents=True)
            (pdir15b / "task_plan.md").write_text(
                "# Err15b\n\n### Phase 1: Apply\n**Status:** complete\n\n"
                "## Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| agnix XP-SK-002 | Fixed |\n",
                encoding="utf-8",
            )
            t15b = root / "t15b.jsonl"
            art15b = root / "art15b"
            art15b.mkdir()
            _write_jsonl(
                t15b,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Apply approved Proposal 7 from learning_proposal.md</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "edit_file",
                                "args": {"Path": str(ws15b / "skills/demo/SKILL.md")},
                            }
                        ],
                    },
                ],
            )
            res15b = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t15b),
                    "artifactDirectoryPath": str(art15b),
                    "cwd": str(ws15b),
                }
            )
            assert res15b.get("decision") == "continue", (
                f"case 15b plan error should trigger: {res15b}"
            )
            checks.append("docs_only_learn_apply_with_error_triggers")

            # 16. Zero-error /learn-apply PR editing code/hooks still triggers
            t16 = root / "t16.jsonl"
            art16 = root / "art16"
            art16.mkdir()
            _write_jsonl(
                t16,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Apply learning_proposal.md</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "read_file",
                                "args": {"Path": str(art16 / "learning_proposal.md")},
                            },
                            {
                                "name": "edit_file",
                                "args": {"Path": str(ws1 / "hooks/post-task-learn.py")},
                            },
                        ],
                    },
                ],
            )
            res16 = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t16),
                    "artifactDirectoryPath": str(art16),
                    "cwd": str(ws1),
                }
            )
            assert res16.get("decision") == "continue", (
                f"case 16 non-doc edit should trigger: {res16}"
            )
            checks.append("non_doc_learn_apply_triggers")

            # 17. transcript_full.jsonl hydration + truncated tool_calls unquoting
            t17 = root / "t17.jsonl"
            t17_full = root / "transcript_full.jsonl"
            art17 = root / "art17"
            art17.mkdir()
            _write_jsonl(
                t17,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Work</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [],
                        "truncated_fields": ["tool_calls"],
                    },
                ],
            )
            _write_jsonl(
                t17_full,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Work</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "edit_file",
                                "args": {"Path": f'"{ws1 / "hydrated.py"}"'},
                            }
                        ],
                    },
                ],
            )
            res17a = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t17),
                    "artifactDirectoryPath": str(art17),
                    "cwd": str(ws1),
                }
            )
            assert res17a.get("decision") == "continue", (
                f"case 17a hydrated edit_file should trigger: {res17a}"
            )
            checks.append("transcript_full_hydration_and_unquoting")

            # 18. Automated Stop hook reasons never ask for RequestFeedback=true
            assert "RequestFeedback=true" not in BASE_TRIGGER_SUFFIX
            assert "RequestFeedback=true" not in TRIGGER_REASON
            checks.append("no_request_feedback_true_in_auto_reasons")

            # 19. Multi-plan completion does NOT silently bypass completed_unlearned[1:]
            ws19 = root / "ws19"
            p19a = ws19 / ".planning" / "2026-11-23-a-clean"
            p19b = ws19 / ".planning" / "2026-11-23-b-errors"
            p19a.mkdir(parents=True)
            p19b.mkdir(parents=True)
            plan19a = p19a / "task_plan.md"
            plan19b = p19b / "task_plan.md"
            plan19a.write_text(
                "# Clean\n\n### Phase 1: Do\n**Status:** complete\n", encoding="utf-8"
            )
            plan19b.write_text(
                "# Err\n\n### Phase 1: Do\n**Status:** complete\n\n"
                "## Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| Multi bug | Fixed |\n",
                encoding="utf-8",
            )
            t19 = root / "t19.jsonl"
            art19 = root / "art19"
            art19.mkdir()
            _write_jsonl(
                t19,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {"name": "edit_file", "args": {"Path": str(plan19a)}},
                            {"name": "edit_file", "args": {"Path": str(plan19b)}},
                        ],
                    },
                ],
            )
            res19 = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t19),
                    "artifactDirectoryPath": str(art19),
                    "cwd": str(ws19),
                }
            )
            assert res19.get(
                "decision"
            ) == "continue" and "1 logged error(s)" in res19.get("reason", ""), (
                f"case 19 failed: {res19}"
            )
            state19 = json.loads(
                (art19 / ".post-task-learn-state.json").read_text(encoding="utf-8")
            )
            assert str(plan19b.resolve()) not in state19["learned_plans"], (
                f"case 19 bypassed second error plan: {state19}"
            )
            checks.append("multi_plan_no_silent_bypass")

            # 20. workspacePaths[0] fallback, .archive exclusion, and ### Errors Encountered boundary
            ws20 = root / "ws20"
            arch20 = ws20 / ".planning" / ".archive"
            arch20.mkdir(parents=True)
            (arch20 / "task_plan.md").write_text(
                "# Archived\n\n### Phase 1: Old\n**Status:** complete\n",
                encoding="utf-8",
            )
            p20 = ws20 / ".planning" / "active20"
            p20.mkdir(parents=True)
            (p20 / "task_plan.md").write_text(
                "# Active20\n\n### Phase 1: Do\n**Status:** complete\n\n"
                "### Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| None yet | - |\n\n"
                "### Later Notes\n| Key | Val |\n|---|---|\n| Foo | Bar |\n",
                encoding="utf-8",
            )
            assert (
                _count_plan_errors((p20 / "task_plan.md").read_text(encoding="utf-8"))
                == 0
            )
            t20 = root / "t20.jsonl"
            art20 = root / "art20"
            art20.mkdir()
            _write_jsonl(
                t20,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    }
                ],
            )
            res20 = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t20),
                    "artifactDirectoryPath": str(art20),
                    "workspacePaths": [str(ws20)],
                }
            )
            assert res20.get("decision") == "continue" and "active20" in res20.get(
                "reason", ""
            ), f"case 20 failed: {res20}"
            checks.append("workspace_paths_archive_exclusion_and_h3_errors_stop")

            # 21. Shared reviews.jsonl no_new_proposals satisfies hard gate
            ws21 = root / "ws21"
            p21 = ws21 / ".planning" / "rev21"
            p21.mkdir(parents=True)
            plan21 = p21 / "task_plan.md"
            plan21.write_text(
                "# Rev21\n\n### Phase 1: Do\n**Status:** complete\n\n"
                "## Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| Bug Z | Fixed Z |\n",
                encoding="utf-8",
            )
            t21 = root / "t21.jsonl"
            art21 = root / "art21"
            art21.mkdir()
            _write_jsonl(
                t21,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    }
                ],
            )
            p21_payload = {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(t21),
                "artifactDirectoryPath": str(art21),
                "cwd": str(ws21),
            }
            assert _run_hook(p21_payload).get("decision") == "continue"
            sha21 = hashlib.sha256(plan21.read_bytes()).hexdigest()
            with _shared_reviews_path().open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "review_id": "rev-test21",
                            "plan_key": str(plan21.resolve()),
                            "plan_sha256": sha21,
                            "outcome": "no_new_proposals",
                            "timestamp_unix_ns": time.time_ns(),
                        }
                    )
                    + "\n"
                )
            assert _run_hook(dict(p21_payload, continuationActive=True)) == {}, (
                "case 21 shared no_new_proposals review should satisfy hard gate"
            )
            checks.append("shared_zero_survivor_review_clears_hard_gate")

            # 22. SessionStart surfaces owing_learn read-only via injectSteps
            ss_out = _run_session_start({"cwd": str(ws9)})
            assert ss_out.get("injectSteps"), (
                f"case 22 SessionStart should surface owing_learn from case 9: {ss_out}"
            )
            assert "auto_escalated_ids" in _stdlib_heal_summary()
            checks.append("session_start_surfaces_owing_learn_read_only")

            # 23. Failed learn CLI run_command in Stop continuation does not bypass _slice_wrote_learning_proposal
            ws23 = root / "ws23"
            p23 = ws23 / ".planning" / "rev23"
            p23.mkdir(parents=True)
            plan23 = p23 / "task_plan.md"
            plan23.write_text(
                "# Rev23\n\n### Phase 1: Do\n**Status:** complete\n\n"
                "## Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| Bug23 | Fixed |\n",
                encoding="utf-8",
            )
            t23 = root / "t23.jsonl"
            art23 = root / "art23"
            art23.mkdir()
            _write_jsonl(
                t23,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    }
                ],
            )
            p23_payload = {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(t23),
                "artifactDirectoryPath": str(art23),
                "cwd": str(ws23),
            }
            assert _run_hook(p23_payload).get("decision") == "continue"
            _write_jsonl(
                t23,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Done</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "run_command",
                                "args": {
                                    "CommandLine": "mise run learn:propose --tier skill"
                                },
                            }
                        ],
                    },
                ],
            )
            res23_cont = _run_hook(dict(p23_payload, continuationActive=True))
            assert res23_cont.get("decision") == "continue", (
                f"case 23 unrecorded learn CLI cmd must NOT clear hard gate: {res23_cont}"
            )
            checks.append("learn_cmd_without_review_does_not_bypass_hard_gate")

            # 24. WORKTREE_RM_RE handles --force before/after slug and same-turn learn:record-review -> worktree:rm does not double-trigger
            assert (
                _extract_worktree_rm_slug("mise run worktree:rm --force feat-a")
                == "feat-a"
            )
            assert (
                _extract_worktree_rm_slug(
                    "mise -C /tmp/r run worktree:rm -f feat_b --force"
                )
                == "feat_b"
            )
            with _shared_reviews_path().open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "review_id": "rev-test24",
                            "plan_key": "/tmp/x/task_plan.md",
                            "plan_sha256": "abc",
                            "worktree_slug": "feat-a",
                            "outcome": "no_new_proposals",
                            "timestamp_unix_ns": time.time_ns(),
                        }
                    )
                    + "\n"
                )
            t24 = root / "t24.jsonl"
            art24 = root / "art24"
            art24.mkdir()
            _write_jsonl(
                t24,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Finish wt</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "edit_file",
                                "args": {"Path": str(ws1 / "src.py")},
                            },
                            {
                                "name": "run_command",
                                "args": {
                                    "CommandLine": "mise run learn:record-review --plan-key /tmp/x/task_plan.md --plan-sha256 abc --outcome no_new_proposals --survivor-count 0 --worktree-slug feat-a"
                                },
                            },
                            {
                                "name": "run_command",
                                "args": {
                                    "CommandLine": "mise run worktree:rm --force feat-a"
                                },
                            },
                        ],
                    },
                ],
            )
            res24 = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t24),
                    "artifactDirectoryPath": str(art24),
                    "cwd": str(ws1),
                }
            )
            assert res24 == {}, (
                f"case 24 same-turn learn -> worktree:rm must not double-trigger: {res24}"
            )
            checks.append("same_turn_learn_then_worktree_rm_no_double_trigger")

            # 25. Cross-turn worktree:rm is suppressed when shared reviews.jsonl cleared the slug, and still fires when unreviewed
            t25a = root / "t25a.jsonl"
            art25a = root / "art25a"
            art25a.mkdir()
            _write_jsonl(
                t25a,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Remove worktree</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "run_command",
                                "args": {
                                    "CommandLine": "mise run worktree:rm --force rev21"
                                },
                            }
                        ],
                    },
                ],
            )
            res25a = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t25a),
                    "artifactDirectoryPath": str(art25a),
                    "cwd": str(ws1),
                }
            )
            assert res25a == {}, (
                f"case 25a cleared shared review must suppress worktree:rm trigger: {res25a}"
            )

            t25b = root / "t25b.jsonl"
            art25b = root / "art25b"
            art25b.mkdir()
            _write_jsonl(
                t25b,
                [
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>Remove unreviewed</USER_REQUEST>",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "run_command",
                                "args": {
                                    "CommandLine": "mise run worktree:rm --force unreviewed-slug"
                                },
                            }
                        ],
                    },
                ],
            )
            res25b = _run_hook(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "fullyIdle": True,
                    "transcriptPath": str(t25b),
                    "artifactDirectoryPath": str(art25b),
                    "cwd": str(ws1),
                }
            )
            assert res25b.get(
                "decision"
            ) == "continue" and "unreviewed-slug" in res25b.get("reason", ""), (
                f"case 25b unreviewed worktree:rm must trigger: {res25b}"
            )
            checks.append("cross_turn_worktree_rm_respects_shared_reviews")

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
        "--session-start",
        action="store_true",
        help="Run read-only agy-learn heal check for SessionStart hook.",
    )
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
        raw_text = sys.stdin.read()
        raw_payload = json.loads(raw_text) if raw_text.strip() else {}
    except (json.JSONDecodeError, OSError, ValueError):
        raw_payload = None
    try:
        if args.session_start:
            result = _run_session_start(
                raw_payload if isinstance(raw_payload, dict) else {}
            )
            sys.stdout.write(json.dumps(result) + "\n")
            return 0
        result = _run_hook(raw_payload) if isinstance(raw_payload, dict) else {}
        sys.stdout.write(json.dumps(result) + "\n")
        return 0
    except Exception as exc:
        sys.stderr.write(f"post-task-learn: unexpected error: {exc}\n")
        if args.session_start:
            sys.stdout.write(
                json.dumps(
                    {
                        "injectSteps": [
                            {
                                "systemMessage": {
                                    "systemMessage": f"[agy-learn] SessionStart hook error: {exc}"
                                }
                            }
                        ]
                    }
                )
                + "\n"
            )
        else:
            sys.stdout.write("{}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
