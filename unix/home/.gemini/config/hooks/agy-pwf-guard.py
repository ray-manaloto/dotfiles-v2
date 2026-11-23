#!/usr/bin/env python3
"""Antigravity PreToolUse guard requiring an unambiguous active PWF plan in PWF-adopted repos."""

from __future__ import annotations

import argparse
import json
import os
import re
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
DENY_REASON = (
    "PWF guard: this repository enforces planning-with-files (.mode present), "
    "but no unambiguous active PWF plan was resolved. Initialize and attest "
    ".planning/<PLAN_ID>/task_plan.md (and pin PLAN_ID in mise.local.toml or use "
    "'mise run worktree:new <slug>') before editing repository files."
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


def _resolve_active_plan(repo_root: Path) -> bool:
    pwf_root_raw = os.environ.get("PWF_PLAN_ROOT", "").strip()
    if pwf_root_raw:
        base = Path(os.path.expanduser(pwf_root_raw)).resolve(strict=False)
        if not base.is_dir():
            return False
    else:
        base = repo_root

    env_plan_id = os.environ.get("PLAN_ID", "").strip()
    if env_plan_id:
        return (
            _valid_slug(env_plan_id)
            and (base / ".planning" / env_plan_id / "task_plan.md").is_file()
        )

    has_local_pin, local_plan_id = _read_plan_id_from_mise_local(repo_root)
    if has_local_pin:
        return (
            _valid_slug(local_plan_id)
            and (base / ".planning" / local_plan_id / "task_plan.md").is_file()
        )

    active_file = base / ".planning" / ".active_plan"
    if active_file.is_file():
        try:
            active_id = active_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            return False
        if active_id:
            return (
                _valid_slug(active_id)
                and (base / ".planning" / active_id / "task_plan.md").is_file()
            )

    planning_dir = base / ".planning"
    if not planning_dir.is_dir():
        return False
    candidates = [
        p
        for p in sorted(planning_dir.glob("*/task_plan.md"))
        if p.parent.name != ".archive" and p.is_file()
    ]
    return len(candidates) == 1


def _run_hook(payload: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_PWF_GUARD") == "1":
        return _allow()

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return _allow()
    tool_name = tool_call.get("name")
    if not isinstance(tool_name, str) or tool_name not in EDIT_TOOLS:
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

    if _resolve_active_plan(repo_root):
        return _allow()
    return _deny()


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
    if not isinstance(matcher, str) or "edit_file" not in matcher:
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
                (pdir / "task_plan.md").write_text("# Plan\n", encoding="utf-8")
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
