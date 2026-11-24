#!/usr/bin/env python3
"""Antigravity PreToolUse/Stop hook enforcing interactive ask_question calls and inline trade-offs."""

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


def _make_hook_state(
    *,
    last_user_idx: int = -2,
    last_blocked_step_idx: int = -1,
    blocks_in_turn: int = 0,
    pre_denied_fps: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "last_user_idx": last_user_idx,
        "last_blocked_step_idx": last_blocked_step_idx,
        "blocks_in_turn": blocks_in_turn,
        "pre_denied_fps": list(pre_denied_fps or ()),
    }


FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
RECOMMENDED_RE = re.compile(r"\(recommended\)", re.IGNORECASE)
PROS_RE = re.compile(r"(?:\*\*pros(?::\*\*|\*\*:?)|\bpros\s*:)", re.IGNORECASE)
CONS_RE = re.compile(r"(?:\*\*cons(?::\*\*|\*\*:?)|\bcons\s*:)", re.IGNORECASE)
OPTION_LABEL_RE = re.compile(r"\boption\s+[12]\b", re.IGNORECASE)
NUMBERED_ITEM_RE = re.compile(r"(?m)^\s*\d+\.\s+")
LIST_ITEM_RE = re.compile(r"(?m)^\s*(?:\d+\.|[-*])\s+")
DECISION_HEADING_RE = re.compile(
    r"(?im)^(?:#{1,6}\s+|\*\*\s*)(?:next\s+step\s+options|key\s+decisions|decisions\s*&\s*trade-offs|options\s*\(with\s*trade-offs\))"
)
LEARN_EXEMPT_RE = re.compile(
    r"(?:/learn\b|learning_proposal\.md|agy-learn|post-task-learn|record-review|learn:apply|proposals?)",
    re.IGNORECASE,
)
PLAN_ID_LINE_RE = re.compile(r'^\s*PLAN_ID\s*=\s*"([^"\r\n]+)"')
PLAN_STATUS_RE = re.compile(
    r"\*\*Status:\*\*[ \t]+(pending|in_progress|complete)(?![A-Za-z0-9_])|\[(pending|in_progress|complete)\]"
)

BLOCK_REASON = (
    "Rule enforcement (interactive-questions.md): Your last response presented "
    "options/decisions in plain prose without calling `ask_question`. "
    "Do NOT repeat the prose — call `ask_question` right now with `(Recommended)` first "
    "(or hand back via `enter_plan_mode` if you are on the execution model)."
)


def _allow() -> dict[str, Any]:
    return {"decision": "allow"}


def _deny(reason: str) -> dict[str, Any]:
    return {"decision": "deny", "reason": reason}


def _strip_inline_code(line: str) -> str:
    """Strip CommonMark single-line inline backtick spans in linear time."""
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


def _looks_like_unasked_options(text: str) -> bool:
    stripped = _strip_fenced_code(text)
    if not stripped.strip():
        return False

    has_recommended = bool(RECOMMENDED_RE.search(stripped))
    pros_hits = len(PROS_RE.findall(stripped))
    cons_hits = len(CONS_RE.findall(stripped))
    has_option_labels = bool(OPTION_LABEL_RE.search(stripped))
    numbered_count = len(NUMBERED_ITEM_RE.findall(stripped))

    if has_recommended and (
        pros_hits > 0 or cons_hits > 0 or has_option_labels or numbered_count >= 2
    ):
        return True

    if DECISION_HEADING_RE.search(stripped) and (
        len(LIST_ITEM_RE.findall(stripped)) >= 2 or has_option_labels
    ):
        return True

    return pros_hits >= 2 and cons_hits >= 2


