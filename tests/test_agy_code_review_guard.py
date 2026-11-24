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


def test_verification_timestamp_parsing_and_mixed_format_interop(
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    # Integer nanosecond parsing matches exact UTC epoch seconds without float64 drift
    assert (
        guard._parse_timestamp_ns("20261123T202111.123456789Z")
        == 1795465271_123_456_789
    )
    assert guard._parse_timestamp_ns("2026-11-23T20:21:11.000001+00:00") == (
        1795465271_000_001_000
    )
    assert guard._parse_timestamp_ns("20261123T202111.123456789Z\n") is None
    assert guard._parse_timestamp_ns("not-a-timestamp") is None

    common_dir = tmp_path / ".git"
    common_dir.mkdir()
    receipt_path = guard._receipt_path(common_dir)

    def write_entry(entry: dict[str, Any]) -> None:
        receipt_path.write_text(
            json.dumps({"latest": "c1", "receipts": {"c1": entry}}),
            encoding="utf-8",
        )

    # Mixed legacy ISO Stage 1 + 9-digit ns Stage 2 passes when Stage 2 >= Stage 1
    write_entry(
        {
            "commit_sha": "c1",
            "tree_sha": "t1",
            "recorded_at": "2026-11-23T20:21:11+00:00",
            "verification": {
                "verified_at": "20261123T202112.000000001+0000",
                "verified_at_unix_ns": 1795465272_000_000_001,
                "proof": "Verified post-review cleanly.",
                "commands": [{"command": "pytest", "exit_code": 0}],
            },
        }
    )
    assert guard._has_valid_verification_receipt(common_dir, "c1", "t1") is True

    # Backdated Stage 2 fails even across legacy ISO vs ns format
    write_entry(
        {
            "commit_sha": "c1",
            "tree_sha": "t1",
            "recorded_at": "2026-11-23T20:21:15+00:00",
            "verification": {
                "verified_at": "20261123T202112.000000001+0000",
                "verified_at_unix_ns": 1795465272_000_000_001,
                "proof": "Verified post-review cleanly.",
                "commands": [{"command": "pytest", "exit_code": 0}],
            },
        }
    )
    assert guard._has_valid_verification_receipt(common_dir, "c1", "t1") is False

    # Malformed verified_at without verified_at_unix_ns fails closed (no string fallback)
    write_entry(
        {
            "commit_sha": "c1",
            "tree_sha": "t1",
            "recorded_at": "20261123T202111.000000001+0000",
            "recorded_at_unix_ns": 1795465271_000_000_001,
            "verification": {
                "verified_at": "zzzz-not-a-date",
                "proof": "Verified post-review cleanly.",
                "commands": [{"command": "pytest", "exit_code": 0}],
            },
        }
    )
    assert guard._has_valid_verification_receipt(common_dir, "c1", "t1") is False


def test_shell_wrapped_gates_past_tense_stop_and_nonzero_verification_rejection(
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    root = tmp_path
    origin = root / "origin.git"
    repo = root / "repo"
    guard._git_exec(root, "init", "--bare", str(origin))
    guard._git_exec(root, "clone", str(origin), str(repo))
    guard._git_exec(repo, "config", "user.email", "test@example.com")
    guard._git_exec(repo, "config", "user.name", "Test User")
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    guard._git_exec(repo, "add", "app.py")
    guard._git_exec(repo, "commit", "-m", "init")
    guard._git_exec(repo, "branch", "-M", "main")
    guard._git_exec(repo, "push", "-u", "origin", "main")
    guard._git_exec(repo, "checkout", "-b", "feat/x")
    (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
    guard._git_exec(repo, "add", "app.py")
    guard._git_exec(repo, "commit", "-m", "feat")
    guard._git_exec(repo, "checkout", "main")

    # Shell-wrapped non-ff git merge into main & unpushed git push & gh pr merge (even after pushing @{u}) are gated cleanly
    res_merge = guard._run_hook(
        {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "zsh -lic 'git merge --no-ff feat/x'",
                    "Cwd": str(repo),
                },
            }
        },
        event="pre_tool_use",
    )
    assert res_merge.get("decision") == "deny", f"git merge must deny: {res_merge}"

    (repo / "app.py").write_text("print('v3')\n", encoding="utf-8")
    guard._git_exec(repo, "add", "app.py")
    guard._git_exec(repo, "commit", "-m", "unpushed main commit")
    res_push = guard._run_hook(
        {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "sh -c 'git push origin main'",
                    "Cwd": str(repo),
                },
            }
        },
        event="pre_tool_use",
    )
    assert res_push.get("decision") == "deny", f"git push must deny: {res_push}"

    guard._git_exec(repo, "checkout", "feat/x")
    guard._git_exec(repo, "push", "-u", "origin", "feat/x")
    res_pr = guard._run_hook(
        {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": 'bash -c "gh pr merge 42 --merge"',
                    "Cwd": str(repo),
                },
            }
        },
        event="pre_tool_use",
    )
    assert res_pr.get("decision") == "deny", f"gh pr merge must deny: {res_pr}"

    # Stop gate: past-tense "fixed the error" + failing verification exit 1 must NOT bypass Stop
    transcript = root / "t_hard.jsonl"
    art_dir = root / "art_hard"
    art_dir.mkdir()
    _write_transcript(
        transcript,
        [
            {"type": "USER_INPUT", "content": "Fix app.py"},
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 1,
                "tool_calls": [{"name": "edit_file", "args": {"Path": "app.py"}}],
                "content": "Updated app.py.",
            },
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 2,
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {"CommandLine": "pytest -q", "Cwd": str(repo)},
                    }
                ],
                "content": "Running pytest.",
            },
            {
                "type": "GENERIC",
                "step_index": 3,
                "content": "Command exited with code 1\n1 failed",
            },
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 4,
                "tool_calls": [],
                "content": "Done! Fixed the error with 0 errors remaining.",
            },
        ],
    )
    stop_res = guard._run_hook(
        {
            "terminationReason": "NO_TOOL_CALL",
            "fullyIdle": True,
            "transcriptPath": str(transcript),
            "artifactDirectoryPath": str(art_dir),
            "cwd": str(repo),
        },
        event="stop",
    )
    assert stop_res.get("decision") == "continue"


