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
    "'mise run worktree:new <slug>') before editing repository files."
)
COMPLETE_DENY_REASON = (
    "PWF guard: the active planning-with-files plan has all phases marked "
    "complete. Start a new PLAN_ID (e.g. 'mise run worktree:new <slug>' or a "
    "new plan in mise.local.toml) or mark the next phase 'in_progress' before "
    "editing tracked repository files."
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


def _valid_slug(slug: str) -> bool:
    return bool(slug and SAFE_SLUG_RE.match(slug) and slug not in (".", ".."))


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
        for candidate in (
            r / ".planning" / slug / "task_plan.md",
            r.parent / f"{r.name}-{slug}" / ".planning" / slug / "task_plan.md",
            r.parent
            / f"{r.name}-worktrees"
            / slug
            / ".planning"
            / slug
            / "task_plan.md",
        ):
            if candidate.is_file():
                try:
                    key = str(candidate.resolve())
                except OSError:
                    key = str(candidate)
                if key not in seen:
                    seen.add(key)
                    plans.append(candidate)
    return plans


def _check_worktree_rm(
    payload: dict[str, Any], base_dir: Path | None, command: str
) -> dict[str, Any]:
    slug = _extract_worktree_rm_slug(command)
    if not slug:
        return _allow()
    plans = _candidate_worktree_plans(base_dir, slug)
    if not plans:
        return _allow()
    artifact_raw = payload.get("artifactDirectoryPath")
    if not isinstance(artifact_raw, str) or not artifact_raw.strip():
        return _allow()
    artifact_dir = Path(os.path.expanduser(artifact_raw.strip()))
    state_path = artifact_dir / ".post-task-learn-state.json"
    learned_plans: dict[str, Any] = {}
    pending_key: str | None = None
    if state_path.is_file():
        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            raw_state = {}
        if isinstance(raw_state, dict):
            lp = raw_state.get("learned_plans")
            if isinstance(lp, dict):
                learned_plans = lp
            pend = raw_state.get("pending_proposal")
            if isinstance(pend, dict) and isinstance(pend.get("plan_key"), str):
                pending_key = pend["plan_key"]

    for plan_path in plans:
        try:
            plan_key = str(plan_path.resolve())
            digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        except OSError:
            continue
        if learned_plans.get(plan_key) != digest or pending_key == plan_key:
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

    plan_path = _resolve_active_plan(repo_root)
    if plan_path is None:
        return _deny()
    if _is_plan_all_complete(plan_path):
        return _deny(COMPLETE_DENY_REASON)
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
        for k in ("PLAN_ID", "PWF_PLAN_ROOT", "AGY_DISABLE_PWF_GUARD")
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

            # 6. Pinning PLAN_ID in mise.local.toml allows edits
            (pwf_repo / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "plan-a"\n', encoding="utf-8"
            )
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(pwf_repo / "AGENTS.md")},
                    }
                }
            )
            assert res == _allow(), f"mise.local.toml pin should allow: {res}"
            checks.append("mise_local_pin_allowed")

            # 7. Env PLAN_ID overrides and validates exact slug
            (pwf_repo / "mise.local.toml").unlink()
            os.environ["PLAN_ID"] = "plan-b"
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(pwf_repo / "AGENTS.md")},
                    }
                }
            )
            assert res == _allow(), f"env PLAN_ID should allow: {res}"
            os.environ["PLAN_ID"] = "missing-plan"
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(pwf_repo / "AGENTS.md")},
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
            (pwf_repo / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "plan-a"\n', encoding="utf-8"
            )
            res = _run_hook(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(pwf_repo / "AGENTS.md")},
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
