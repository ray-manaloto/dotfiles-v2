"""Schema-generated data models, typed error/return helpers, and atomic persistence for `herdr_roll`."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from dotfiles_py.generated import (
    AgyLogProof,
    GraphSnapshot,
    HarnessKind,
    HerdrRollErrorCode,
    HerdrRollErrorEntry,
    HerdrRollResponse,
    HerdrRollReturnCode,
    HerdrRollReturnEntry,
    ReceiptRecord,
    Role,
    RunRecord,
    SelfPlanResponse,
    SessionOutcome,
    SnapshotSummary,
    SpaceSnapshot,
    TabSnapshot,
    TerminalSnapshot,
    Timing,
)
from dotfiles_py.time_ns import now_ns_pair

__all__ = [
    "AgyLogProof",
    "GraphSnapshot",
    "HarnessKind",
    "HerdrRollErrorCode",
    "HerdrRollErrorEntry",
    "HerdrRollResponse",
    "HerdrRollReturnCode",
    "HerdrRollReturnEntry",
    "ReceiptRecord",
    "Role",
    "RunRecord",
    "SelfPlanResponse",
    "SessionOutcome",
    "SnapshotSummary",
    "SpaceSnapshot",
    "TabSnapshot",
    "TerminalSnapshot",
    "Timing",
    "make_session_outcome",
    "outcome_codes_for_result",
    "write_atomic",
]

_RESULT_ERROR_MAP: dict[str, HerdrRollErrorCode] = {
    "skipped-unsupported": HerdrRollErrorCode.ERR_UNSUPPORTED_HARNESS,
    "skipped-no-session-id": HerdrRollErrorCode.ERR_NO_SESSION_ID,
    "failed-no-session-id": HerdrRollErrorCode.ERR_NO_SESSION_ID,
    "skipped-unknown-flag": HerdrRollErrorCode.ERR_UNKNOWN_FLAG,
    "skipped-dangling-flag": HerdrRollErrorCode.ERR_DANGLING_FLAG,
    "skipped-gone": HerdrRollErrorCode.ERR_TAB_GONE,
    "skipped-moved": HerdrRollErrorCode.ERR_TAB_MOVED,
    "skipped-foreground-changed": HerdrRollErrorCode.ERR_FOREGROUND_CHANGED,
    "blocked-dialog": HerdrRollErrorCode.ERR_BLOCKED_DIALOG,
    "skipped-busy": HerdrRollErrorCode.ERR_BUSY,
    "skipped-busy-after-handshake": HerdrRollErrorCode.ERR_BUSY_AFTER_HANDSHAKE,
    "skipped-busy-keep-me": HerdrRollErrorCode.ERR_BUSY_KEEP_ME,
    "skipped-handshake-not-sent": HerdrRollErrorCode.ERR_HANDSHAKE_NOT_SENT,
    "skipped-no-handshake-ack": HerdrRollErrorCode.ERR_NO_HANDSHAKE_ACK,
    "skipped-background-work": HerdrRollErrorCode.ERR_BACKGROUND_WORK,
    "skipped-tab-busy": HerdrRollErrorCode.ERR_TAB_BUSY,
    "failed-exit-send": HerdrRollErrorCode.ERR_EXIT_SEND_FAILED,
    "failed-exit-timeout": HerdrRollErrorCode.ERR_EXIT_TIMEOUT,
    "failed-relaunch-send": HerdrRollErrorCode.ERR_RELAUNCH_SEND_FAILED,
    "failed-open": HerdrRollErrorCode.ERR_OPEN_FAILED,
    "failed-verify-timeout": HerdrRollErrorCode.ERR_VERIFY_TIMEOUT,
    "failed-wrong-folder": HerdrRollErrorCode.ERR_WRONG_FOLDER,
    "failed-worktree-changed": HerdrRollErrorCode.ERR_WORKTREE_CHANGED,
    "failed-tab-close": HerdrRollErrorCode.ERR_TAB_CLOSE_FAILED,
    "failed-in-place-restart": HerdrRollErrorCode.ERR_IN_PLACE_RESTART_FAILED,
    "failed-in-place-send": HerdrRollErrorCode.ERR_IN_PLACE_SEND_FAILED,
    "failed-verify-turn": HerdrRollErrorCode.ERR_VERIFY_TURN_FAILED,
}

_SUCCESS_RETURN_MAP: dict[str, HerdrRollReturnCode] = {
    "ok": HerdrRollReturnCode.RET_OK,
    "planned": HerdrRollReturnCode.RET_PLANNED,
    "restarted": HerdrRollReturnCode.RET_RESTARTED,
    "stopped": HerdrRollReturnCode.RET_STOPPED,
    "started": HerdrRollReturnCode.RET_STARTED,
}


def outcome_codes_for_result(
    result: str,
    notes: Sequence[str] = (),
) -> tuple[list[HerdrRollErrorEntry], list[HerdrRollReturnEntry]]:
    """Map an outcome `result` string to closed `HerdrRollErrorCode` and `HerdrRollReturnCode` lists."""
    note_msg = "; ".join(n for n in notes if n) or None
    detail: dict[str, Any] | None = {"notes": list(notes)} if notes else None
    if result in _SUCCESS_RETURN_MAP:
        return [], [
            HerdrRollReturnEntry(
                code=_SUCCESS_RETURN_MAP[result],
                message=note_msg,
                detail=detail,
            )
        ]

    err_code = _RESULT_ERROR_MAP.get(result, HerdrRollErrorCode.ERR_UNKNOWN)
    ret_code = (
        HerdrRollReturnCode.RET_SKIPPED
        if result.startswith("skipped-")
        else HerdrRollReturnCode.RET_FAILED
    )
    return (
        [HerdrRollErrorEntry(code=err_code, message=note_msg or result, detail=detail)],
        [HerdrRollReturnEntry(code=ret_code, message=result)],
    )


def make_session_outcome(
    *,
    where: str,
    tab_id: str,
    terminal: str,
    kind: HarnessKind,
    session_id: str | None,
    result: str,
    old_pid: int | None = None,
    new_pid: int | None = None,
    process_started_at: str | None = None,
    process_lstart: str | None = None,
    agy_log_proof: AgyLogProof | None = None,
    elapsed_s: float = 0.0,
    notes: Sequence[str] = (),
) -> SessionOutcome:
    """Construct an immutable schema-validated `SessionOutcome` with 9-digit ns timestamp."""
    ts, ns = now_ns_pair()
    note_list = list(notes)
    errors, returns = outcome_codes_for_result(result, note_list)
    return SessionOutcome(
        where=where,
        tab_id=tab_id,
        terminal=terminal,
        kind=kind,
        session_id=session_id,
        result=result,
        old_pid=old_pid,
        new_pid=new_pid,
        process_started_at=process_started_at,
        process_lstart=process_lstart,
        agy_log_proof=agy_log_proof,
        elapsed_s=round(float(elapsed_s), 2),
        notes=note_list,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=errors,
        returns=returns,
    )


def write_atomic(path: Path, content: str | bytes) -> None:
    """Write *content* to *path* atomically via a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    raw = content.encode("utf-8") if isinstance(content, str) else content
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