def _read_state(state_path: Path) -> dict[str, Any]:
    out = _make_hook_state()
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return out
    if not isinstance(raw, dict):
        return out
    last_user = raw.get("last_user_idx")
    if isinstance(last_user, int) and not isinstance(last_user, bool):
        out["last_user_idx"] = last_user
    last_step = raw.get("last_blocked_step_idx")
    if isinstance(last_step, int) and not isinstance(last_step, bool):
        out["last_blocked_step_idx"] = last_step
    blocks = raw.get("blocks_in_turn")
    if isinstance(blocks, int) and not isinstance(blocks, bool):
        out["blocks_in_turn"] = blocks
    raw_fps = raw.get("pre_denied_fps")
    if isinstance(raw_fps, list):
        out["pre_denied_fps"] = [fp for fp in raw_fps if isinstance(fp, str)][-16:]
    return out


def _write_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=state_path.parent, prefix=f".{state_path.name}."
    )
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state) + "\n")
        os.replace(tmp_name, state_path)
        replaced = True
    except OSError:
        return
    finally:
        if not replaced:
            Path(tmp_name).unlink(missing_ok=True)


def _read_jsonl_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").split("\n")


def _load_full_records(
    transcript_path: Path, expected_types: dict[int, tuple[Any, Any]]
) -> dict[int, dict[str, Any]]:
    if not expected_types:
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


def _scan_last_user_idx(transcript_path: Path) -> int | None:
    if not transcript_path.is_file():
        return None
    try:
        lines = _read_jsonl_lines(transcript_path)
    except OSError:
        return None
    last_user_idx = -1
    for idx, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "USER_INPUT":
            last_user_idx = idx
    return last_user_idx


def _resolve_state_path(payload: dict[str, Any]) -> Path | None:
    artifact_dir = payload.get("artifactDirectoryPath")
    if isinstance(artifact_dir, str) and artifact_dir:
        return (
            Path(os.path.expanduser(artifact_dir))
            / ".agy-interactive-questions-state.json"
        )
    transcript_raw = payload.get("transcriptPath")
    if isinstance(transcript_raw, str) and transcript_raw:
        return (
            Path(os.path.expanduser(transcript_raw)).parent
            / ".agy-interactive-questions-state.json"
        )
    return None


def _shared_reviews_path() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(os.path.expanduser(xdg_state))
        if xdg_state
        else Path.home() / ".local" / "state"
    )
    return base / "agy-learn" / "reviews.jsonl"


def _resolve_active_complete_plan(payload: dict[str, Any]) -> tuple[Path, str] | None:
    cwd_raw = payload.get("cwd")
    base = (
        Path(os.path.expanduser(cwd_raw.strip()))
        if isinstance(cwd_raw, str) and cwd_raw.strip()
        else None
    )
    if base is None:
        wp = payload.get("workspacePaths")
        if isinstance(wp, list) and wp and isinstance(wp[0], str) and wp[0].strip():
            base = Path(os.path.expanduser(wp[0].strip()))
    if base is None or not base.is_dir():
        return None
    repo = base.resolve(strict=False)
    while repo != repo.parent and not (repo / ".git").exists():
        repo = repo.parent
    if not (repo / ".git").exists():
        return None
    plan_id = os.environ.get("PLAN_ID", "").strip()
    local_toml = repo / "mise.local.toml"
    if local_toml.is_file():
        try:
            for line in local_toml.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                m = PLAN_ID_LINE_RE.match(line)
                if m:
                    plan_id = m.group(1).strip()
                    break
        except OSError:
            pass
    if not plan_id:
        return None
    plan_file = repo / ".planning" / plan_id / "task_plan.md"
    if not plan_file.is_file():
        return None
    try:
        text = plan_file.read_text(encoding="utf-8", errors="replace")
        digest = hashlib.sha256(plan_file.read_bytes()).hexdigest()
    except OSError:
        return None
    statuses = [m.group(1) or m.group(2) for m in PLAN_STATUS_RE.finditer(text)]
    if statuses and all(st == "complete" for st in statuses):
        return plan_file.resolve(strict=False), digest
    return None


