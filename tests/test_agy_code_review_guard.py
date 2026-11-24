"""Git-integration & Stop-hook tests for `agy-code-review-guard.py` (moved out of fast doctor `--self-check`)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

NS_TS_RE = re.compile(r"^[0-9]{8}T[0-9]{6}\.[0-9]{9}(?:Z|[+-][0-9]{4})$")

HOOK_PATH = (
    Path(__file__).resolve().parent.parent
    / "unix"
    / "home"
    / ".gemini"
    / "config"
    / "hooks"
    / "agy-code-review-guard.py"
)


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agy_code_review_guard", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_transcript(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(rec) + "\n" for rec in records), encoding="utf-8"
    )


def test_git_exec_sanitizes_hook_local_env_vars_and_avoids_core_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _load_guard()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main_repo = root / "main_repo"
        guard._git_exec(root, "init", "--initial-branch=main", str(main_repo))
        guard._git_exec(main_repo, "config", "user.name", "Test User")
        guard._git_exec(main_repo, "config", "user.email", "test@example.com")
        (main_repo / "README.md").write_text("# Demo\n", encoding="utf-8")
        guard._git_exec(main_repo, "add", "README.md")
        guard._git_exec(main_repo, "commit", "-m", "init")

        # Simulate running inside a linked-worktree git hook where GIT_DIR/GIT_WORK_TREE are exported.
        poisoned_repo = root / "poisoned_target"
        monkeypatch.setenv("GIT_DIR", str(main_repo / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(main_repo))
        guard._git_exec(root, "init", "--initial-branch=main", str(poisoned_repo))

        cfg = subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(main_repo / ".git" / "config"),
                "--get-all",
                "core.worktree",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert cfg.stdout.strip() == ""
        assert (poisoned_repo / ".git").exists()


def test_full_code_review_and_post_review_verification_gate() -> None:
    guard = _load_guard()
    py_run = f"{shlex.quote(sys.executable)} -I -B app.py"

    def call_cmd(cmd: str, cwd: Path) -> dict[str, Any]:
        return guard._run_hook(
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": cmd, "Cwd": str(cwd)},
                }
            },
            event="pre-tool-use",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        non_git = root / "non_git"
        non_git.mkdir()
        assert call_cmd("git push origin main", non_git) == guard._allow()

        remote = root / "origin.git"
        guard._git_exec(root, "init", "--bare", "--initial-branch=main", str(remote))
        repo = root / "repo"
        guard._git_exec(root, "clone", str(remote), str(repo))
        guard._git_exec(repo, "config", "user.name", "Test User")
        guard._git_exec(repo, "config", "user.email", "test@example.com")

        (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
        guard._git_exec(repo, "add", "README.md")
        guard._git_exec(repo, "commit", "-m", "init")
        guard._git_exec(repo, "push", "-u", "origin", "main")

        (repo / "NOTES.md").write_text("docs only\n", encoding="utf-8")
        guard._git_exec(repo, "add", "NOTES.md")
        guard._git_exec(repo, "commit", "-m", "docs")
        assert call_cmd("git push origin main", repo) == guard._allow()
        guard._git_exec(repo, "push", "origin", "main")

        assert call_cmd("git push --dry-run origin main", repo) == guard._allow()
        assert call_cmd("git push origin :stale-branch", repo) == guard._allow()

        wt = root / "repo-wt"
        guard._git_exec(repo, "worktree", "add", "-b", "wt/feat", str(wt), "main")
        (wt / "app.py").write_text("print(42)\n", encoding="utf-8")
        guard._git_exec(wt, "add", "app.py")
        guard._git_exec(wt, "commit", "-m", "add app")

        assert call_cmd("git merge --ff-only wt/feat", repo).get("decision") == "deny"
        assert (
            call_cmd(f"cd {wt} && git push origin HEAD", repo).get("decision") == "deny"
        )
        assert (
            call_cmd(f"cd {wt}\ngit push origin HEAD", repo).get("decision") == "deny"
        )
        assert (
            call_cmd(f"(cd {wt} && env -u GIT_DIR git push origin HEAD)", repo).get(
                "decision"
            )
            == "deny"
        )
        assert call_cmd("git push origin wt/feat", repo).get("decision") == "deny"
        assert (
            call_cmd("repotools pr-create --title t --body-file b", wt).get("decision")
            == "deny"
        )
        assert call_cmd("gh pr create --fill", wt).get("decision") == "deny"
        assert call_cmd("mise run ship", wt).get("decision") == "deny"
        assert (
            call_cmd(f"mise --cd={wt} -E dev run ship", repo).get("decision") == "deny"
        )

        with pytest.raises(RuntimeError):
            guard._verify_receipt(
                wt,
                "HEAD",
                runs=["true"],
                evidence="",
                proof="Ran app.py cleanly before review",
            )

        doc_wt = root / "repo-doc-wt"
        guard._git_exec(repo, "worktree", "add", "-b", "wt/docs", str(doc_wt), "main")
        (repo / "main_code.py").write_text("x = 1\n", encoding="utf-8")
        guard._git_exec(repo, "add", "main_code.py")
        guard._git_exec(repo, "commit", "-m", "main code")
        guard._git_exec(repo, "push", "origin", "main")
        (doc_wt / "GUIDE.md").write_text("# Guide\n", encoding="utf-8")
        guard._git_exec(doc_wt, "add", "GUIDE.md")
        guard._git_exec(doc_wt, "commit", "-m", "docs")
        assert call_cmd("git push origin wt/docs", doc_wt) == guard._allow()

        guard._git_exec(repo, "reset", "--hard", "HEAD~1")
        guard._record_receipt(wt, "HEAD", "self-check review")
        deny_no_verify = call_cmd("git merge --ff-only wt/feat", repo)
        assert deny_no_verify.get("decision") == "deny"
        assert "--verify" in str(deny_no_verify.get("reason", ""))
        assert call_cmd("git push origin wt/feat", wt).get("decision") == "deny"

        with pytest.raises(RuntimeError):
            guard._verify_receipt(
                wt,
                "HEAD",
                runs=["exit 1"],
                evidence="",
                proof="Command failed deliberately",
            )
        assert call_cmd("git push origin wt/feat", wt).get("decision") == "deny"

        (wt / "app.py").write_text("print(99)\n", encoding="utf-8")
        with pytest.raises(RuntimeError):
            guard._verify_receipt(
                wt,
                "HEAD",
                runs=["true"],
                evidence="",
                proof="Should fail on dirty tracked file",
            )
        guard._git_exec(wt, "checkout", "--", "app.py")
        with pytest.raises(RuntimeError):
            guard._verify_receipt(
                wt,
                "HEAD",
                runs=["echo '# dirty' >> app.py"],
                evidence="",
                proof="Should fail when verification dirties tracked file",
            )
        guard._git_exec(wt, "checkout", "--", "app.py")

        ver_res = guard._verify_receipt(
            wt,
            "HEAD",
            runs=[py_run],
            evidence="",
            proof="Observed app.py output 42 exit 0",
        )
        assert ver_res.get("verified") is True
        assert NS_TS_RE.fullmatch(str(ver_res.get("verified_at", "")))
        assert isinstance(ver_res.get("verified_at_unix_ns"), int)
        receipt_file = Path(str(ver_res["receipt_file"]))
        receipt_data = json.loads(receipt_file.read_text(encoding="utf-8"))
        latest_entry = receipt_data["receipts"][receipt_data["latest"]]
        assert NS_TS_RE.fullmatch(latest_entry["recorded_at"])
        assert NS_TS_RE.fullmatch(latest_entry["verification"]["verified_at"])
        assert NS_TS_RE.fullmatch(latest_entry["verification"]["commands"][0]["ran_at"])
        assert guard._format_ns_timestamp(1_700_000_000_000_000_007, utc=True).endswith(
            ".000000007+0000"
        )
        assert call_cmd("git merge --ff-only wt/feat", repo) == guard._allow()
        guard._git_exec(repo, "merge", "--ff-only", "wt/feat")
        assert call_cmd("git push origin main", repo) == guard._allow()

        guard._record_receipt(repo, "HEAD", "re-review clears verify")
        assert call_cmd("git push origin main", repo).get("decision") == "deny"

        guard._verify_receipt(
            repo,
            "HEAD",
            runs=["true"],
            evidence="",
            proof="Re-verified app.py output 42",
        )
        assert call_cmd("git push origin main", repo) == guard._allow()
        assert (
            call_cmd("git commit -am msg && git push origin main", repo).get("decision")
            == "deny"
        )
        (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        guard._git_exec(repo, "add", "app.py")
        guard._git_exec(repo, "commit", "-m", "update app")
        assert call_cmd("git push origin main", repo).get("decision") == "deny"

        transcript = root / "transcript.jsonl"
        art_dir = root / "artifacts"
        art_dir.mkdir()

        _write_transcript(
            transcript,
            [
                {"type": "USER_INPUT", "content": "Fix app.py"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 10,
                    "tool_calls": [
                        {
                            "name": "edit_file",
                            "args": {"Path": str(repo / "app.py")},
                        }
                    ],
                    "content": "Editing app.py now.",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 11,
                    "tool_calls": [],
                    "content": "Done! I fixed app.py cleanly.",
                },
            ],
        )
        stop_payload = {
            "terminationReason": "NO_TOOL_CALL",
            "fullyIdle": True,
            "transcriptPath": str(transcript),
            "artifactDirectoryPath": str(art_dir),
        }
        stop_block = guard._run_hook(stop_payload, event="stop")
        assert stop_block.get("decision") == "continue"
        assert guard._run_hook(stop_payload, event="stop") == {}

        guard._record_receipt(repo, "HEAD", "reviewed stop-check commit")
        guard._verify_receipt(
            repo,
            "HEAD",
            runs=["true"],
            evidence="",
            proof="Observed python3 app.py output v2 exit 0",
        )
        full_transcript = transcript.with_name("transcript_full.jsonl")
        full_records_b = [
            {"type": "USER_INPUT", "content": "Fix app.py v2"},
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 20,
                "tool_calls": [{"name": "edit_file", "args": {"Path": "app.py"}}],
                "content": "Updated app.py.",
            },
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 21,
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {"CommandLine": "python3 app.py", "Cwd": "."},
                    }
                ],
                "content": "Ran verification.",
            },
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 22,
                "tool_calls": [],
                "content": "Done! Fixed app.py and verified with `python3 app.py` (passed, exit code 0, output v2).",
            },
        ]
        _write_transcript(full_transcript, full_records_b)
        compact_records_b = [
            full_records_b[0],
            {
                **full_records_b[1],
                "tool_calls": [{"name": "edit_file", "args": {"Path": '"app.py"'}}],
                "truncated_fields": ["tool_calls"],
            },
            full_records_b[2],
            full_records_b[3],
        ]
        _write_transcript(transcript, compact_records_b)
        stop_payload_with_cwd = {**stop_payload, "cwd": str(repo)}
        assert guard._run_hook(stop_payload_with_cwd, event="stop") == {}

        _write_transcript(
            transcript,
            [
                {"type": "USER_INPUT", "content": "Edit again"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 25,
                    "tool_calls": [{"name": "edit_file", "args": {"Path": "app.py"}}],
                    "content": "Edited.",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 26,
                    "tool_calls": [
                        {
                            "name": "run_command",
                            "args": {
                                "CommandLine": "ls -la && python3 -I -B ~/.gemini/config/hooks/agy-code-review-guard.py --record --summary x",
                                "Cwd": str(repo),
                            },
                        }
                    ],
                    "content": "Listed files.",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 27,
                    "tool_calls": [],
                    "content": "Done! Fixed on 11/23 cleanly.",
                },
            ],
        )
        assert (
            guard._run_hook(stop_payload_with_cwd, event="stop").get("decision")
            == "continue"
        )
        _write_transcript(
            transcript,
            [
                {"type": "USER_INPUT", "content": "Report blocker"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 28,
                    "tool_calls": [{"name": "edit_file", "args": {"Path": "app.py"}}],
                    "content": "Edited.",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 29,
                    "tool_calls": [],
                    "content": "I implemented the draft in app.py, but pytest failed and I am blocked.",
                },
            ],
        )
        assert guard._run_hook(stop_payload_with_cwd, event="stop") == {}

        full_transcript.unlink(missing_ok=True)
        _write_transcript(
            transcript,
            [
                {"type": "USER_INPUT", "content": "Learn check"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 30,
                    "tool_calls": [],
                    "content": "Post-task /learn: no reusable customization updates needed.",
                },
            ],
        )
        assert guard._run_hook(stop_payload, event="stop") == {}
        assert os.path.isfile(HOOK_PATH)


def test_agy_code_review_guard_macos_python3_9_compatible() -> None:
    mac_py = Path("/usr/bin/python3")
    if not mac_py.is_file():
        pytest.skip("/usr/bin/python3 not present")
    hooks_json = (
        Path(__file__).resolve().parent.parent
        / "unix"
        / "home"
        / ".gemini"
        / "config"
        / "hooks.json"
    )
    proc = subprocess.run(
        [
            str(mac_py),
            "-I",
            "-B",
            str(HOOK_PATH),
            "--self-check",
            "--check-hooks-json",
            str(hooks_json),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
