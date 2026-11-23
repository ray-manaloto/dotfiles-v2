"""Typed data models and atomic persistence helpers for `herdr_roll`."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class HarnessKind(StrEnum):
    """Supported Herdr interactive agent harnesses plus plain shells."""

    agy = "agy"
    codex = "codex"
    claude = "claude"
    opencode = "opencode"
    hermes = "hermes"
    zcode = "zcode"
    shell = "shell"


class Role(StrEnum):
    """Scheduling wave role for a Herdr tab."""

    coordinator = "coordinator"
    child = "child"
    lead = "lead"


@dataclass(frozen=True, slots=True)
class Timing:
    """Timeout, polling, and job-confirmation timings (in seconds)."""

    wait: float = 180.0
    settle: float = 45.0
    poll_min: float = 0.25
    poll_max: float = 1.0
    busy_wait: float = 0.0
    job_confirm_delay: float = 1.5


@dataclass(frozen=True, slots=True)
class TerminalSnapshot:
    """Observed state of one live or previously saved Herdr terminal."""

    address: str
    kind: HarnessKind
    session_id: str | None = None
    cwd: str | None = None
    fg_argv: tuple[str, ...] = ()
    term_argv: tuple[str, ...] = ()
    pid: int | None = None
    fg_pid: int | None = None
    status_state: str | None = None
    status_app: str | None = None
    title: str = ""
    turn: int | None = None
    is_coordinator: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "kind": self.kind.value,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "fg_argv": list(self.fg_argv),
            "term_argv": list(self.term_argv),
            "pid": self.pid,
            "fg_pid": self.fg_pid,
            "status_state": self.status_state,
            "status_app": self.status_app,
            "title": self.title,
            "turn": self.turn,
            "is_coordinator": self.is_coordinator,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TerminalSnapshot:
        return cls(
            address=str(data["address"]),
            kind=HarnessKind(str(data["kind"])),
            session_id=str(data["session_id"])
            if data.get("session_id") is not None
            else None,
            cwd=str(data["cwd"]) if data.get("cwd") is not None else None,
            fg_argv=tuple(str(x) for x in data.get("fg_argv") or ()),
            term_argv=tuple(str(x) for x in data.get("term_argv") or ()),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            fg_pid=int(data["fg_pid"]) if data.get("fg_pid") is not None else None,
            status_state=str(data["status_state"])
            if data.get("status_state") is not None
            else None,
            status_app=str(data["status_app"])
            if data.get("status_app") is not None
            else None,
            title=str(data.get("title") or ""),
            turn=int(data["turn"]) if data.get("turn") is not None else None,
            is_coordinator=bool(data.get("is_coordinator", False)),
        )


@dataclass(frozen=True, slots=True)
class TabSnapshot:
    """Observed state of one Herdr tab."""

    id: str
    name: str
    terminals: tuple[TerminalSnapshot, ...] = ()
    kept_from_previous_graph: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "terminals": [term.to_dict() for term in self.terminals],
        }
        if self.kept_from_previous_graph:
            data["kept_from_previous_graph"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TabSnapshot:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            terminals=tuple(
                TerminalSnapshot.from_dict(row) for row in data.get("terminals") or ()
            ),
            kept_from_previous_graph=bool(data.get("kept_from_previous_graph", False)),
        )


@dataclass(frozen=True, slots=True)
class SpaceSnapshot:
    """Observed state of one Herdr space."""

    id: str
    name: str
    folder: str | None = None
    tabs: tuple[TabSnapshot, ...] = ()
    kept_from_previous_graph: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "folder": self.folder,
            "tabs": [tab.to_dict() for tab in self.tabs],
        }
        if self.kept_from_previous_graph:
            data["kept_from_previous_graph"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpaceSnapshot:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            folder=str(data["folder"]) if data.get("folder") is not None else None,
            tabs=tuple(TabSnapshot.from_dict(row) for row in data.get("tabs") or ()),
            kept_from_previous_graph=bool(data.get("kept_from_previous_graph", False)),
        )


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """Snapshot of all Herdr spaces, tabs, and terminals."""

    taken_at: str
    coordinator: str | None = None
    spaces: tuple[SpaceSnapshot, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "taken_at": self.taken_at,
            "coordinator": self.coordinator,
            "spaces": [space.to_dict() for space in self.spaces],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphSnapshot:
        return cls(
            taken_at=str(data["taken_at"]),
            coordinator=str(data["coordinator"])
            if data.get("coordinator") is not None
            else None,
            spaces=tuple(
                SpaceSnapshot.from_dict(row) for row in data.get("spaces") or ()
            ),
        )


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """Outcome record for a single targeted session."""

    where: str
    tab_id: str
    terminal: str
    kind: HarnessKind
    session_id: str | None
    result: str
    old_pid: int | None = None
    new_pid: int | None = None
    elapsed_s: float = 0.0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "where": self.where,
            "tab_id": self.tab_id,
            "terminal": self.terminal,
            "kind": self.kind.value,
            "session_id": self.session_id,
            "result": self.result,
            "old_pid": self.old_pid,
            "new_pid": self.new_pid,
            "elapsed_s": round(self.elapsed_s, 2),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionOutcome:
        return cls(
            where=str(data["where"]),
            tab_id=str(data.get("tab_id") or ""),
            terminal=str(data["terminal"]),
            kind=HarnessKind(str(data["kind"])),
            session_id=str(data["session_id"])
            if data.get("session_id") is not None
            else None,
            result=str(data["result"]),
            old_pid=int(data["old_pid"]) if data.get("old_pid") is not None else None,
            new_pid=int(data["new_pid"]) if data.get("new_pid") is not None else None,
            elapsed_s=float(data.get("elapsed_s") or 0.0),
            notes=tuple(str(n) for n in data.get("notes") or ()),
        )


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One append-only JSONL entry in `runs.jsonl`."""

    run_id: str
    step: str
    args: tuple[str, ...]
    started_at: str
    ended_at: str
    rc: int
    log_path: str
    outcomes: tuple[SessionOutcome, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step": self.step,
            "args": list(self.args),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "rc": self.rc,
            "log_path": self.log_path,
            "outcomes": [item.to_dict() for item in self.outcomes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        return cls(
            run_id=str(data["run_id"]),
            step=str(data["step"]),
            args=tuple(str(x) for x in data.get("args") or ()),
            started_at=str(data["started_at"]),
            ended_at=str(data["ended_at"]),
            rc=int(data["rc"]),
            log_path=str(data.get("log_path") or ""),
            outcomes=tuple(
                SessionOutcome.from_dict(item) for item in data.get("outcomes") or ()
            ),
        )


def write_atomic(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
