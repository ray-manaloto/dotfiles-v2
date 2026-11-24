"""Core rolling-restart execution engine, `filelock` locking, wave scheduler, and dual-sink `structlog`."""

from __future__ import annotations

import os
import shlex
import sys
import threading
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import orjson
from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from pydantic import BaseModel, ValidationError

from dotfiles_py.logging import configure_logging, get_logger
from dotfiles_py.time_ns import format_ns_timestamp, now_ns_pair
from herdr_roll.adapters import (
    ADAPTERS,
    AdapterSkipError,
    claude_record_for_tree,
    harness_running,
)
from herdr_roll.graph import (
    RefusedError,
    TargetTuple,
    build_graph,
    load_graph,
    merge_graph,
    render_ledger,
    role_of,
)
from herdr_roll.herdr import (
    LiveClient,
    adaptive_poll,
    clean_relaunch_command,
    git_status_porcelain,
    hc_keys,
    hc_read,
    hc_send,
    herdr_app_do,
    herdr_app_ls,
    inspect_agy_cli_log,
    is_bare_shell,
    lsof_cwd,
    process_start_info,
    program_name,
)
from herdr_roll.models import (
    AgyLogProof,
    HarnessKind,
    HerdrRollErrorCode,
    HerdrRollErrorEntry,
    HerdrRollReturnCode,
    HerdrRollReturnEntry,
    ReceiptRecord,
    Role,
    RunRecord,
    SelfPlanResponse,
    SessionOutcome,
    SnapshotSummary,
    Timing,
    make_session_outcome,
    write_atomic,
)

HARNESS_BINARIES: dict[HarnessKind, tuple[str, ...]] = {
    HarnessKind.AGY: ("agy",),
    HarnessKind.CODEX: ("codex",),
    HarnessKind.CLAUDE: ("claude",),
    HarnessKind.OPENCODE: ("opencode",),
    HarnessKind.HERMES: ("hermes",),
    HarnessKind.ZCODE: ("zcode",),
}

EXIT_DIALOG = "Background work is running"
READY_MARKER = "READY-TO-RESTART"
BUSY_MARKER = "BUSY-KEEP-ME"
VERIFY_PROMPT = "Rolling restart verification only: confirm resumed session and cwd cleanly; make no edits."
HANDSHAKE_TEMPLATE = (
    "From herdr rolling restart: finish your current micro-step safely with no new work, "
    "then reply with the single line READY-TO-RESTART {token} "
    "(or BUSY-KEEP-ME {token} with your reason if stopping now would break active work)."
)

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2


class SkipStep(Exception):
    """Non-fatal per-session skip or failure converted cleanly into `SessionOutcome`."""

    def __init__(self, result: str, *notes: str) -> None:
        super().__init__(result)
        self.result = result
        self.notes = notes


def state_dir() -> Path:
    """Return `${XDG_STATE_HOME:-~/.local/state}/herdr-roll`."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else (Path.home() / ".local/state")
    return root / "herdr-roll"


def now_iso() -> str:
    """Return current zero-padded 9-digit nanosecond timestamp."""
    return format_ns_timestamp()


def new_run_id(step: str) -> str:
    return f"run-{format_ns_timestamp()}-{step}-{os.getpid()}"


@contextmanager
def acquire_lock(lock_path: Path) -> Generator[None]:
    """Acquire non-blocking exclusive advisory lock via `filelock` or fail closed with `RefusedError`."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path), timeout=0)
    try:
        lock.acquire(timeout=0)
    except FileLockTimeout:
        raise RefusedError(
            f"Another roll step holds lock {lock_path}; wait for it to finish."
        ) from None
    try:
        yield
    finally:
        lock.release()


def read_history(root: Path) -> list[RunRecord]:
    """Read valid `RunRecord` entries from `runs.jsonl`."""
    runs_file = root / "runs.jsonl"
    if not runs_file.is_file():
        return []
    out: list[RunRecord] = []
    for line in runs_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            out.append(RunRecord.model_validate(orjson.loads(line)))
        except ValueError, ValidationError:
            continue
    return out


def last_run_id(root: Path, steps: set[str]) -> str | None:
    """Find the newest `run_id` matching any step in *steps*."""
    for rec in reversed(read_history(root)):
        if rec.step in steps:
            return rec.run_id
    return None


def _dump_compact_json(model: BaseModel) -> str:
    return orjson.dumps(model.model_dump(mode="json")).decode("utf-8")


def _dump_pretty_json(model: BaseModel) -> str:
    return (
        orjson.dumps(model.model_dump(mode="json"), option=orjson.OPT_INDENT_2).decode(
            "utf-8"
        )
        + "\n"
    )


class StepRecorder:
    """Thread-safe dual-sink `structlog` streamer, stdout JSON emitter, and `runs.jsonl` writer."""

    def __init__(
        self,
        *,
        root: Path,
        run_id: str,
        step: str,
        argv: tuple[str, ...],
        apply: bool,
    ) -> None:
        self.root = root
        self.run_id = run_id
        self.step = step
        self.argv = argv
        self.apply = apply
        self.started_at = now_iso()
        self.run_dir = self.root / self.run_id
        self.log_path = self.run_dir / f"{self.step}.jsonl"
        self.outcomes: list[SessionOutcome] = []
        self._lock = threading.Lock()
        configure_logging(
            jsonl_path=self.log_path if self.apply else None,
            logger_name="herdr_roll",
        )
        self._log = get_logger("herdr_roll")

    def emit(self, payload: BaseModel, *, event_name: str = "step_summary") -> None:
        line = _dump_compact_json(payload)
        with self._lock:
            sys.stdout.write(f"{line}\n")
            sys.stdout.flush()
            self._log.info(
                event_name, run_id=self.run_id, step=self.step, payload=payload
            )

    def add_outcome(self, outcome: SessionOutcome) -> SessionOutcome:
        line = _dump_compact_json(outcome)
        with self._lock:
            self.outcomes.append(outcome)
            sys.stdout.write(f"{line}\n")
            sys.stdout.flush()
            self._log.info(
                "session_outcome",
                run_id=self.run_id,
                step=self.step,
                outcome=outcome,
            )
        return outcome

    def finish(self, rc: int) -> int:
        if self.apply:
            with self._lock:
                self.run_dir.mkdir(parents=True, exist_ok=True)
                ts, ns = now_ns_pair()
                all_errors: list[HerdrRollErrorEntry] = []
                for item in self.outcomes:
                    all_errors.extend(item.errors)
                ret_code = (
                    HerdrRollReturnCode.RET_OK
                    if rc == EXIT_OK
                    else (
                        HerdrRollReturnCode.RET_REFUSED
                        if rc == EXIT_REFUSED
                        else HerdrRollReturnCode.RET_FAILED
                    )
                )
                rec = RunRecord(
                    run_id=self.run_id,
                    step=self.step,
                    args=list(self.argv),
                    started_at=self.started_at,
                    ended_at=now_iso(),
                    rc=rc,
                    log_path=str(self.log_path),
                    outcomes=list(self.outcomes),
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=all_errors,
                    returns=[HerdrRollReturnEntry(code=ret_code)],
                )
                runs_file = self.root / "runs.jsonl"
                runs_file.parent.mkdir(parents=True, exist_ok=True)
                with runs_file.open("a", encoding="utf-8") as handle:
                    handle.write(_dump_compact_json(rec) + "\n")
                self._log.info(
                    "step_finished", run_id=self.run_id, step=self.step, rc=rc
                )
        return rc


