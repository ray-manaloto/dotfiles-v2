#!/usr/bin/env python3
"""Antigravity Stop hook enforcing interactive ask_question calls when prose presents options."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

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

BLOCK_REASON = (
    "Rule enforcement (interactive-questions.md): Your last response presented "
    "options/decisions in plain prose without calling `ask_question`. "
    "Do NOT repeat the prose — call `ask_question` right now with `(Recommended)` first "
    "(or hand back via `enter_plan_mode` if you are on the execution model)."
)


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
            kept.append(line)
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


def _read_state(state_path: Path) -> dict[str, int]:
    default = {"last_user_idx": -2, "last_blocked_step_idx": -1, "blocks_in_turn": 0}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default
    if not isinstance(raw, dict):
        return default
    out = dict(default)
    for key in default:
        val = raw.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            out[key] = val
    return out


def _write_state(state_path: Path, state: dict[str, int]) -> None:
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


def _load_full_content(transcript_path: Path, line_idx: int) -> str | None:
    full_path = transcript_path.with_name("transcript_full.jsonl")
    if not full_path.is_file():
        return None
    try:
        with full_path.open("r", encoding="utf-8", errors="replace") as handle:
            for idx, raw in enumerate(handle):
                if idx == line_idx:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and isinstance(obj.get("content"), str):
                        return obj["content"]
                    return None
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return None


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
    transcript_path = Path(transcript_raw)
    if not transcript_path.is_file():
        return {}

    try:
        lines = transcript_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
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

    final_line_idx, resp_obj = final_resp
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
    text = content if isinstance(content, str) else ""
    truncated = resp_obj.get("truncated_fields")
    if isinstance(truncated, list) and "content" in truncated:
        full_text = _load_full_content(transcript_path, final_line_idx)
        if full_text is not None:
            text = full_text

    if not _looks_like_unasked_options(text):
        return {}

    artifact_dir = payload.get("artifactDirectoryPath")
    state_base = (
        Path(artifact_dir)
        if isinstance(artifact_dir, str) and artifact_dir
        else transcript_path.parent
    )
    state_path = state_base / ".agy-interactive-questions-state.json"
    state = _read_state(state_path)
    if state["last_user_idx"] != last_user_idx:
        state = {
            "last_user_idx": last_user_idx,
            "last_blocked_step_idx": -1,
            "blocks_in_turn": 0,
        }

    if state["last_blocked_step_idx"] == step_idx or state["blocks_in_turn"] >= 2:
        return {}

    _write_state(
        state_path,
        {
            "last_user_idx": last_user_idx,
            "last_blocked_step_idx": step_idx,
            "blocks_in_turn": state["blocks_in_turn"] + 1,
        },
    )
    return {"decision": "continue", "reason": BLOCK_REASON}


def _process_stdin_text(raw_stdin: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return _run_hook(payload)


def _check_hooks_json(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "agy-interactive-questions" not in data:
        raise AssertionError(f"missing 'agy-interactive-questions' in {path}")
    block = data["agy-interactive-questions"]
    stop_handlers = block.get("Stop") if isinstance(block, dict) else None
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
        raise AssertionError(f"unexpected hook command: {cmd!r}")
    if cmd.startswith("/") or "/Users/" in cmd:
        raise AssertionError(
            f"hook command must be portable relative path, got: {cmd!r}"
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
        checks.append("non_idle_and_malformed_ignored")

    if hooks_json_path is not None:
        _check_hooks_json(hooks_json_path)
        checks.append("hooks_json_portable")

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

    result = _process_stdin_text(sys.stdin.read())
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