def _check_learn_boundary_before_ask(
    payload: dict[str, Any], questions: list[Any]
) -> str | None:
    """Enforce interactive-questions.md #6: complete /learn review on finished plans before ask_question."""
    for q in questions:
        if not isinstance(q, dict):
            continue
        blob = " ".join(
            [
                str(q.get("question") or ""),
                *[str(o) for o in (q.get("options") or [])],
            ]
        )
        if LEARN_EXEMPT_RE.search(blob):
            return None

    active = _resolve_active_complete_plan(payload)
    if active is None:
        return None
    plan_path, digest = active
    plan_key = str(plan_path)
    reviews_path = _shared_reviews_path()
    latest_outcome: str | None = None
    latest_sha: str | None = None
    latest_ns = -1
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
                    continue
                if not isinstance(rec, dict):
                    continue
                rk = rec.get("plan_key")
                if not isinstance(rk, str) or not rk:
                    continue
                try:
                    rk_norm = str(Path(os.path.expanduser(rk)).resolve(strict=False))
                except OSError:
                    rk_norm = rk
                if rk_norm != plan_key:
                    continue
                ns = rec.get("timestamp_unix_ns", 0)
                ns_val = ns if isinstance(ns, int) and not isinstance(ns, bool) else 0
                if ns_val >= latest_ns:
                    latest_ns = ns_val
                    latest_sha = rec.get("plan_sha256")
                    latest_outcome = rec.get("outcome")
        except OSError:
            pass
    if latest_outcome == "owing_learn" and latest_sha == digest:
        return (
            f"Completed plan '{plan_path.parent.name}' is marked `owing_learn`; "
            "run a bounded `/learn` review (`learn:record-review` or `learn:propose`) before calling `ask_question`."
        )
    if not (
        latest_sha == digest
        and latest_outcome in ("proposed", "no_new_proposals", "applied")
    ):
        return (
            f"Completed plan '{plan_path.parent.name}' has no `/learn` review in reviews.jsonl; "
            "run one bounded `/learn` pass before calling `ask_question` (interactive-questions.md #6)."
        )
    return None