def find_terminal_in_spaces(
    spaces: list[dict[str, Any]], address: str
) -> tuple[str, str, str] | None:
    """Return `(space_name, tab_id, tab_name)` for *address* in `herdr-app ls`, or None."""
    for sp in spaces:
        sname = str(sp.get("name") or "")
        for tb in sp.get("tabs") or ():
            if not isinstance(tb, dict):
                continue
            tid = str(tb.get("id") or "")
            tname = str(tb.get("name") or "")
            for pane in tb.get("terminals") or ():
                if isinstance(pane, dict) and str(pane.get("terminal")) == address:
                    return sname, tid, tname
    return None


def assert_same_tab(target: TargetTuple) -> None:
    """Verify terminal still lives in the expected space & tab ID before acting."""
    space, tab, term = target
    found: tuple[str, str, str] | None = None
    for attempt in range(2):
        found = find_terminal_in_spaces(herdr_app_ls(), term.address)
        if found is not None or attempt == 1:
            break
        time.sleep(0.5)
    if found is None:
        raise SkipStep("skipped-gone", f"herdr-app ls no longer lists {term.address}")
    sname, tid, tname = found
    if sname != space.name or tid != tab.id:
        raise SkipStep(
            "skipped-moved", f"{term.address} moved to {sname} / {tname} ({tid})"
        )


def _wait_for_exit(live: LiveClient, target: TargetTuple, timing: Timing) -> None:
    """Send exit command cleanly and wait until bare shell returns or terminal closes."""
    _, _, term = target
    adapter = ADAPTERS[term.kind]
    if not hc_send(term.address, adapter.exit_command(), raw=True):
        raise SkipStep("failed-exit-send", f"hc send {adapter.exit_command()} failed")

    dialog_handled = False

    def settled() -> str | None:
        nonlocal dialog_handled
        screen_text = hc_read(term.address, lines=0)
        if not dialog_handled and EXIT_DIALOG in screen_text:
            block = screen_text.split("will stop when you exit:", 1)[-1].split(
                "Exit and stop tasks", 1
            )[0]
            items = [
                ln.strip()
                for ln in block.splitlines()
                if ln.strip() and not ln.strip().startswith(("❯", "1.", "2."))
            ]
            crons_only = bool(items) and all(
                item.startswith("scheduled task") for item in items
            )
            hc_keys(term.address, "enter" if crons_only else "esc")
            dialog_handled = True
            if not crons_only:
                raise SkipStep("skipped-background-work", *items[:5])
            return None
        live.refresh_terminal(term.address)
        if term.address not in live.terminals:
            return "gone"
        _, fg_argv = live.foreground(term.address)
        if fg_argv and is_bare_shell(fg_argv):
            return "shell"
        if fg_argv and not harness_running(term.kind, fg_argv):
            return "exited"
        return None

    res = adaptive_poll(
        settled,
        timing.settle,
        min_interval=timing.poll_min,
        max_interval=timing.poll_max,
    )
    if res is None:
        raise SkipStep(
            "failed-exit-timeout",
            f"{term.address} did not exit within {timing.settle}s",
        )


def _handshake(
    live: LiveClient, target: TargetTuple, timing: Timing, token: str
) -> None:
    """Opt-in stop-point handshake requesting READY-TO-RESTART <token>."""
    _, _, term = target
    prompt = HANDSHAKE_TEMPLATE.format(token=token)
    if not hc_send(term.address, prompt, raw=False, wait=True, timeout=timing.wait):
        raise SkipStep("skipped-handshake-not-sent")

    def check_reply() -> str | None:
        text = hc_read(term.address, lines=80)
        for line in reversed(text.splitlines()):
            cleaned = line.strip(" │●•❯>")
            if (
                "reply with the single line" in cleaned
                or "(or BUSY-KEEP-ME" in cleaned
                or "From herdr rolling restart:" in cleaned
            ):
                continue
            if cleaned == f"{READY_MARKER} {token}":
                return "ready"
            if cleaned.startswith(f"{BUSY_MARKER} {token}"):
                return "busy"
        live.refresh_terminal(term.address)
        return None

    ans = adaptive_poll(
        check_reply, min(30.0, timing.wait), min_interval=0.5, max_interval=1.0
    )
    if ans == "busy":
        raise SkipStep("skipped-busy-keep-me", "session replied BUSY-KEEP-ME")
    if ans != "ready":
        raise SkipStep("skipped-no-handshake-ack", f"missing {READY_MARKER} {token}")


def _resolve_folder(target: TargetTuple) -> str:
    space, _, term = target
    if term.cwd and Path(term.cwd).is_dir():
        return term.cwd
    if space.folder and Path(space.folder).is_dir():
        return space.folder
    return str(Path.home())