def test_hook_shell_unwrapping_tool_matching_and_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = _load_guard()
    hooks_dir = HOOK_PATH.parent

    def _load_mod(filename: str) -> ModuleType:
        spec = importlib.util.spec_from_file_location(
            filename[:-3], hooks_dir / filename
        )
        assert spec is not None and spec.loader is not None
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    # 1. POSIX shell -c edge cases in agy-code-review-guard & agy-hc-send-guard & agy-pwf-guard
    hc_guard = _load_mod("agy-hc-send-guard.py")
    pwf_guard = _load_mod("agy-pwf-guard.py")
    iq_guard = _load_mod("agy-interactive-questions.py")
    ptl_guard = _load_mod("post-task-learn.py")

    for cmd in (
        "zsh -c -e 'git push origin main'",
        "bash -lc -- 'git push origin main'",
        "sh +e --rcfile /dev/null -c 'git push origin main'",
    ):
        segs = guard._split_shell_segments(cmd)
        assert segs == [["git", "push", "origin", "main"]], (cmd, segs)

    hc_segs = hc_guard._split_shell_segments("zsh -c -e 'hc send tab1 payload'")
    assert hc_segs == [["hc", "send", "tab1", "payload"]]

    pwf_targets = pwf_guard._extract_worktree_targets(
        "(git -C /tmp/repo worktree remove --force /tmp/repo-wt); zsh -lc -- 'mise -C /tmp/repo run worktree:rm wt-slug'",
        tmp_path,
    )
    assert [slug for slug, _ in pwf_targets] == ["repo-wt", "wt-slug"]

    # 2. _tool_call_verified_exit_zero prioritizes "Command exited with code" over inner text and fails closed on missing result
    entries = [
        (
            0,
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 1,
                "tool_calls": [{"name": "read_file"}, {"name": "run_command"}],
            },
        ),
        (
            1,
            {
                "type": "GENERIC",
                "step_index": 2,
                "content": "read ok",
            },
        ),
        (
            2,
            {
                "type": "GENERIC",
                "step_index": 3,
                "content": "Command exited with code 0.\n\ntested failure message: exit code: 1",
            },
        ),
    ]
    assert guard._tool_call_verified_exit_zero(
        entries, 0, entries[0][1], 1, entries[0][1]["tool_calls"][1], {}
    )
    assert ptl_guard._tool_call_succeeded_in_slice(
        entries, 0, entries[0][1], 1, entries[0][1]["tool_calls"][1], {}
    )
    # Missing result for tc_idx >= len(results) without step match must fail closed (False)
    short_entries = [
        (
            0,
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 10,
                "tool_calls": [{"name": "read_file"}, {"name": "run_command"}],
            },
        ),
        (1, {"type": "GENERIC", "content": "only read result"}),
    ]
    assert not guard._tool_call_verified_exit_zero(
        short_entries,
        0,
        short_entries[0][1],
        1,
        short_entries[0][1]["tool_calls"][1],
        {},
    )
    assert not ptl_guard._tool_call_succeeded_in_slice(
        short_entries,
        0,
        short_entries[0][1],
        1,
        short_entries[0][1]["tool_calls"][1],
        {},
    )

    # 3. interactive-questions narrowed LEARN_EXEMPT_RE, AGY_LEARN_STATE_DIR, and relative plan_key resolution
    assert iq_guard.LEARN_EXEMPT_RE.search("Run learn:apply for proposal 2")
    assert not iq_guard.LEARN_EXEMPT_RE.search("Choose architecture proposal for API")
    repo_dir = tmp_path / "repo_rel"
    (repo_dir / ".git").mkdir(parents=True)
    plan_dir = repo_dir / ".planning" / "2026-11-24-rel"
    plan_dir.mkdir(parents=True)
    plan_file = plan_dir / "task_plan.md"
    plan_file.write_text("### Phase 1\n- **Status:** complete\n", encoding="utf-8")
    (repo_dir / "mise.local.toml").write_text(
        '[env]\nPLAN_ID = "2026-11-24-rel"\n', encoding="utf-8"
    )
    sha = __import__("hashlib").sha256(plan_file.read_bytes()).hexdigest()
    state_dir = tmp_path / "learn_state"
    state_dir.mkdir()
    monkeypatch.setenv("AGY_LEARN_STATE_DIR", str(state_dir))
    assert iq_guard._shared_reviews_path() == state_dir / "reviews.jsonl"
    (state_dir / "reviews.jsonl").write_text(
        json.dumps(
            {
                "plan_key": ".planning/2026-11-24-rel/task_plan.md",
                "repo_root": str(repo_dir),
                "plan_sha256": sha,
                "outcome": "no_new_proposals",
                "timestamp_unix_ns": 10,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert (
        iq_guard._check_learn_boundary_before_ask(
            {"cwd": str(repo_dir)},
            [{"question": "Which proposal?", "options": ["(Recommended) Opt 1"]}],
        )
        is None
    )
