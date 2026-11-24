#!/usr/bin/env -S /usr/bin/python3 -I -B
"""Portable /usr/bin/python3 (3.9+) SessionStart + Stop /learn hook for Claude Code and Codex CLI."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PHASE_HEADING_RE = re.compile(r"^\s*#{2,6}\s+Phase\b", re.IGNORECASE)
STATUS_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*Status:\*\*|Status:)\s*([a-zA-Z_-]+)",
    re.IGNORECASE,
)
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
ERROR_HEADING_RE = re.compile(r"^(#{1,6})\s+Errors\s+Encountered\b", re.IGNORECASE)
ANY_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+\S")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MISE_PLAN_ID_RE = re.compile(r"""^\s*PLAN_ID\s*=\s*["']([^"']+)["']""")
ERROR_PLACEHOLDER_CELLS = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "none",
        "none yet",
        "n/a",
        "na",
        "null",
        "nil",
        "no errors",
        "no error",
    }
)
VALID_CLEAR_OUTCOMES = frozenset({"proposed", "no_new_proposals", "applied"})


def _now_ns_pair() -> tuple[str, int]:
    ns = time.time_ns()
    sec, rem = divmod(ns, 1_000_000_000)
    local_dt = datetime.fromtimestamp(sec, tz=timezone.utc).astimezone()
    ts = f"{local_dt.strftime('%Y%m%dT%H%M%S')}.{rem:09d}{local_dt.strftime('%z')}"
    return ts, ns


def _valid_slug(val: Any) -> bool:
    return isinstance(val, str) and bool(SLUG_RE.match(val)) and ".." not in val


def _resolve_state_dir() -> Path:
    override = os.environ.get("AGY_LEARN_STATE_DIR", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(os.path.expanduser(xdg_state))
        if xdg_state
        else Path.home() / ".local" / "state"
    )
    return base / "agy-learn"


def _is_bare_plan_key(raw_key: str) -> bool:
    stripped = raw_key.strip()
    if not stripped:
        return False
    return (
        "/" not in stripped
        and "\\" not in stripped
        and not stripped.endswith("task_plan.md")
    )


def _normalize_plan_key(raw_key: str, repo_root_raw: Any = None) -> str:
    stripped = raw_key.strip()
    if _is_bare_plan_key(stripped):
        return stripped
    expanded = Path(os.path.expanduser(stripped))
    if expanded.is_absolute():
        try:
            return str(expanded.resolve(strict=False))
        except OSError:
            return stripped
    if isinstance(repo_root_raw, str) and repo_root_raw.strip():
        try:
            base = Path(os.path.expanduser(repo_root_raw.strip()))
            return str((base / expanded).resolve(strict=False))
        except OSError:
            return stripped
    return expanded.as_posix()


def _read_jsonl_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.is_file():
        return [], 0
    rows: list[dict[str, Any]] = []
    parse_errors = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], 1
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            parse_errors += 1
    return rows, parse_errors


def _latest_reviews(state_dir: Path) -> tuple[dict[str, dict[str, Any]], int]:
    rows, errs = _read_jsonl_rows(state_dir / "reviews.jsonl")
    latest: dict[str, dict[str, Any]] = {}
    for rec in rows:
        raw_key = rec.get("plan_key")
        sha = rec.get("plan_sha256")
        outcome = rec.get("outcome")
        if not (
            isinstance(raw_key, str)
            and raw_key.strip()
            and isinstance(sha, str)
            and sha
            and isinstance(outcome, str)
            and outcome
        ):
            continue
        key = _normalize_plan_key(raw_key, rec.get("repo_root"))
        ts_ns = rec.get("timestamp_unix_ns", 0)
        ns_val = ts_ns if isinstance(ts_ns, int) and not isinstance(ts_ns, bool) else 0
        prev = latest.get(key)
        prev_ns = prev.get("_ns", -1) if prev is not None else -1
        if prev is None or ns_val >= prev_ns:
            copied = dict(rec)
            copied["_key"] = key
            copied["_ns"] = ns_val
            latest[key] = copied
    return latest, errs


def _session_start_summary(state_dir: Path) -> str | None:
    latest_rev, rev_errs = _latest_reviews(state_dir)
    owing = [
        key
        for key, rec in sorted(latest_rev.items())
        if rec.get("outcome") == "owing_learn"
    ]
    prop_rows, prop_errs = _read_jsonl_rows(state_dir / "proposals.jsonl")
    latest_props: dict[str, dict[str, Any]] = {}
    for rec in prop_rows:
        pid = rec.get("proposal_id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        ts_ns = rec.get("timestamp_unix_ns", 0)
        ns_val = ts_ns if isinstance(ts_ns, int) and not isinstance(ts_ns, bool) else 0
        prev = latest_props.get(pid)
        prev_ns = prev.get("_ns", -1) if prev is not None else -1
        if prev is None or ns_val >= prev_ns:
            copied = dict(rec)
            copied["_ns"] = ns_val
            latest_props[pid] = copied

    now_ns = time.time_ns()
    active_count = 0
    escalated_ids: list[str] = []
    declined_expired: list[str] = []
    for pid, prop in sorted(latest_props.items()):
        status = prop.get("status")
        if status == "declined":
            until = prop.get("declined_until_unix_ns")
            if (
                isinstance(until, int)
                and not isinstance(until, bool)
                and now_ns >= until
            ):
                declined_expired.append(pid)
        elif status in ("pending", "approved"):
            active_count += 1
            raw_occ = prop.get("occurrences", 1)
            occ = (
                raw_occ
                if isinstance(raw_occ, int) and not isinstance(raw_occ, bool)
                else 1
            )
            if occ >= 2 and (
                prop.get("recommended_tier") in ("skill", "rule")
                or (
                    prop.get("recommended_tier")
                    in ("hk_pkl", "mise_doctor", "agy_hook")
                    and prop.get("tier") != prop.get("recommended_tier")
                )
            ):
                escalated_ids.append(pid)

    parse_errors = rev_errs + prop_errs
    parts: list[str] = []
    if owing:
        parts.append(f"{len(owing)} owing_learn review(s) ({', '.join(owing[:3])})")
    if active_count > 0:
        parts.append(f"{active_count} active proposal(s)")
    if escalated_ids:
        parts.append(f"auto-escalated: {', '.join(escalated_ids[:3])}")
    if declined_expired:
        parts.append(f"{len(declined_expired)} declined buffer expired")
    if parse_errors:
        parts.append(f"{parse_errors} corrupt JSONL line(s)")
    if not parts:
        return None
    return (
        "[agy-learn] Ledger health notice: "
        + "; ".join(parts)
        + ". Inspect via `mise run learn:heal --check --json` or `mise run learn:list`."
    )


def _read_mise_local_plan_id(root: Path) -> str | None:
    mise_local = root / "mise.local.toml"
    if not mise_local.is_file():
        return None
    try:
        lines = mise_local.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    in_env = False
    for raw in lines:
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("["):
            in_env = stripped == "[env]"
            continue
        if in_env:
            m = MISE_PLAN_ID_RE.match(stripped)
            if m and _valid_slug(m.group(1).strip()):
                return m.group(1).strip()
    return None


def _plans_for_root(root: Path) -> list[Path]:
    planning = root / ".planning"
    if not planning.is_dir():
        legacy = root / "task_plan.md"
        return [legacy] if legacy.is_file() else []
    pin = _read_mise_local_plan_id(root)
    if pin:
        pinned = planning / pin / "task_plan.md"
        return [pinned] if pinned.is_file() else []
    env_id = os.environ.get("PLAN_ID", "").strip()
    if _valid_slug(env_id):
        env_plan = planning / env_id / "task_plan.md"
        return [env_plan] if env_plan.is_file() else []
    active_file = planning / ".active_plan"
    if active_file.is_file():
        try:
            active_id = active_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            active_id = ""
        if _valid_slug(active_id):
            p = planning / active_id / "task_plan.md"
            return [p] if p.is_file() else []
    cands = [
        p
        for p in sorted(planning.glob("*/task_plan.md"))
        if p.parent.name != ".archive" and p.is_file()
    ]
    return cands if len(cands) == 1 else []


def _parse_pwf_plan(text: str) -> tuple[int, int, int, int, bool]:
    total = complete = in_progress = pending = 0
    in_phase = False
    has_status = True
    for raw in text.splitlines():
        if PHASE_HEADING_RE.match(raw):
            if in_phase and not has_status:
                return 0, 0, 0, 0, False
            total += 1
            in_phase = True
            has_status = False
            continue
        if in_phase:
            m = STATUS_RE.match(raw)
            if m:
                st = m.group(1).lower()
                has_status = True
                if st == "complete":
                    complete += 1
                elif st in ("in_progress", "active"):
                    in_progress += 1
                elif st in ("pending", "todo", "planned", "blocked"):
                    pending += 1
                else:
                    return 0, 0, 0, 0, False
                in_phase = False
    if in_phase and not has_status:
        return 0, 0, 0, 0, False
    valid = total > 0 and (complete + in_progress + pending) == total
    return total, complete, in_progress, pending, valid


def _count_plan_errors(text: str) -> int:
    in_section = False
    err_level = 0
    fence_char = ""
    fence_len = 0
    count = 0
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        fm = FENCE_RE.match(line)
        if fm:
            marker_run, rest = fm.group(1), fm.group(2)
            marker = marker_run[0]
            if not fence_char:
                if not (marker == "`" and "`" in rest):
                    fence_char = marker
                    fence_len = len(marker_run)
                    continue
            elif (
                marker == fence_char
                and len(marker_run) >= fence_len
                and not rest.strip(" \t")
            ):
                fence_char = ""
                fence_len = 0
                continue
        if fence_char:
            continue
        stripped = line.strip()
        em = ERROR_HEADING_RE.match(stripped)
        if em:
            in_section = True
            err_level = len(em.group(1))
            continue
        if in_section:
            hm = ANY_MD_HEADING_RE.match(stripped)
            if hm and len(hm.group(1)) <= err_level:
                break
        if not in_section or not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells or all(re.fullmatch(r":?-{1,}:?", c or "-") for c in cells):
            continue
        first = cells[0].lower().rstrip(".")
        if first in {"error", "issue", "errors"}:
            continue
        if first in ERROR_PLACEHOLDER_CELLS or all(
            c.lower().rstrip(".") in ERROR_PLACEHOLDER_CELLS for c in cells
        ):
            continue
        count += 1
    return count


def _append_owing_review(
    state_dir: Path, plan_path: Path, digest: str, err_count: int
) -> None:
    reviews_path = state_dir / "reviews.jsonl"
    lock_path = reviews_path.with_suffix(".jsonl.lock")
    ts, ns = _now_ns_pair()
    plan_key = str(plan_path.resolve(strict=False))
    label = (
        plan_path.parent.name
        if plan_path.parent.parent.name == ".planning"
        else str(plan_path)
    )
    repo_root: str | None = None
    cur = plan_path.parent
    while cur != cur.parent:
        if (cur / ".git").exists():
            repo_root = str(cur)
            break
        cur = cur.parent
    wt_slug = (
        plan_path.parent.name if plan_path.parent.parent.name == ".planning" else None
    )
    rec = {
        "review_id": "rev-"
        + hashlib.sha256(f"{plan_key}:{digest}:{ns}".encode()).hexdigest()[:12],
        "plan_key": plan_key,
        "plan_id": label,
        "worktree_slug": wt_slug,
        "plan_sha256": digest,
        "error_count": max(0, int(err_count)),
        "outcome": "owing_learn",
        "survivor_count": 0,
        "proposal_ids": [],
        "provenance": {
            "plan_error_ref": f"{plan_key}#Errors Encountered",
            "plan_path": plan_key,
        },
        "repo_root": repo_root,
        "notes": "Portable harness Stop retry exhausted without recorded review.",
        "timestamp": ts,
        "timestamp_unix_ns": ns,
    }
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    try:
        reviews_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                with reviews_path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _handle_stop(payload: dict[str, Any], harness: str) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_AUTO_LEARN") == "1":
        return {}
    cwd_raw = payload.get("cwd")
    if not isinstance(cwd_raw, str) or not cwd_raw.strip():
        return {}
    root = Path(os.path.expanduser(cwd_raw.strip()))
    if not root.is_dir():
        return {}

    plans = _plans_for_root(root)
    if not plans:
        return {}

    state_dir = _resolve_state_dir()
    latest_rev, _ = _latest_reviews(state_dir)

    unreviewed: list[tuple[Path, str, str, int]] = []
    for plan_path in plans:
        try:
            plan_bytes = plan_path.read_bytes()
        except OSError:
            continue
        text = plan_bytes.decode("utf-8", errors="replace")
        total, complete, in_progress, pending, valid = _parse_pwf_plan(text)
        if not valid or not (complete == total and in_progress == 0 and pending == 0):
            continue
        digest = hashlib.sha256(plan_bytes).hexdigest()
        plan_key = str(plan_path.resolve(strict=False))
        rec = latest_rev.get(plan_key)
        err_count = _count_plan_errors(text)
        if (
            rec is not None
            and rec.get("plan_sha256") == digest
            and rec.get("outcome") in VALID_CLEAR_OUTCOMES
        ):
            rec_err = rec.get("error_count", 0)
            rec_err_int = (
                rec_err
                if isinstance(rec_err, int) and not isinstance(rec_err, bool)
                else 0
            )
            if err_count <= rec_err_int:
                continue
        unreviewed.append((plan_path, plan_key, digest, err_count))

    if not unreviewed:
        return {}

    stop_active = bool(payload.get("stop_hook_active") is True)
    session_id = str(payload.get("session_id") or "default").strip() or "default"
    safe_sess = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:64]
    retry_file = state_dir / f"portable-stop-{harness}-{safe_sess}.json"
    retries: dict[str, int] = {}
    if retry_file.is_file():
        try:
            loaded = json.loads(retry_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                retries = {
                    str(k): int(v) for k, v in loaded.items() if isinstance(v, int)
                }
        except (OSError, ValueError, json.JSONDecodeError):
            retries = {}

    if stop_active:
        for plan_path, _plan_key, digest, err_count in unreviewed:
            if err_count > 0:
                _append_owing_review(state_dir, plan_path, digest, err_count)
        return {}

    first_unseen: tuple[Path, str, str, int] | None = None
    for item in unreviewed:
        sig = f"{item[1]}:{item[2]}"
        if retries.get(sig, 0) < 1:
            first_unseen = item
            retries[sig] = retries.get(sig, 0) + 1
    try:
        retry_file.parent.mkdir(parents=True, exist_ok=True)
        retry_file.write_text(json.dumps(retries) + "\n", encoding="utf-8")
    except OSError:
        pass

    if first_unseen is None:
        return {}

    labels = ", ".join(
        p.parent.name if p.parent.parent.name == ".planning" else str(p)
        for p, _, _, _ in unreviewed
    )
    return {
        "decision": "block",
        "reason": (
            f"Automated post-task /learn check ({harness}): completed PWF plan '{labels}' has no recorded review in reviews.jsonl. "
            "Run one bounded `/learn` review and record via `mise run learn:record-review --plan-key <plan_path> --plan-sha256 <sha256>` "
            "(`--outcome no_new_proposals --survivor-count 0` if 0 survivors) or `mise run learn:propose`."
        ),
    }


def _handle_session_start(_payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_AUTO_LEARN") == "1":
        return {}
    msg = _session_start_summary(_resolve_state_dir())
    if not msg:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": msg,
        }
    }


def _run_self_check() -> list[str]:
    import tempfile

    checks: list[str] = []
    saved = {
        k: os.environ.pop(k, None)
        for k in (
            "AGY_LEARN_STATE_DIR",
            "AGY_DISABLE_AUTO_LEARN",
            "PLAN_ID",
            "PWF_PLAN_ROOT",
        )
    }
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir()
            os.environ["AGY_LEARN_STATE_DIR"] = str(state_dir)

            assert _handle_session_start({}) == {}
            (state_dir / "reviews.jsonl").write_text(
                json.dumps(
                    {
                        "review_id": "rev-1",
                        "plan_key": "/tmp/demo/.planning/p1/task_plan.md",
                        "plan_sha256": "abc",
                        "outcome": "owing_learn",
                        "timestamp_unix_ns": 100,
                    }
                )
                + "\n{corrupt\n",
                encoding="utf-8",
            )
            ss = _handle_session_start({})
            ctx = ss["hookSpecificOutput"]["additionalContext"]
            assert ss["hookSpecificOutput"]["hookEventName"] == "SessionStart"
            assert "owing_learn" in ctx and "corrupt JSONL" in ctx
            checks.append("session_start_surfaces_owing_and_corrupt")

            repo = root / "repo"
            pdir = repo / ".planning" / "demo"
            pdir.mkdir(parents=True)
            plan = pdir / "task_plan.md"
            plan.write_text(
                "# Demo\n\n### Phase 1: Work\n**Status:** complete\n\n"
                "## Errors Encountered\n| Error | Resolution |\n|---|---|\n"
                "| Err1 | Fixed |\n",
                encoding="utf-8",
            )
            stop_payload = {
                "cwd": str(repo),
                "session_id": "s1",
                "stop_hook_active": False,
            }
            blk = _handle_stop(stop_payload, "codex")
            assert blk.get("decision") == "block" and "demo" in blk.get("reason", "")
            checks.append("stop_blocks_unreviewed_completed_plan")

            res_active = _handle_stop(
                {"cwd": str(repo), "session_id": "s1", "stop_hook_active": True},
                "codex",
            )
            assert res_active == {}
            latest_after, _ = _latest_reviews(state_dir)
            assert (
                latest_after[str(plan.resolve(strict=False))]["outcome"]
                == "owing_learn"
            )
            checks.append("stop_hook_active_marks_owing_no_loop")

            digest = hashlib.sha256(plan.read_bytes()).hexdigest()
            with (state_dir / "reviews.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "review_id": "rev-clear",
                            "plan_key": str(plan.resolve(strict=False)),
                            "plan_sha256": digest,
                            "outcome": "no_new_proposals",
                            "error_count": 1,
                            "timestamp_unix_ns": time.time_ns(),
                        }
                    )
                    + "\n"
                )
            assert (
                _handle_stop(
                    {"cwd": str(repo), "session_id": "s2", "stop_hook_active": False},
                    "claude",
                )
                == {}
            )
            checks.append("stop_clears_after_matching_review")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Portable /learn SessionStart & Stop hook adapter for Claude Code and Codex CLI."
    )
    parser.add_argument("--harness", choices=("claude", "codex"), default="claude")
    parser.add_argument("--event", choices=("session-start", "stop"), default="stop")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)

    if args.self_check:
        checks = _run_self_check()
        sys.stdout.write(json.dumps({"status": "ok", "checks": checks}) + "\n")
        return 0

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, ValueError, json.JSONDecodeError):
        payload = {}

    if args.event == "session-start":
        out = _handle_session_start(payload)
    else:
        out = _handle_stop(payload, args.harness)
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