def _run_pre_tool_use_hook(payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_INTERACTIVE_QUESTION_GUARD") == "1":
        return _allow()

    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        tool_name = tool_call.get("name")
        args = tool_call.get("args")
    else:
        tool_name = payload.get("tool_name")
        args = payload.get("tool_input")

    if tool_name != "ask_question" or not isinstance(args, dict):
        return _allow()

    questions = args.get("questions")
    if not isinstance(questions, list) or not questions:
        return _allow()

    learn_violation = _check_learn_boundary_before_ask(payload, questions)
    if learn_violation is not None:
        return _deny(f"Rule enforcement (interactive-questions.md): {learn_violation}")

    violations: list[str] = []
    normalized_qs: list[dict[str, Any]] = []
    for q_idx, q in enumerate(questions, start=1):
        if not isinstance(q, dict):
            continue
        options = q.get("options")
        if not isinstance(options, list) or len(options) < 2:
            continue
        is_multi = bool(q.get("is_multi_select", False))
        str_opts = [opt for opt in options if isinstance(opt, str) and opt.strip()]
        normalized_qs.append(
            {
                "question": q.get("question", "")
                if isinstance(q.get("question"), str)
                else "",
                "is_multi_select": is_multi,
                "options": str_opts,
            }
        )
        if len(str_opts) != len(options):
            violations.append(f"Q{q_idx}: all options must be non-empty strings")
            continue
        if not is_multi and not RECOMMENDED_RE.search(str_opts[0]):
            violations.append(
                f"Q{q_idx}: leading option must include '(Recommended)' first"
            )
        for o_idx, opt in enumerate(str_opts, start=1):
            missing: list[str] = []
            if not PROS_RE.search(opt):
                missing.append("Pros:")
            if not CONS_RE.search(opt):
                missing.append("Cons:")
            if missing:
                violations.append(
                    f"Q{q_idx} option {o_idx}: missing inline {' and '.join(missing)}"
                )

    if not violations:
        return _allow()

    state_path = _resolve_state_path(payload)
    if state_path is None:
        return _allow()

    transcript_raw = payload.get("transcriptPath")
    user_idx = (
        _scan_last_user_idx(Path(os.path.expanduser(transcript_raw)))
        if isinstance(transcript_raw, str) and transcript_raw
        else None
    )
    user_turn_id = str(user_idx) if user_idx is not None else "no-transcript"
    q_sha = hashlib.sha256(
        json.dumps(normalized_qs, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    fp = f"{user_turn_id}:{q_sha}"

    state = _read_state(state_path)
    if user_idx is not None and state["last_user_idx"] != user_idx:
        state = _make_hook_state(last_user_idx=user_idx)

    pre_fps = list(state["pre_denied_fps"])
    if fp in pre_fps:
        return _allow()

    pre_fps.append(fp)
    state["pre_denied_fps"] = pre_fps[-16:]
    _write_state(state_path, state)
    return _deny(
        "Rule enforcement (interactive-questions.md): "
        + "; ".join(violations)
        + ". Fix once (include '(Recommended)' on option 1 for single-select "
        + "questions and compact 'Pros: ... | Cons: ...' in each option) and retry."
    )


def _run_hook(payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_INTERACTIVE_QUESTION_GUARD") == "1":
        return {}
    if payload.get("terminationReason") != "NO_TOOL_CALL":
        return {}
    if payload.get("fullyIdle") is not True:
        return {}

    transcript_raw = payload.get("transcriptPath")
    if not isinstance(transcript_raw, str) or not transcript_raw:
        return {}
    transcript_path = Path(os.path.expanduser(transcript_raw))
    if not transcript_path.is_file():
        return {}

    try:
        lines = _read_jsonl_lines(transcript_path)
    except OSError:
        return {}

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
        entries.append((idx, obj))
        if obj.get("type") == "USER_INPUT":
            last_user_idx = idx

    final_resp: tuple[int, dict[str, Any]] | None = None
    for idx, obj in entries:
        if idx > last_user_idx and obj.get("type") == "PLANNER_RESPONSE":
            final_resp = (idx, obj)

    if final_resp is None:
        return {}

    final_line_idx, raw_resp_obj = final_resp
    truncated = raw_resp_obj.get("truncated_fields")
    resp_obj = raw_resp_obj
    if isinstance(truncated, list) and (
        "content" in truncated or "tool_calls" in truncated
    ):
        full_records = _load_full_records(
            transcript_path,
            {
                final_line_idx: (
                    raw_resp_obj.get("type"),
                    raw_resp_obj.get("step_index"),
                )
            },
        )
        resp_obj = full_records.get(final_line_idx, raw_resp_obj)

    tool_calls = resp_obj.get("tool_calls") or []
    if isinstance(tool_calls, list) and len(tool_calls) > 0:
        return {}

    raw_step = resp_obj.get("step_index")
    step_idx = (
        raw_step
        if isinstance(raw_step, int) and not isinstance(raw_step, bool)
        else final_line_idx
    )

    content = resp_obj.get("content")
    text = _unquote_truncated(content) if isinstance(content, str) else ""

    if not _looks_like_unasked_options(text):
        return {}

    artifact_dir = payload.get("artifactDirectoryPath")
    state_base = (
        Path(os.path.expanduser(artifact_dir))
        if isinstance(artifact_dir, str) and artifact_dir
        else transcript_path.parent
    )
    state_path = state_base / ".agy-interactive-questions-state.json"
    state = _read_state(state_path)
    if state["last_user_idx"] != last_user_idx:
        state = _make_hook_state(last_user_idx=last_user_idx)

    if state["last_blocked_step_idx"] == step_idx or state["blocks_in_turn"] >= 2:
        return {}

    _write_state(
        state_path,
        _make_hook_state(
            last_user_idx=last_user_idx,
            last_blocked_step_idx=step_idx,
            blocks_in_turn=state["blocks_in_turn"] + 1,
            pre_denied_fps=state["pre_denied_fps"],
        ),
    )
    return {"decision": "continue", "reason": BLOCK_REASON}


def _process_stdin_text(raw_stdin: str, event: str = "auto") -> dict[str, Any]:
    try:
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, ValueError):
        return _allow() if event == "pre-tool-use" else {}
    if not isinstance(payload, dict):
        return _allow() if event == "pre-tool-use" else {}
    if event == "pre-tool-use" or (
        event == "auto" and ("toolCall" in payload or "tool_input" in payload)
    ):
        return _run_pre_tool_use_hook(payload)
    return _run_hook(payload)


def _check_hooks_json(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "agy-interactive-questions" not in data:
        raise AssertionError(f"missing 'agy-interactive-questions' in {path}")
    block = data["agy-interactive-questions"]
    if not isinstance(block, dict):
        msg = f"invalid agy-interactive-questions block in {path}"
        raise TypeError(msg)

    pre_handlers = block.get("PreToolUse")
    if not isinstance(pre_handlers, list) or not pre_handlers:
        raise AssertionError(
            f"missing PreToolUse handlers for agy-interactive-questions in {path}"
        )
    first_pre = pre_handlers[0]
    matcher = first_pre.get("matcher", "") if isinstance(first_pre, dict) else ""
    if not isinstance(matcher, str) or "ask_question" not in matcher:
        raise AssertionError(f"unexpected PreToolUse matcher: {matcher!r}")
    inner = first_pre.get("hooks") if isinstance(first_pre, dict) else None
    if not isinstance(inner, list) or not inner:
        raise AssertionError(f"missing PreToolUse inner hooks list in {path}")
    pre_cmd = inner[0].get("command", "") if isinstance(inner[0], dict) else ""
    if (
        not isinstance(pre_cmd, str)
        or "agy-interactive-questions.py" not in pre_cmd
        or "--event pre-tool-use" not in pre_cmd
    ):
        raise AssertionError(f"unexpected PreToolUse hook command: {pre_cmd!r}")
    if pre_cmd.startswith("/") or "/Users/" in pre_cmd:
        raise AssertionError(
            f"PreToolUse hook command must be portable relative path, got: {pre_cmd!r}"
        )

    stop_handlers = block.get("Stop")
    if not isinstance(stop_handlers, list) or not stop_handlers:
        raise AssertionError(
            f"missing Stop handlers for agy-interactive-questions in {path}"
        )
    cmd = (
        stop_handlers[0].get("command", "")
        if isinstance(stop_handlers[0], dict)
        else ""
    )
    if not isinstance(cmd, str) or "agy-interactive-questions.py" not in cmd:
        raise AssertionError(f"unexpected Stop hook command: {cmd!r}")
    if cmd.startswith("/") or "/Users/" in cmd:
        raise AssertionError(
            f"Stop hook command must be portable relative path, got: {cmd!r}"
        )


def _write_transcript(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(rec) + "\n" for rec in records), encoding="utf-8"
    )


def _run_self_check(hooks_json_path: Path | None) -> list[str]:
    checks: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        transcript = root / "transcript.jsonl"
        artifact_dir = root / "artifacts"
        artifact_dir.mkdir()

        def base_payload(**extra: Any) -> dict[str, Any]:
            p: dict[str, Any] = {
                "terminationReason": "NO_TOOL_CALL",
                "fullyIdle": True,
                "transcriptPath": str(transcript),
                "artifactDirectoryPath": str(artifact_dir),
            }
            p.update(extra)
            return p

        # 1. Recommended + Pros/Cons with tool_calls=[] -> continue
        _write_transcript(
            transcript,
            [
                {"type": "USER_INPUT", "content": "What next?"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 10,
                    "tool_calls": [],
                    "content": "1. `(Recommended)` Option A\n   - **Pros:** Fast.\n   - **Cons:** Scope.\n2. Option B\n   - **Pros:** Minimal.\n   - **Cons:** Slow.",
                },
            ],
        )
        res1 = _run_hook(base_payload())
        assert res1.get("decision") == "continue", f"case 1 failed: {res1}"
        checks.append("recommended_pros_cons_blocks")

        # 2. Same step_index re-run -> {} (bounded, no loop)
        res2 = _run_hook(base_payload())
        assert res2 == {}, f"case 2 failed: {res2}"
        checks.append("same_step_deduped")

        # 3. Continuation turn (continuationActive: true) + Key Decisions -> continue
        _write_transcript(
            transcript,
            [
                {"type": "USER_INPUT", "content": "What next?"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 10,
                    "tool_calls": [],
                    "content": "1. `(Recommended)` Option A\n   - **Pros:** Fast.",
                },
                {
                    "type": "SYSTEM_MESSAGE",
                    "content": "Automated post-task /learn check",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 14,
                    "tool_calls": [],
                    "content": "### Key Decisions\n\n- Option 1\n  - **Pros:** Complete.\n  - **Cons:** Longer.\n- Option 2\n  - **Pros:** Fast.",
                },
            ],
        )
        res3 = _run_hook(base_payload(continuationActive=True))
        assert res3.get("decision") == "continue", f"case 3 failed: {res3}"
        checks.append("continuation_active_enforced")

        # 4. Routine summary with numbered steps -> {}
        _write_transcript(
            transcript,
            [
                {"type": "USER_INPUT", "content": "Run checks"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 20,
                    "tool_calls": [],
                    "content": "Completed tasks:\n1. Ran hk check --all (33/33 passed).\n2. Synced pc, mac, and vm cleanly.",
                },
            ],
        )
        res4 = _run_hook(base_payload())
        assert res4 == {}, f"case 4 failed: {res4}"
        checks.append("routine_numbered_summary_allowed")

        # 5. (Recommended) + Pros/Cons inside fenced code block -> {}
        _write_transcript(
            transcript,
            [
                {"type": "USER_INPUT", "content": "Show example"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 30,
                    "tool_calls": [],
                    "content": "Example format:\n```markdown\n1. `(Recommended)` Choice A\n   - **Pros:** Good.\n   - **Cons:** None.\n```",
                },
            ],
        )
        res5 = _run_hook(base_payload())
        assert res5 == {}, f"case 5 failed: {res5}"
        checks.append("fenced_code_ignored")

        # 6. Non-NO_TOOL_CALL & malformed JSON -> {}
        assert _run_hook(base_payload(terminationReason="MAX_INVOCATIONS")) == {}
        assert _process_stdin_text("{not valid json") == {}
        assert _process_stdin_text("{not valid json", event="pre-tool-use") == _allow()
        checks.append("non_idle_and_malformed_ignored")

        # 7. Inline backtick status report quoting `(Recommended)` / `Pros:` / `Cons:` -> {}
        _write_transcript(
            transcript,
            [
                {"type": "USER_INPUT", "content": "Status?"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 40,
                    "tool_calls": [],
                    "content": (
                        "Completed updates:\n"
                        "1. Enforced `(Recommended)` and `Pros:` / `Cons:` inside `ask_question`.\n"
                        "2. Verified `hk check --all` passes."
                    ),
                },
            ],
        )
        res7 = _run_hook(base_payload())
        assert res7 == {}, f"case 7 inline-code status report failed: {res7}"
        checks.append("inline_code_status_report_allowed")

        # 8. PreToolUse non-ask_question tool allowed
        pre_other = _run_pre_tool_use_hook(
            {
                "artifactDirectoryPath": str(artifact_dir),
                "transcriptPath": str(transcript),
                "toolCall": {"name": "edit_file", "args": {"Path": "foo.md"}},
            }
        )
        assert pre_other == _allow(), f"case 8 failed: {pre_other}"
        checks.append("pre_tool_use_non_ask_question_allowed")

        # 9. PreToolUse valid single-select ask_question allowed
        pre_valid = _run_pre_tool_use_hook(
            {
                "artifactDirectoryPath": str(artifact_dir),
                "transcriptPath": str(transcript),
                "toolCall": {
                    "name": "ask_question",
                    "args": {
                        "questions": [
                            {
                                "question": "Which option?",
                                "is_multi_select": False,
                                "options": [
                                    "Option A (Recommended) — Pros: fast | Cons: scope",
                                    "Option B — Pros: tiny | Cons: slower",
                                ],
                            }
                        ]
                    },
                },
            }
        )
        assert pre_valid == _allow(), f"case 9 failed: {pre_valid}"
        checks.append("pre_tool_use_valid_single_select_allowed")

        # 10. PreToolUse missing (Recommended) or Pros:/Cons: denies ONCE then allows same payload
        bad_pre_payload = {
            "artifactDirectoryPath": str(artifact_dir),
            "transcriptPath": str(transcript),
            "toolCall": {
                "name": "ask_question",
                "args": {
                    "questions": [
                        {
                            "question": "Pick one",
                            "is_multi_select": False,
                            "options": ["Option A", "Option B"],
                        }
                    ]
                },
            },
        }
        pre_deny_1 = _process_stdin_text(
            json.dumps(bad_pre_payload), event="pre-tool-use"
        )
        assert pre_deny_1.get("decision") == "deny", f"case 10a failed: {pre_deny_1}"
        pre_retry_2 = _process_stdin_text(
            json.dumps(bad_pre_payload), event="pre-tool-use"
        )
        assert pre_retry_2 == _allow(), f"case 10b 1-shot allow failed: {pre_retry_2}"
        checks.append(
            "pre_tool_use_missing_recommended_or_pros_cons_denied_once_then_allowed"
        )

        # 11. PreToolUse multi-select without (Recommended) allowed when Pros:/Cons: present
        multi_ok = _run_pre_tool_use_hook(
            {
                "artifactDirectoryPath": str(artifact_dir),
                "transcriptPath": str(transcript),
                "toolCall": {
                    "name": "ask_question",
                    "args": {
                        "questions": [
                            {
                                "question": "Select any",
                                "is_multi_select": True,
                                "options": [
                                    "Item 1 — Pros: clean | Cons: none",
                                    "Item 2 — Pros: flexible | Cons: extra edit",
                                ],
                            }
                        ]
                    },
                },
            }
        )
        assert multi_ok == _allow(), f"case 11 failed: {multi_ok}"
        checks.append(
            "pre_tool_use_multi_select_without_recommended_allowed_when_pros_cons_present"
        )

        # 12. PreToolUse multi-select missing Pros:/Cons: denied once then allowed
        multi_bad_payload = {
            "artifactDirectoryPath": str(artifact_dir),
            "transcriptPath": str(transcript),
            "toolCall": {
                "name": "ask_question",
                "args": {
                    "questions": [
                        {
                            "question": "Select any",
                            "is_multi_select": True,
                            "options": [
                                "Item 1 — Pros: clean",
                                "Item 2 — Cons: extra edit",
                            ],
                        }
                    ]
                },
            },
        }
        multi_deny_1 = _run_pre_tool_use_hook(multi_bad_payload)
        assert multi_deny_1.get("decision") == "deny", (
            f"case 12a failed: {multi_deny_1}"
        )
        res12b = _run_pre_tool_use_hook(multi_bad_payload)
        assert res12b == _allow(), f"case 12b failed: {res12b}"
        checks.append("pre_tool_use_multi_select_missing_pros_cons_denied_once")

        # 13. Hydrates truncated tool_calls and content from transcript_full.jsonl
        full_transcript = root / "transcript_full.jsonl"
        _write_transcript(
            transcript,
            [
                {"type": "USER_INPUT", "content": "Prior"},
                {"type": "USER_INPUT", "content": "Decide"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 50,
                    "tool_calls": [],
                    "truncated_fields": ["tool_calls", "content"],
                    "content": '"1. `(Recommended)` Option A\\n   - **Pros:** Fast."',
                },
            ],
        )
        _write_transcript(
            full_transcript,
            [
                {"type": "USER_INPUT", "content": "Prior"},
                {"type": "USER_INPUT", "content": "Decide"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 50,
                    "tool_calls": [{"name": "ask_question", "args": {}}],
                    "content": "1. `(Recommended)` Option A\n   - **Pros:** Fast.",
                },
            ],
        )
        assert _run_hook(base_payload()) == {}, "case 13a full tool_calls should allow"
        _write_transcript(
            full_transcript,
            [
                {"type": "USER_INPUT", "content": "Prior"},
                {"type": "USER_INPUT", "content": "Decide2"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 51,
                    "tool_calls": [],
                    "content": "1. (Recommended) Option A\n   - **Pros:** Fast.\n   - **Cons:** Slow.\n2. Option B\n   - **Pros:** Small.",
                },
            ],
        )
        _write_transcript(
            transcript,
            [
                {"type": "USER_INPUT", "content": "Prior"},
                {"type": "USER_INPUT", "content": "Decide2"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 51,
                    "tool_calls": [],
                    "truncated_fields": ["content"],
                    "content": '"1. `(Recommended)` Option A"',
                },
            ],
        )
        res13b = _run_hook(base_payload())
        assert res13b.get("decision") == "continue", f"case 13b failed: {res13b}"
        checks.append("transcript_full_hydration_tool_calls_and_content")

        # 14. Interactive ask_question /learn boundary enforces review on completed plans
        repo14 = root / "repo14"
        (repo14 / ".git").mkdir(parents=True)
        plan14_dir = repo14 / ".planning" / "plan14"
        plan14_dir.mkdir(parents=True)
        plan14 = plan14_dir / "task_plan.md"
        plan14.write_text(
            "# Plan 14\n\n### Phase 1: Do\n**Status:** complete\n", encoding="utf-8"
        )
        (repo14 / "mise.local.toml").write_text(
            '[env]\nPLAN_ID = "plan14"\n', encoding="utf-8"
        )
        xdg14 = root / "xdg14"
        saved_xdg = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(xdg14)
        try:
            q_follow = {
                "cwd": str(repo14),
                "toolCall": {
                    "name": "ask_question",
                    "args": {
                        "questions": [
                            {
                                "question": "Next step?",
                                "is_multi_select": False,
                                "options": [
                                    "Deploy (Recommended) — Pros: fast | Cons: risk",
                                    "Wait — Pros: safe | Cons: slow",
                                ],
                            }
                        ]
                    },
                },
            }
            assert _run_pre_tool_use_hook(q_follow).get("decision") == "deny", (
                "case 14a completed unreviewed plan must block follow-up ask_question"
            )
            q_learn = {
                "cwd": str(repo14),
                "toolCall": {
                    "name": "ask_question",
                    "args": {
                        "questions": [
                            {
                                "question": "Approve /learn proposals?",
                                "is_multi_select": False,
                                "options": [
                                    "Approve (Recommended) — Pros: clean | Cons: none",
                                    "Decline — Pros: skip | Cons: deferred",
                                ],
                            }
                        ]
                    },
                },
            }
            assert _run_pre_tool_use_hook(q_learn) == _allow(), (
                "case 14b /learn-topic ask_question should be exempt"
            )
            rev14 = xdg14 / "agy-learn" / "reviews.jsonl"
            rev14.parent.mkdir(parents=True)
            rev14.write_text(
                json.dumps(
                    {
                        "plan_key": str(plan14.resolve()),
                        "plan_sha256": hashlib.sha256(plan14.read_bytes()).hexdigest(),
                        "outcome": "no_new_proposals",
                        "timestamp_unix_ns": 100,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            assert _run_pre_tool_use_hook(q_follow) == _allow(), (
                "case 14c reviewed completed plan allows ask_question"
            )
        finally:
            if saved_xdg is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = saved_xdg
        checks.append("pre_tool_use_learn_boundary_enforced")

    if hooks_json_path is not None:
        _check_hooks_json(hooks_json_path)
        checks.append("hooks_json_portable")

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event",
        choices=("auto", "stop", "pre-tool-use"),
        default="auto",
        help="Hook lifecycle event mode.",
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
        help="Verify hooks.json registers agy-interactive-questions cleanly.",
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

    result = _process_stdin_text(sys.stdin.read(), event=args.event)
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