def _launch_fresh_tab(
    target: TargetTuple,
    argv: list[str],
    cwd: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Spawn a new tab in the same space; return the new terminal address (old tab untouched until verified)."""
    space, tab, _ = target
    launch_argv = (
        ["/usr/bin/env", *(f"{k}={v}" for k, v in sorted(extra_env.items())), *argv]
        if extra_env
        else argv
    )
    resp = herdr_app_do(
        "tab.new",
        "--space",
        space.id,
        "--name",
        tab.name,
        "--folder",
        cwd,
        "--focus",
        "false",
        "--argv",
        orjson.dumps(launch_argv).decode("utf-8"),
    )
    if not resp or not resp.get("terminal"):
        raise SkipStep(
            "failed-open", f"herdr-app do tab.new failed for {space.name} / {tab.name}"
        )
    return str(resp["terminal"])


def verify_target(
    live: LiveClient,
    target: TargetTuple,
    *,
    address: str,
    old_pid: int | None,
    expected_cwd: str,
    expected_git: str | None,
    timing: Timing,
    in_place_before_started_at: int | None = None,
) -> tuple[int, str]:
    """Verify relaunched/resumed session with adaptive polling up to `timing.settle`."""
    _, _, term = target
    adapter = ADAPTERS[term.kind]
    if not term.session_id:
        raise SkipStep("failed-no-session-id")
    session_id = term.session_id

    def check() -> int | None:
        live.refresh_terminal(address)
        info = live.info.get(address) or {}
        if address not in live.terminals or not info:
            return None
        fg_pid, fg_argv = live.foreground(address)
        st_obj = info.get("status")
        st_state = (
            str(st_obj.get("state"))
            if isinstance(st_obj, dict) and st_obj.get("state")
            else None
        )
        screen_text = hc_read(address, lines=0)

        modal = adapter.blocking_dialog(screen_text, st_state)
        if modal:
            raise SkipStep("blocked-dialog", modal)

        if in_place_before_started_at is not None and term.kind is HarnessKind.CLAUDE:
            rec = claude_record_for_tree(fg_pid, live.tree(address))
            started_at = rec.get("startedAt") if rec else None
            if (
                isinstance(started_at, int)
                and started_at > in_place_before_started_at
                and rec
                and rec.get("sessionId") == session_id
                and st_state in {"idle", "done"}
            ):
                return fg_pid
            return None

        ok, _reason = adapter.verify_resumed(
            old_pid=old_pid,
            new_pid=fg_pid,
            fg_argv=fg_argv,
            tree_pids=live.tree(address),
            status_state=st_state,
            screen_text=screen_text,
            session_id=session_id,
        )
        if not ok:
            return None
        return fg_pid

    new_pid = adaptive_poll(
        check, timing.settle, min_interval=timing.poll_min, max_interval=timing.poll_max
    )
    if new_pid is None:
        live.refresh_terminal(address)
        fg_pid, fg_argv = live.foreground(address)
        info = live.info.get(address) or {}
        st_obj = info.get("status")
        st_state = str(st_obj.get("state")) if isinstance(st_obj, dict) else None
        if in_place_before_started_at is not None and term.kind is HarnessKind.CLAUDE:
            detail: str | None = (
                f"in-place /restart startedAt did not advance past {in_place_before_started_at}"
            )
        else:
            _, detail = adapter.verify_resumed(
                old_pid=old_pid,
                new_pid=fg_pid,
                fg_argv=fg_argv,
                tree_pids=live.tree(address),
                status_state=st_state,
                screen_text=hc_read(address, lines=0),
                session_id=session_id,
            )
        raise SkipStep(
            "failed-verify-timeout", detail or "did not verify before settle deadline"
        )

    live_cwd = lsof_cwd(new_pid) or str(
        ((live.info.get(address) or {}).get("foreground") or {}).get("cwd") or ""
    )
    if live_cwd:
        try:
            if Path(live_cwd).resolve() != Path(expected_cwd).resolve():
                raise SkipStep(
                    "failed-wrong-folder", f"expected {expected_cwd}, got {live_cwd}"
                )
        except OSError:
            pass

    if expected_git is not None:
        now_git = git_status_porcelain(expected_cwd)
        if now_git != expected_git:
            raise SkipStep(
                "failed-worktree-changed", "git status changed during restart"
            )

    return new_pid, address


def restart_one(
    target: TargetTuple,
    *,
    timing: Timing,
    run_dir: Path | None,
    apply: bool,
    handshake: bool = False,
    verify_turn: bool = False,
    fresh_tab: bool = False,
    force_clean_env: bool = True,
    live: LiveClient | None = None,
) -> SessionOutcome:
    """Execute dry-run planning or live rolling restart for a single target session."""
    space, tab, term = target
    where = f"{space.name} / {tab.name}"
    started = time.monotonic()

    def make_outcome(
        result: str,
        notes: tuple[str, ...] = (),
        *,
        new_pid: int | None = None,
        address_override: str | None = None,
        process_started_at: str | None = None,
        process_lstart: str | None = None,
        agy_log_proof: AgyLogProof | None = None,
    ) -> SessionOutcome:
        return make_session_outcome(
            where=where,
            tab_id=tab.id,
            terminal=address_override or term.address,
            kind=term.kind,
            session_id=term.session_id,
            result=result,
            old_pid=term.fg_pid or term.pid,
            new_pid=new_pid,
            process_started_at=process_started_at,
            process_lstart=process_lstart,
            agy_log_proof=agy_log_proof,
            elapsed_s=time.monotonic() - started,
            notes=notes,
        )

    adapter = ADAPTERS.get(term.kind)
    if adapter is None:
        return make_outcome("skipped-unsupported", (f"no adapter for {term.kind}",))
    if not term.session_id:
        return make_outcome(
            "skipped-no-session-id", ("unable to resolve resume session ID safely",)
        )

    cwd = _resolve_folder(target)
    screen_text = hc_read(term.address, lines=0)
    try:
        argv = adapter.resume_argv(
            term,
            session_id=term.session_id,
            is_coordinator=term.is_coordinator,
            prompt=None,
            screen_text=screen_text,
        )
    except AdapterSkipError as err:
        return make_outcome(err.code, err.notes)

    extra_env = adapter.extra_env(list(term.term_argv))
    if force_clean_env:
        relaunch_line = clean_relaunch_command(
            cwd, argv, terminal_address=term.address, extra_env=extra_env
        )
    else:
        env_prefix = (
            f"env {' '.join(f'{k}={shlex.quote(v)}' for k, v in sorted(extra_env.items()))} "
            if extra_env
            else ""
        )
        relaunch_line = f"cd {shlex.quote(cwd)} && {env_prefix}{' '.join(shlex.quote(tok) for tok in argv)}"

    if not apply:
        tree_for_plan = (
            live.tree(term.address)
            if live
            else ([] if term.fg_pid is None else [term.fg_pid])
        )
        if fresh_tab:
            mode = "fresh-tab"
        elif adapter.can_in_place_restart(
            term.fg_pid,
            tree_for_plan,
            force_clean_env=force_clean_env,
            fresh_tab=fresh_tab,
        ):
            mode = "in-place-fast"
        elif force_clean_env:
            mode = "in-place-clean-env"
        else:
            mode = "in-place-keep-env"
        return make_outcome(
            "planned", (f"mode={mode}", f"cwd={cwd}", f"cmd={relaunch_line}")
        )

    client = live if live is not None else LiveClient(max_workers=2)
    try:
        assert_same_tab(target)
        client.refresh_terminal(term.address)
        fg_pid, fg_argv = client.foreground(term.address)
        info = client.info.get(term.address) or {}
        st_obj = info.get("status")
        st_state = (
            str(st_obj.get("state"))
            if isinstance(st_obj, dict) and st_obj.get("state")
            else None
        )
        st_app = (
            str(st_obj.get("app"))
            if isinstance(st_obj, dict) and st_obj.get("app")
            else None
        )

        if not harness_running(term.kind, fg_argv, st_app):
            raise SkipStep(
                "skipped-foreground-changed", f"foreground is {program_name(fg_argv)!r}"
            )

        modal = adapter.blocking_dialog(hc_read(term.address, lines=0), st_state)
        if modal:
            raise SkipStep("blocked-dialog", modal)

        def is_ready_for_stop() -> bool | None:
            client.refresh_terminal(term.address)
            cur_pid, _ = client.foreground(term.address)
            cur_info = client.info.get(term.address) or {}
            cur_st = (cur_info.get("status") or {}).get("state")
            cur_st_str = str(cur_st) if isinstance(cur_st, str) else None
            scr = hc_read(term.address, lines=0)
            if not adapter.is_idle(cur_st_str, cur_pid, client.tree(term.address), scr):
                return None
            jobs = client.active_jobs(
                term.address,
                term.kind,
                HARNESS_BINARIES,
                confirm_delay=timing.job_confirm_delay,
            )
            return True if not jobs else None

        if timing.busy_wait > 0:
            idle_ok = adaptive_poll(
                is_ready_for_stop, timing.busy_wait, min_interval=2.0, max_interval=5.0
            )
        else:
            idle_ok = is_ready_for_stop()
        if not idle_ok:
            jobs = client.active_jobs(
                term.address, term.kind, HARNESS_BINARIES, confirm_delay=0.0
            )
            detail = jobs[0] if jobs else f"status={st_state}"
            raise SkipStep("skipped-busy", detail)

        git_before = git_status_porcelain(cwd)
        if run_dir is not None:
            r_ts, r_ns = now_ns_pair()
            receipt = ReceiptRecord(
                where=where,
                tab_id=tab.id,
                terminal=term.address,
                kind=term.kind,
                session_id=term.session_id,
                cwd=cwd,
                old_pid=fg_pid,
                git_status=git_before,
                relaunch_argv=argv,
                timestamp=r_ts,
                timestamp_unix_ns=r_ns,
            )
            write_atomic(
                run_dir / "receipts" / f"{term.address}.json",
                _dump_pretty_json(receipt),
            )

        if handshake:
            token = f"{run_dir.name if run_dir else 'manual'}-{term.address}"
            _handshake(client, target, timing, token)
            if not is_ready_for_stop():
                raise SkipStep("skipped-busy-after-handshake")

        def run_optional_verify_turn(addr: str) -> None:
            if verify_turn and not hc_send(
                addr,
                VERIFY_PROMPT,
                raw=False,
                wait=True,
                timeout=min(45.0, timing.wait),
            ):
                raise SkipStep("failed-verify-turn", "verify prompt send failed")

        tree_now = client.tree(term.address)
        if adapter.can_in_place_restart(
            fg_pid, tree_now, force_clean_env=force_clean_env, fresh_tab=fresh_tab
        ):
            cmd_in_place = adapter.in_place_command()
            rec_before = claude_record_for_tree(fg_pid, tree_now)
            started_before = rec_before.get("startedAt") if rec_before else None
            if not cmd_in_place or not isinstance(started_before, int):
                raise SkipStep("failed-in-place-restart")
            if not hc_send(term.address, cmd_in_place, raw=True):
                raise SkipStep("failed-in-place-send")
            new_pid, verified_addr = verify_target(
                client,
                target,
                address=term.address,
                old_pid=fg_pid,
                expected_cwd=cwd,
                expected_git=git_before,
                timing=timing,
                in_place_before_started_at=started_before,
            )
            run_optional_verify_turn(verified_addr)
            p_started_at, p_lstart, p_agy, _ = _collect_verify_proof(
                client, verified_addr, term.kind, new_pid, term.session_id
            )
            return make_outcome(
                "restarted",
                ("in-place /restart",),
                new_pid=new_pid,
                address_override=verified_addr,
                process_started_at=p_started_at,
                process_lstart=p_lstart,
                agy_log_proof=p_agy,
            )

        if fresh_tab:
            _wait_for_exit(client, target, timing)
            new_addr = _launch_fresh_tab(target, argv, cwd, extra_env=extra_env)
            new_pid, verified_addr = verify_target(
                client,
                target,
                address=new_addr,
                old_pid=fg_pid,
                expected_cwd=cwd,
                expected_git=git_before,
                timing=timing,
            )
            run_optional_verify_turn(verified_addr)
            p_started_at, p_lstart, p_agy, _ = _collect_verify_proof(
                client, verified_addr, term.kind, new_pid, term.session_id
            )
            if len(tab.terminals) == 1 and (
                herdr_app_do("tab.close", "--id", tab.id, "--confirmed", "true") is None
            ):
                raise SkipStep(
                    "failed-tab-close",
                    f"new tab {verified_addr} verified but failed to close {tab.id}",
                )
            return make_outcome(
                "ok",
                (f"fresh-tab={verified_addr}",),
                new_pid=new_pid,
                address_override=verified_addr,
                process_started_at=p_started_at,
                process_lstart=p_lstart,
                agy_log_proof=p_agy,
            )

        _wait_for_exit(client, target, timing)
        assert_same_tab(target)
        if not hc_send(term.address, relaunch_line, raw=True):
            raise SkipStep("failed-relaunch-send", "hc send relaunch command failed")
        new_pid, verified_addr = verify_target(
            client,
            target,
            address=term.address,
            old_pid=fg_pid,
            expected_cwd=cwd,
            expected_git=git_before,
            timing=timing,
        )
        run_optional_verify_turn(verified_addr)
        p_started_at, p_lstart, p_agy, _ = _collect_verify_proof(
            client, verified_addr, term.kind, new_pid, term.session_id
        )
        return make_outcome(
            "ok",
            ("clean-env-in-place" if force_clean_env else "keep-env-in-place",),
            new_pid=new_pid,
            address_override=verified_addr,
            process_started_at=p_started_at,
            process_lstart=p_lstart,
            agy_log_proof=p_agy,
        )

    except SkipStep as skip:
        return make_outcome(skip.result, skip.notes)


def cmd_snapshot(
    *,
    tabs: tuple[str, ...] = (),
    spaces: tuple[str, ...] = (),
    harnesses: tuple[HarnessKind, ...] = (),
    all_targets: bool = False,
    exclude: tuple[str, ...] = (),
    apply: bool = False,
    argv: tuple[str, ...] = (),
) -> int:
    """Capture live Herdr graph snapshot (dry-run reports without writing unless `--apply`)."""
    from herdr_roll.graph import select_targets

    root = state_dir()
    run_id = new_run_id("snapshot")
    rec = StepRecorder(
        root=root,
        run_id=run_id,
        step="snapshot",
        argv=argv or ("snapshot",),
        apply=apply,
    )
    graph_file = root / "graph.json"
    live = LiveClient()
    current = build_graph(live)

    try:
        if apply:
            with acquire_lock(root / "roll.lock"):
                previous = load_graph(graph_file) if graph_file.is_file() else None
                merged = merge_graph(current, previous)
                payload = _dump_pretty_json(merged)
                stamp = format_ns_timestamp()
                write_atomic(graph_file, payload)
                write_atomic(root / f"graph-{stamp}.json", payload)
                write_atomic(rec.run_dir / "graph.json", payload)
                write_atomic(root / "ledger.md", render_ledger(merged))
        else:
            previous = load_graph(graph_file) if graph_file.is_file() else None
            merged = merge_graph(current, previous)

        if tabs or spaces or harnesses or all_targets:
            targets = select_targets(
                merged,
                tabs=tabs,
                spaces=spaces,
                harnesses=harnesses,
                all_targets=all_targets,
                exclude=exclude,
                caller_id=live.me,
            )
        else:
            targets = []

        agents_live = [
            term
            for sp in merged.spaces
            if not sp.kept_from_previous_graph
            for tb in sp.tabs
            if not tb.kept_from_previous_graph
            for term in tb.terminals
            if term.kind is not HarnessKind.SHELL
        ]
        missing_sid = [
            f"{t.address} ({t.kind.value})" for t in agents_live if not t.session_id
        ]
        s_ts, s_ns = now_ns_pair()
        errors = (
            [
                HerdrRollErrorEntry(
                    code=HerdrRollErrorCode.ERR_NO_SESSION_ID,
                    message=f"{len(missing_sid)} live agent(s) without resolved session_id",
                    detail={"agents": missing_sid},
                )
            ]
            if missing_sid
            else []
        )
        ret_code = (
            HerdrRollReturnCode.RET_SNAPSHOT_CAPTURED
            if apply
            else HerdrRollReturnCode.RET_PLANNED
        )
        summary = SnapshotSummary(
            run_id=run_id,
            apply=apply,
            spaces=len(merged.spaces),
            agents_live=len(agents_live),
            agents_without_session=missing_sid,
            selected_targets=len(targets),
            timestamp=s_ts,
            timestamp_unix_ns=s_ns,
            errors=errors,
            returns=[HerdrRollReturnEntry(code=ret_code)],
        )
        rec.emit(summary, event_name="snapshot_summary")
    except Exception:
        rec.finish(EXIT_FAILED)
        raise
    return rec.finish(EXIT_OK)


def cmd_restart(
    *,
    tabs: tuple[str, ...] = (),
    spaces: tuple[str, ...] = (),
    harnesses: tuple[HarnessKind, ...] = (),
    all_targets: bool = False,
    exclude: tuple[str, ...] = (),
    concurrency: int = 4,
    timing: Timing | None = None,
    handshake: bool = False,
    verify_turn: bool = False,
    fresh_tab: bool = False,
    force_clean_env: bool = True,
    close_self_on_ok: bool = False,
    apply: bool = False,
    argv: tuple[str, ...] = (),
) -> int:
    """Plan or run wave-ordered bounded-parallel rolling restarts."""
    from herdr_roll.graph import select_targets

    root = state_dir()
    timing_cfg = timing or Timing()
    run_id = new_run_id("restart")
    rec = StepRecorder(
        root=root, run_id=run_id, step="restart", argv=argv or ("restart",), apply=apply
    )
    live = LiveClient()
    graph = build_graph(live)

    targets = select_targets(
        graph,
        tabs=tabs,
        spaces=spaces,
        harnesses=harnesses,
        all_targets=all_targets,
        exclude=exclude,
        caller_id=live.me,
    )

    if live.me and any(tm.address == live.me for _, _, tm in targets):
        raise RefusedError(
            f"Cannot restart own running tab {live.me} directly; run `mise run roll:self --apply` instead."
        )

    wave0 = [t for t in targets if role_of(t[0].name, t[1].name) is Role.COORDINATOR]
    wave1 = [t for t in targets if role_of(t[0].name, t[1].name) is Role.CHILD]
    wave2 = [t for t in targets if role_of(t[0].name, t[1].name) is Role.LEAD]

    def run_item(item: TargetTuple) -> SessionOutcome:
        return restart_one(
            item,
            timing=timing_cfg,
            run_dir=rec.run_dir if apply else None,
            apply=apply,
            handshake=handshake,
            verify_turn=verify_turn,
            fresh_tab=fresh_tab,
            force_clean_env=force_clean_env,
        )

    def execute_wave(items: list[TargetTuple], workers: int) -> list[SessionOutcome]:
        if not items:
            return []
        if not apply or workers <= 1 or len(items) == 1:
            return [rec.add_outcome(run_item(item)) for item in items]
        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
            futures = [pool.submit(run_item, item) for item in items]
            return [rec.add_outcome(fut.result()) for fut in as_completed(futures)]

    rc = EXIT_OK
    try:
        if apply:
            with acquire_lock(root / "roll.lock"):
                write_atomic(
                    rec.run_dir / "graph.json",
                    _dump_pretty_json(graph),
                )
                w0_outcomes = execute_wave(wave0, 1)
                if any(
                    o.result.startswith(("failed-", "blocked-")) for o in w0_outcomes
                ):
                    rc = EXIT_FAILED
                else:
                    execute_wave(wave1, max(1, concurrency))
                    execute_wave(wave2, max(1, concurrency))
        else:
            execute_wave(wave0, 1)
            execute_wave(wave1, 1)
            execute_wave(wave2, 1)
    except Exception:
        rec.finish(EXIT_FAILED)
        raise
    if any(o.result.startswith(("failed-", "blocked-")) for o in rec.outcomes):
        rc = EXIT_FAILED
    rc = rec.finish(rc)
    if apply and close_self_on_ok and rc == EXIT_OK and live.me:
        self_loc = find_terminal_in_spaces(herdr_app_ls(), live.me)
        if self_loc and self_loc[2] == "roll":
            herdr_app_do("tab.close", "--id", self_loc[1], "--confirmed", "true")
    return rc


def cmd_stop(
    *,
    tabs: tuple[str, ...] = (),
    spaces: tuple[str, ...] = (),
    harnesses: tuple[HarnessKind, ...] = (),
    all_targets: bool = False,
    exclude: tuple[str, ...] = (),
    timing: Timing | None = None,
    handshake: bool = False,
    apply: bool = False,
    argv: tuple[str, ...] = (),
) -> int:
    """Stop selected sessions cleanly into bare shells (for shutdown/reboot preparation)."""
    from herdr_roll.graph import select_targets

    root = state_dir()
    timing_cfg = timing or Timing()
    run_id = new_run_id("stop")
    rec = StepRecorder(
        root=root, run_id=run_id, step="stop", argv=argv or ("stop",), apply=apply
    )
    live = LiveClient()
    graph = build_graph(live)
    targets = select_targets(
        graph,
        tabs=tabs,
        spaces=spaces,
        harnesses=harnesses,
        all_targets=all_targets,
        exclude=exclude,
        caller_id=live.me,
    )

    def stop_one(item: TargetTuple) -> SessionOutcome:
        space, tab, term = item
        where = f"{space.name} / {tab.name}"
        t0 = time.monotonic()
        adapter = ADAPTERS.get(term.kind)
        if not adapter or not term.session_id:
            return make_session_outcome(
                where=where,
                tab_id=tab.id,
                terminal=term.address,
                kind=term.kind,
                session_id=term.session_id,
                result="skipped-no-session-id",
            )
        cwd = _resolve_folder(item)
        if not apply:
            return make_session_outcome(
                where=where,
                tab_id=tab.id,
                terminal=term.address,
                kind=term.kind,
                session_id=term.session_id,
                result="planned",
                old_pid=term.fg_pid,
                notes=(f"cmd={adapter.exit_command()}", f"cwd={cwd}"),
            )
        try:
            assert_same_tab(item)
            live.refresh_terminal(term.address)
            fg_pid, fg_argv = live.foreground(term.address)
            info = live.info.get(term.address) or {}
            st_obj = info.get("status")
            st_state = (
                str(st_obj.get("state"))
                if isinstance(st_obj, dict) and st_obj.get("state")
                else None
            )
            st_app = (
                str(st_obj.get("app"))
                if isinstance(st_obj, dict) and st_obj.get("app")
                else None
            )
            if not harness_running(term.kind, fg_argv, st_app):
                raise SkipStep(
                    "skipped-foreground-changed",
                    f"foreground is {program_name(fg_argv)!r}",
                )
            modal = adapter.blocking_dialog(hc_read(term.address, lines=0), st_state)
            if modal:
                raise SkipStep("blocked-dialog", modal)

            def is_ready_for_stop() -> bool | None:
                live.refresh_terminal(term.address)
                cur_pid, _ = live.foreground(term.address)
                cur_info = live.info.get(term.address) or {}
                cur_st = (cur_info.get("status") or {}).get("state")
                cur_st_str = str(cur_st) if isinstance(cur_st, str) else None
                scr = hc_read(term.address, lines=0)
                if not adapter.is_idle(
                    cur_st_str, cur_pid, live.tree(term.address), scr
                ):
                    return None
                jobs = live.active_jobs(
                    term.address,
                    term.kind,
                    HARNESS_BINARIES,
                    confirm_delay=timing_cfg.job_confirm_delay,
                )
                return True if not jobs else None

            if timing_cfg.busy_wait > 0:
                idle_ok = adaptive_poll(
                    is_ready_for_stop,
                    timing_cfg.busy_wait,
                    min_interval=2.0,
                    max_interval=5.0,
                )
            else:
                idle_ok = is_ready_for_stop()
            if not idle_ok:
                jobs = live.active_jobs(
                    term.address, term.kind, HARNESS_BINARIES, confirm_delay=0.0
                )
                detail = jobs[0] if jobs else f"status={st_state}"
                raise SkipStep("skipped-busy", detail)

            if handshake:
                _handshake(live, item, timing_cfg, f"{run_id}-{term.address}")
                if not is_ready_for_stop():
                    raise SkipStep("skipped-busy-after-handshake")
            _wait_for_exit(live, item, timing_cfg)
            return make_session_outcome(
                where=where,
                tab_id=tab.id,
                terminal=term.address,
                kind=term.kind,
                session_id=term.session_id,
                result="stopped",
                old_pid=fg_pid or term.fg_pid,
                elapsed_s=time.monotonic() - t0,
            )
        except SkipStep as err:
            return make_session_outcome(
                where=where,
                tab_id=tab.id,
                terminal=term.address,
                kind=term.kind,
                session_id=term.session_id,
                result=err.result,
                old_pid=term.fg_pid,
                elapsed_s=time.monotonic() - t0,
                notes=err.notes,
            )

    try:
        if apply:
            with acquire_lock(root / "roll.lock"):
                graph_file = root / "graph.json"
                previous = load_graph(graph_file) if graph_file.is_file() else None
                merged = merge_graph(graph, previous)
                payload = _dump_pretty_json(merged)
                write_atomic(rec.run_dir / "graph.json", payload)
                write_atomic(graph_file, payload)
                for item in targets:
                    rec.add_outcome(stop_one(item))
        else:
            for item in targets:
                rec.add_outcome(stop_one(item))
    except Exception:
        rec.finish(EXIT_FAILED)
        raise
    rc = (
        EXIT_FAILED
        if any(o.result.startswith(("failed-", "blocked-")) for o in rec.outcomes)
        else EXIT_OK
    )
    return rec.finish(rc)


def cmd_start(
    *,
    run_id: str | None = None,
    graph_path: Path | None = None,
    tabs: tuple[str, ...] = (),
    spaces: tuple[str, ...] = (),
    harnesses: tuple[HarnessKind, ...] = (),
    all_targets: bool = False,
    exclude: tuple[str, ...] = (),
    prompt: str | None = None,
    fresh_tab: bool = False,
    concurrency: int = 4,
    timing: Timing | None = None,
    apply: bool = False,
    argv: tuple[str, ...] = (),
) -> int:
    """Resume sessions from a prior stop run or saved reboot `graph.json`."""
    from herdr_roll.graph import select_targets

    root = state_dir()
    timing_cfg = timing or Timing()
    picked_run = (
        run_id or last_run_id(root, {"stop", "snapshot"}) or new_run_id("start")
    )
    candidate_graph = graph_path or (root / picked_run / "graph.json")
    if run_id is None and graph_path is None and not candidate_graph.is_file():
        candidate_graph = root / "graph.json"
    if not candidate_graph.is_file():
        raise RefusedError(
            f"No saved graph found at {candidate_graph}; run roll:snapshot --apply first."
        )

    saved = load_graph(candidate_graph)
    live = LiveClient()
    targets = select_targets(
        saved,
        tabs=tabs,
        spaces=spaces,
        harnesses=harnesses,
        all_targets=all_targets or not (tabs or spaces or harnesses),
        exclude=exclude,
        caller_id=live.me,
        recover=True,
    )
    rec = StepRecorder(
        root=root, run_id=picked_run, step="start", argv=argv or ("start",), apply=apply
    )

    def start_one(item: TargetTuple, worker_client: LiveClient) -> SessionOutcome:
        space, tab, term = item
        where = f"{space.name} / {tab.name}"
        t0 = time.monotonic()
        adapter = ADAPTERS.get(term.kind)
        if not adapter or not term.session_id:
            return make_session_outcome(
                where=where,
                tab_id=tab.id,
                terminal=term.address,
                kind=term.kind,
                session_id=term.session_id,
                result="skipped-no-session-id",
            )
        cwd = _resolve_folder(item)
        try:
            resume_cmd = adapter.resume_argv(
                term,
                session_id=term.session_id,
                is_coordinator=term.is_coordinator,
                prompt=prompt,
                screen_text="",
            )
        except AdapterSkipError as err:
            return make_session_outcome(
                where=where,
                tab_id=tab.id,
                terminal=term.address,
                kind=term.kind,
                session_id=term.session_id,
                result=err.code,
                notes=err.notes,
            )
        extra_env = adapter.extra_env(list(term.term_argv))
        relaunch_line = clean_relaunch_command(
            cwd,
            resume_cmd,
            terminal_address=term.address,
            extra_env=extra_env,
        )
        if not apply:
            return make_session_outcome(
                where=where,
                tab_id=tab.id,
                terminal=term.address,
                kind=term.kind,
                session_id=term.session_id,
                result="planned",
                notes=(f"cwd={cwd}", f"cmd={relaunch_line}"),
            )
        try:
            loc = find_terminal_in_spaces(herdr_app_ls(), term.address)
            if not fresh_tab and loc and loc[0] == space.name and loc[1] == tab.id:
                worker_client.refresh_terminal(term.address)
                _, fg_argv = worker_client.foreground(term.address)
                if not is_bare_shell(fg_argv):
                    raise SkipStep(
                        "skipped-tab-busy", "tab foreground is not a bare shell"
                    )
                if not hc_send(term.address, relaunch_line, raw=True):
                    raise SkipStep("failed-relaunch-send")
                addr = term.address
            else:
                addr = _launch_fresh_tab(item, resume_cmd, cwd, extra_env=extra_env)
            new_pid, verified_addr = verify_target(
                worker_client,
                item,
                address=addr,
                old_pid=None,
                expected_cwd=cwd,
                expected_git=None,
                timing=timing_cfg,
            )
            p_started_at, p_lstart, p_agy, _ = _collect_verify_proof(
                worker_client, verified_addr, term.kind, new_pid, term.session_id
            )
            return make_session_outcome(
                where=where,
                tab_id=tab.id,
                terminal=verified_addr,
                kind=term.kind,
                session_id=term.session_id,
                result="started",
                new_pid=new_pid,
                process_started_at=p_started_at,
                process_lstart=p_lstart,
                agy_log_proof=p_agy,
                elapsed_s=time.monotonic() - t0,
            )
        except SkipStep as err:
            return make_session_outcome(
                where=where,
                tab_id=tab.id,
                terminal=term.address,
                kind=term.kind,
                session_id=term.session_id,
                result=err.result,
                elapsed_s=time.monotonic() - t0,
                notes=err.notes,
            )

    wave0 = [t for t in targets if role_of(t[0].name, t[1].name) is Role.COORDINATOR]
    wave1 = [t for t in targets if role_of(t[0].name, t[1].name) is Role.CHILD]
    wave2 = [t for t in targets if role_of(t[0].name, t[1].name) is Role.LEAD]

    def execute_start_wave(
        items: list[TargetTuple], workers: int
    ) -> list[SessionOutcome]:
        if not items:
            return []
        if not apply or workers <= 1 or len(items) == 1:
            return [rec.add_outcome(start_one(item, live)) for item in items]
        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
            futures = [
                pool.submit(start_one, item, LiveClient(max_workers=2))
                for item in items
            ]
            return [rec.add_outcome(fut.result()) for fut in as_completed(futures)]

    try:
        if apply:
            with acquire_lock(root / "roll.lock"):
                execute_start_wave(wave0, 1)
                execute_start_wave(wave1, max(1, concurrency))
                execute_start_wave(wave2, max(1, concurrency))
        else:
            execute_start_wave(wave0, 1)
            execute_start_wave(wave1, 1)
            execute_start_wave(wave2, 1)
    except Exception:
        rec.finish(EXIT_FAILED)
        raise

    rc = (
        EXIT_FAILED
        if any(o.result.startswith(("failed-", "blocked-")) for o in rec.outcomes)
        else EXIT_OK
    )
    return rec.finish(rc)


def _collect_verify_proof(
    live: LiveClient,
    address: str,
    kind: HarnessKind,
    pid: int | None,
    session_id: str | None,
) -> tuple[str | None, str | None, AgyLogProof | None, list[str]]:
    """Gather `psutil` start timestamps and (for `agy`) CLI log PID/conv/hook confirmation."""
    started_at, lstart = process_start_info(pid)
    notes: list[str] = []
    if started_at:
        notes.append(f"started_at={started_at}")
    if lstart:
        notes.append(f"lstart={lstart}")
    agy_proof: AgyLogProof | None = None
    if kind is HarnessKind.AGY:
        _, fg_argv = live.foreground(address)
        agy_proof = inspect_agy_cli_log(live.tree(address), session_id, fg_argv=fg_argv)
        if agy_proof.log_path:
            notes.append(f"cli_log={Path(agy_proof.log_path).name}")
            notes.append(f"cli_pid={agy_proof.pid}")
            notes.append(f"cli_conv={agy_proof.conversation_id or '-'}")
            notes.append(
                f"hooks={agy_proof.loaded_hook_count}/{agy_proof.expected_hook_count}"
            )
    return started_at, lstart, agy_proof, notes


def cmd_verify(
    *,
    run_id: str | None = None,
    tabs: tuple[str, ...] = (),
    spaces: tuple[str, ...] = (),
    harnesses: tuple[HarnessKind, ...] = (),
    all_targets: bool = False,
    exclude: tuple[str, ...] = (),
    timing: Timing | None = None,
    apply: bool = False,
    argv: tuple[str, ...] = (),
) -> int:
    """Verify selected sessions are running their expected threads, CWD, and clean dialogs."""
    from herdr_roll.graph import select_targets

    root = state_dir()
    timing_cfg = timing or Timing()
    picked_run = (
        run_id
        or last_run_id(root, {"restart", "start", "stop"})
        or new_run_id("verify")
    )
    live = LiveClient()
    graph = build_graph(live)
    targets = select_targets(
        graph,
        tabs=tabs,
        spaces=spaces,
        harnesses=harnesses,
        all_targets=all_targets or not (tabs or spaces or harnesses),
        exclude=exclude,
        caller_id=live.me,
    )
    rec = StepRecorder(
        root=root,
        run_id=picked_run,
        step="verify",
        argv=argv or ("verify",),
        apply=apply,
    )

    def run_all() -> None:
        for item in targets:
            space, tab, term = item
            where = f"{space.name} / {tab.name}"
            t0 = time.monotonic()
            cwd = _resolve_folder(item)
            try:
                new_pid, verified_addr = verify_target(
                    live,
                    item,
                    address=term.address,
                    old_pid=None,
                    expected_cwd=cwd,
                    expected_git=None,
                    timing=timing_cfg,
                )
                started_at, lstart, agy_proof, proof_notes = _collect_verify_proof(
                    live, verified_addr, term.kind, new_pid, term.session_id
                )
                if (
                    agy_proof is not None
                    and agy_proof.log_path is not None
                    and not agy_proof.verified
                ):
                    mismatch: list[str] = []
                    if not agy_proof.pid_matched:
                        mismatch.append(f"cli_pid={agy_proof.pid} not in tree")
                    if term.session_id and not agy_proof.conversation_matched:
                        mismatch.append(
                            f"cli_conv={agy_proof.conversation_id} != {term.session_id}"
                        )
                    if (
                        agy_proof.expected_hook_count is not None
                        and not agy_proof.hooks_matched
                    ):
                        mismatch.append(
                            f"loaded hooks {agy_proof.loaded_hook_count} != hooks.json {agy_proof.expected_hook_count}"
                        )
                    rec.add_outcome(
                        make_session_outcome(
                            where=where,
                            tab_id=tab.id,
                            terminal=verified_addr,
                            kind=term.kind,
                            session_id=term.session_id,
                            result="failed-verify-timeout",
                            new_pid=new_pid,
                            process_started_at=started_at,
                            process_lstart=lstart,
                            agy_log_proof=agy_proof,
                            elapsed_s=time.monotonic() - t0,
                            notes=(*mismatch, *proof_notes),
                        )
                    )
                    continue
                rec.add_outcome(
                    make_session_outcome(
                        where=where,
                        tab_id=tab.id,
                        terminal=verified_addr,
                        kind=term.kind,
                        session_id=term.session_id,
                        result="ok",
                        new_pid=new_pid,
                        process_started_at=started_at,
                        process_lstart=lstart,
                        agy_log_proof=agy_proof,
                        elapsed_s=time.monotonic() - t0,
                        notes=proof_notes,
                    )
                )
            except SkipStep as err:
                fg_pid, _ = live.foreground(term.address)
                started_at, lstart, agy_proof, proof_notes = _collect_verify_proof(
                    live,
                    term.address,
                    term.kind,
                    fg_pid or term.fg_pid,
                    term.session_id,
                )
                rec.add_outcome(
                    make_session_outcome(
                        where=where,
                        tab_id=tab.id,
                        terminal=term.address,
                        kind=term.kind,
                        session_id=term.session_id,
                        result=err.result,
                        process_started_at=started_at,
                        process_lstart=lstart,
                        agy_log_proof=agy_proof,
                        elapsed_s=time.monotonic() - t0,
                        notes=(*err.notes, *proof_notes),
                    )
                )

    try:
        if apply:
            with acquire_lock(root / "roll.lock"):
                run_all()
        else:
            run_all()
    except Exception:
        rec.finish(EXIT_FAILED)
        raise

    rc = (
        EXIT_FAILED
        if any(o.result.startswith(("failed-", "blocked-")) for o in rec.outcomes)
        else EXIT_OK
    )
    return rec.finish(rc)


def cmd_self(
    *,
    fresh_tab: bool = False,
    busy_wait: float = 30.0,
    settle: float = 45.0,
    handshake: bool = False,
    apply: bool = False,
) -> int:
    """Spawn a detached parentless `roll` tab in the caller's space to restart `$HC_TERMINAL_ID` safely."""
    configure_logging(logger_name="herdr_roll")
    log = get_logger("herdr_roll")

    me = os.environ.get("HC_TERMINAL_ID")
    if not me:
        raise RefusedError(
            "HC_TERMINAL_ID is not set; roll:self must run inside a Herdr terminal."
        )

    live = LiveClient()
    graph = build_graph(live)
    found = next(
        (
            (sp, tb, tm)
            for sp in graph.spaces
            for tb in sp.tabs
            for tm in tb.terminals
            if tm.address == me
        ),
        None,
    )
    if not found:
        raise RefusedError(f"Unable to locate terminal {me} in `herdr-app ls`.")
    space, tab, term = found
    space_id, space_name, tab_name = space.id, space.name, tab.name
    if tab_name == "roll":
        raise RefusedError(
            "Refusing to spawn recursive roll tab from within an existing 'roll' tab."
        )
    if term.kind is HarnessKind.SHELL:
        agents = [
            (tb, tm)
            for tb in space.tabs
            for tm in tb.terminals
            if tm.kind is not HarnessKind.SHELL
        ]
        if agents:
            listed = ", ".join(f"{tb.name} ({tm.address})" for tb, tm in agents)
            hint = f"Run `mise run roll:restart --tabs {agents[0][1].address} --apply` (live agent tabs in {space_name}: {listed})."
        else:
            hint = "Run `mise run roll:restart --tabs <tab> --apply` instead."
        raise RefusedError(
            f"Terminal {tab_name} ({me}) is a bare shell tab, not an agent session; "
            f"`roll:self` only restarts the active agent tab itself. {hint}"
        )
    if not term.session_id:
        raise RefusedError(
            f"Cannot self-restart {tab_name} ({me}) ({term.kind.value}): unable to resolve a resumable session ID."
        )

    lib_root = str(Path(__file__).resolve().parent.parent)
    child_argv = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); from herdr_roll.cli import main; raise SystemExit(main(sys.argv[2:]))",
        lib_root,
        "restart",
        "--tabs",
        me,
        "--busy-wait",
        f"{busy_wait:g}",
        "--settle",
        f"{settle:g}",
        "--close-self-on-ok",
        "--apply",
    ]
    if fresh_tab:
        child_argv.append("--fresh-tab")
    if handshake:
        child_argv.append("--handshake")

    ts, ns = now_ns_pair()
    if not apply:
        plan = SelfPlanResponse(
            action="tab.new",
            space=space_name,
            space_id=space_id,
            name="roll",
            target_terminal=f"{tab_name} ({me})",
            folder=str(Path.home()),
            focus=False,
            argv=child_argv,
            apply=False,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[],
            returns=[HerdrRollReturnEntry(code=HerdrRollReturnCode.RET_PLANNED)],
        )
        sys.stdout.write(_dump_pretty_json(plan))
        sys.stdout.flush()
        log.info("roll_self_planned", response=plan)
        return EXIT_OK

    resp = herdr_app_do(
        "tab.new",
        "--space",
        space_id,
        "--name",
        "roll",
        "--folder",
        str(Path.home()),
        "--focus",
        "false",
        "--argv",
        orjson.dumps(child_argv).decode("utf-8"),
    )
    if not resp or not resp.get("terminal"):
        raise RefusedError(
            f"Failed to spawn detached `roll` tab in space {space_name} ({space_id})."
        )

    r_ts, r_ns = now_ns_pair()
    result = SelfPlanResponse(
        action="tab.new",
        space=space_name,
        space_id=space_id,
        name="roll",
        target_terminal=f"{tab_name} ({me})",
        folder=str(Path.home()),
        focus=False,
        argv=child_argv,
        apply=True,
        spawned_tab_id=str(resp.get("tab")) if resp.get("tab") is not None else None,
        spawned_terminal=str(resp.get("terminal")),
        next_hint=f"End your turn cleanly so roll ({resp.get('terminal')}) restarts {tab_name} ({me}).",
        timestamp=r_ts,
        timestamp_unix_ns=r_ns,
        errors=[],
        returns=[HerdrRollReturnEntry(code=HerdrRollReturnCode.RET_SELF_SPAWNED)],
    )
    sys.stdout.write(_dump_pretty_json(result))
    sys.stdout.flush()
    log.info("roll_self_spawned", response=result)
    return EXIT_OK
