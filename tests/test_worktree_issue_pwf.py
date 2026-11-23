"""Integration and unit tests for Issue #6: GitHub issue -> PWF PLAN_ID -> worktree mapping."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKTREE_NEW = REPO_ROOT / "tasks" / "worktree" / "new"
PWF_STATUS = (
    REPO_ROOT / "unix" / "home" / ".config" / "mise" / "tasks" / "pwf" / "status"
)
PWF_GUARD = (
    REPO_ROOT / "unix" / "home" / ".gemini" / "config" / "hooks" / "agy-pwf-guard.py"
)
HOOKS_JSON = REPO_ROOT / "unix" / "home" / ".gemini" / "config" / "hooks.json"
NS_TS_RE = re.compile(r"^\d{8}T\d{6}\.\d{9}[+-]\d{4}$")


def _load_guard_module() -> Any:
    spec = importlib.util.spec_from_file_location("agy_pwf_guard", PWF_GUARD)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _clean_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    try:
        local_vars = subprocess.check_output(
            ["git", "rev-parse", "--local-env-vars"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).splitlines()
    except OSError, subprocess.CalledProcessError:
        local_vars = [
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_CONFIG",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_COUNT",
            "GIT_OBJECT_DIRECTORY",
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_IMPLICIT_WORK_TREE",
            "GIT_GRAFT_FILE",
            "GIT_INDEX_FILE",
            "GIT_NO_REPLACE_OBJECTS",
            "GIT_PREFIX",
            "GIT_COMMON_DIR",
        ]
    for key in (
        *local_vars,
        "PLAN_ID",
        "PWF_PLAN_ROOT",
        "GITHUB_ISSUE",
        "GITHUB_ISSUE_URL",
        "AGY_DISABLE_PWF_GUARD",
        "MISE_ORIGINAL_CWD",
        "usage_slug",
        "usage_base",
        "usage_issue",
        "usage_dir",
        "usage_doctor",
        "usage_here",
    ):
        env.pop(key, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["HK"] = "0"
    if extra:
        env.update(extra)
    return env


def _git(repo: Path, *args: str, env: dict[str, str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _init_temp_git_repo(root: Path, env: dict[str, str]) -> Path:
    repo = root / "demo-repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main", env=env)
    _git(repo, "config", "user.name", "Test User", env=env)
    _git(repo, "config", "user.email", "test@example.invalid", env=env)
    (repo / ".mode").write_text("autonomous\n", encoding="utf-8")
    (repo / "ROADMAP.md").write_text("# Roadmap\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _git(repo, "add", ".mode", "ROADMAP.md", "docs/guide.md", "app.py", env=env)
    _git(repo, "commit", "-m", "init", env=env)
    return repo


def _write_exec(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class WorktreeIssuePwfTests(unittest.TestCase):
    def test_guard_self_check_and_nanosecond_receipt(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(PWF_GUARD),
                "--self-check",
                "--check-hooks-json",
                str(HOOKS_JSON),
            ],
            env=_clean_git_env(),
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        self.assertEqual(data.get("status"), "ok")
        self.assertRegex(data.get("verified_at", ""), NS_TS_RE)
        self.assertIn(
            "main_checkout_denied_docs_roadmap_bypass_allowed", data["checks"]
        )
        self.assertIn("linked_worktree_issue_mapping_enforced", data["checks"])
        self.assertIn("nanosecond_timestamp_format", data["checks"])

    def test_format_ns_timestamp_zero_pads(self) -> None:
        guard = _load_guard_module()
        ts = guard._format_ns_timestamp(1_700_000_000_000_000_005)
        self.assertRegex(ts, NS_TS_RE)
        self.assertIn(".000000005", ts)

    def test_real_git_worktree_enforcement_and_bypass(self) -> None:
        env = _clean_git_env()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _init_temp_git_repo(tmp_path, env)

            # Main checkout with issue-linked active plan still denies code edits,
            # while allowing narrow ROADMAP.md and docs/** bypass.
            main_plan = repo / ".planning" / "main-plan"
            main_plan.mkdir(parents=True)
            (main_plan / "task_plan.md").write_text(
                "# Task Plan: #6 Main\n\n### Phase 1: Work\n**Status:** in_progress\n",
                encoding="utf-8",
            )
            (repo / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "main-plan"\nGITHUB_ISSUE = "6"\n',
                encoding="utf-8",
            )

            def invoke_guard(target: Path) -> dict[str, Any]:
                payload = {
                    "cwd": str(target.parent),
                    "toolCall": {"name": "edit_file", "args": {"Path": str(target)}},
                }
                proc = subprocess.run(
                    [sys.executable, "-I", "-B", str(PWF_GUARD)],
                    input=json.dumps(payload),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return json.loads(proc.stdout)

            res_main_code = invoke_guard(repo / "app.py")
            self.assertEqual(res_main_code.get("decision"), "deny")
            self.assertEqual(invoke_guard(repo / "ROADMAP.md"), {"decision": "allow"})
            self.assertEqual(
                invoke_guard(repo / "docs" / "guide.md"), {"decision": "allow"}
            )

            # Create real linked git worktree
            wt = tmp_path / "demo-repo-issue-7"
            _git(repo, "worktree", "add", "-b", "wt/issue-7", str(wt), "HEAD", env=env)
            wt_plan = wt / ".planning" / "2026-11-23-issue-7"
            wt_plan.mkdir(parents=True)
            (wt_plan / "task_plan.md").write_text(
                "# Task Plan: Unlinked\n\n## Goal\nDo things\n\n### Phase 1: Work\n**Status:** in_progress\n",
                encoding="utf-8",
            )
            (wt / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "2026-11-23-issue-7"\n',
                encoding="utf-8",
            )
            self.assertEqual(invoke_guard(wt / "app.py").get("decision"), "deny")

            # Link via ## Goal URL in task_plan.md
            (wt_plan / "task_plan.md").write_text(
                "# Task Plan: Feature\n\n## Goal\nImplement https://github.com/org/repo/issues/7\n\n### Phase 1: Work\n**Status:** in_progress\n",
                encoding="utf-8",
            )
            self.assertEqual(invoke_guard(wt / "app.py"), {"decision": "allow"})

            # Invalid GITHUB_ISSUE in mise.local.toml fails closed
            (wt / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "2026-11-23-issue-7"\nGITHUB_ISSUE = "not-a-num"\n',
                encoding="utf-8",
            )
            self.assertEqual(invoke_guard(wt / "app.py").get("decision"), "deny")

            # Matching GITHUB_ISSUE allows
            (wt / "mise.local.toml").write_text(
                '[env]\nPLAN_ID = "2026-11-23-issue-7"\nGITHUB_ISSUE = "7"\n',
                encoding="utf-8",
            )
            self.assertEqual(invoke_guard(wt / "app.py"), {"decision": "allow"})

    def test_worktree_new_and_pwf_status_end_to_end(self) -> None:
        base_env = _clean_git_env()
        rel_manifest = (
            Path.home()
            / ".local"
            / "share"
            / "agy-pwf"
            / "current"
            / ".agy-pwf-release.json"
        )
        if not rel_manifest.is_file():
            self.skipTest(f"missing agy-pwf release manifest at {rel_manifest}")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = _init_temp_git_repo(tmp_path, base_env)

            # Build isolated bin/ shims for gh, mise, repotools so host state is untouched
            shim_bin = tmp_path / "bin"
            shim_bin.mkdir()
            _write_exec(
                shim_bin / "gh",
                r"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "issue" && "${2:-}" == "view" ]]; then
  num="${3:-}"
  if [[ "${num}" == "42" ]]; then
    printf '%s\n' '{"number":42,"title":"Fix \"quotes\" & slashes/test","url":"https://github.com/example/demo-repo/issues/42"}'
    exit 0
  fi
fi
echo "issue not found" >&2
exit 1
""",
            )
            _write_exec(shim_bin / "mise", "#!/usr/bin/env bash\nexit 0\n")
            _write_exec(shim_bin / "repotools", "#!/usr/bin/env bash\nexit 0\n")

            test_env = _clean_git_env(
                {"PATH": f"{shim_bin}:{base_env.get('PATH', os.defpath)}"}
            )

            # 1. Invalid --issue fails closed before creating worktree or branch
            bad_proc = subprocess.run(
                ["bash", str(WORKTREE_NEW)],
                cwd=repo,
                env={**test_env, "usage_slug": "bad-issue", "usage_issue": "0"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(bad_proc.returncode, 0)
            self.assertFalse((tmp_path / "demo-repo-bad-issue").exists())

            # 2. Missing gh issue fails closed before creating worktree
            miss_proc = subprocess.run(
                ["bash", str(WORKTREE_NEW)],
                cwd=repo,
                env={**test_env, "usage_slug": "missing-issue", "usage_issue": "999"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(miss_proc.returncode, 0)
            self.assertFalse((tmp_path / "demo-repo-missing-issue").exists())

            # 3. Valid --issue 42 creates worktree, rewrites + re-attests task_plan.md, pins mise.local.toml
            ok_proc = subprocess.run(
                ["bash", str(WORKTREE_NEW)],
                cwd=repo,
                env={**test_env, "usage_slug": "feat-42", "usage_issue": "42"},
                capture_output=True,
                text=True,
                check=True,
            )
            wt_42 = tmp_path / "demo-repo-feat-42"
            self.assertTrue(wt_42.is_dir())
            self.assertIn("issue=#42", ok_proc.stdout)
            m_ts = re.search(r"verified_at=(\S+)\)", ok_proc.stdout)
            self.assertIsNotNone(m_ts)
            assert m_ts is not None
            self.assertRegex(m_ts.group(1), NS_TS_RE)

            plan_id_42 = (
                (wt_42 / ".planning" / ".active_plan")
                .read_text(encoding="utf-8")
                .strip()
            )
            plan_dir_42 = wt_42 / ".planning" / plan_id_42
            plan_text_42 = (plan_dir_42 / "task_plan.md").read_text(encoding="utf-8")
            self.assertTrue(
                plan_text_42.splitlines()[0]
                == '# Task Plan: #42 Fix "quotes" & slashes/test'
            )
            self.assertIn(
                "## Goal\nImplement https://github.com/example/demo-repo/issues/42\n",
                plan_text_42,
            )
            stored_sha = (
                (plan_dir_42 / ".attestation").read_text(encoding="utf-8").strip()
            )
            actual_sha = hashlib.sha256(
                (plan_dir_42 / "task_plan.md").read_bytes()
            ).hexdigest()
            self.assertEqual(stored_sha, actual_sha)

            local_toml = (wt_42 / "mise.local.toml").read_text(encoding="utf-8")
            self.assertIn(f'PLAN_ID = "{plan_id_42}"', local_toml)
            self.assertIn('GITHUB_ISSUE = "42"', local_toml)
            self.assertIn(
                'GITHUB_ISSUE_URL = "https://github.com/example/demo-repo/issues/42"',
                local_toml,
            )

            ev_data = json.loads(
                (plan_dir_42 / "worktree-evidence.json").read_text(encoding="utf-8")
            )
            self.assertRegex(ev_data["verified_at"], NS_TS_RE)
            self.assertEqual(ev_data["github_issue"], 42)

            # 4. Zero-arg backwards compatibility (no --issue)
            plain_proc = subprocess.run(
                ["bash", str(WORKTREE_NEW)],
                cwd=repo,
                env={**test_env, "usage_slug": "plain-wt"},
                capture_output=True,
                text=True,
                check=True,
            )
            wt_plain = tmp_path / "demo-repo-plain-wt"
            self.assertTrue(wt_plain.is_dir())
            plain_toml = (wt_plain / "mise.local.toml").read_text(encoding="utf-8")
            self.assertNotIn("GITHUB_ISSUE", plain_toml)
            self.assertIn("verified_at=", plain_proc.stdout)

            # Give plain-wt a task_plan-based issue #99 to test pwf:status fallback parsing
            plain_pid = (
                (wt_plain / ".planning" / ".active_plan")
                .read_text(encoding="utf-8")
                .strip()
            )
            plain_plan_file = wt_plain / ".planning" / plain_pid / "task_plan.md"
            plain_plan_file.write_text(
                "# Task Plan: #99 Fallback Link\n\n## Goal\nImplement https://github.com/example/demo-repo/issues/99\n\n### Phase 1: Do\n**Status:** in_progress\n",
                encoding="utf-8",
            )

            # 5. pwf:status displays linked GitHub issues and URLs alongside PLAN_ID and progress
            status_proc = subprocess.run(
                ["bash", str(PWF_STATUS)],
                cwd=wt_42,
                env={
                    **test_env,
                    "usage_dir": str(wt_42),
                    "MISE_ORIGINAL_CWD": str(wt_42),
                },
                capture_output=True,
                text=True,
                check=True,
            )
            out = status_proc.stdout
            self.assertIn(
                "GitHub Issue: #42 (https://github.com/example/demo-repo/issues/42)",
                out,
            )
            self.assertIn(plan_id_42, out)
            self.assertIn("#42", out)
            self.assertIn("https://github.com/example/demo-repo/issues/42", out)
            self.assertIn("#99", out)
            self.assertIn("https://github.com/example/demo-repo/issues/99", out)
            self.assertIn("(unlinked)", out)

            # Verify fallback TOML parser handles `#` inside quotes and Python 3.9 compat
            guard_mod = _load_guard_module()
            modules_dict = cast(dict[str, Any], sys.modules)
            orig_tomllib = modules_dict.get("tomllib", ...)
            try:
                modules_dict["tomllib"] = None
                fallback_repo = tmp_path / "fallback-repo"
                fallback_repo.mkdir()
                (fallback_repo / "mise.local.toml").write_text(
                    'GITHUB_ISSUE = "#42" # inline comment\n'
                    'GITHUB_ISSUE_URL = "https://github.com/example/demo-repo/issues/42" # cmt\n',
                    encoding="utf-8",
                )
                has_issue, num_str = guard_mod._read_issue_from_mise_local(
                    fallback_repo
                )
                self.assertTrue(has_issue)
                self.assertEqual(num_str, "42")
            finally:
                if orig_tomllib is ...:
                    modules_dict.pop("tomllib", None)
                else:
                    modules_dict["tomllib"] = orig_tomllib

            # Verify _format_ns_timestamp outputs 9-digit ns format
            self.assertRegex(guard_mod._format_ns_timestamp(), NS_TS_RE)

            # Verify in_goal resets on subsequent `# ` headings without corrupting goal extraction
            goal_plan = tmp_path / "goal_reset.md"
            goal_plan.write_text(
                "# Task Plan: #55 Title\n\n## Goal\nDo x\n# Unrelated H1\n"
                "https://github.com/example/demo-repo/issues/88\n",
                encoding="utf-8",
            )
            has_p_issue, p_num = guard_mod._extract_task_plan_issue(goal_plan)
            self.assertTrue(has_p_issue)
            self.assertEqual(p_num, "55")


if __name__ == "__main__":
    unittest.main()
