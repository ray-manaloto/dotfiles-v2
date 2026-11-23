"""Contract and fixture tests for all 6 `herdr_roll` harness adapters."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdr_roll.adapters import (
    ADAPTERS,
    AdapterSkipError,
    AgyAdapter,
    ClaudeAdapter,
    CodexAdapter,
    HermesAdapter,
    OpenCodeAdapter,
    ZCodeAdapter,
    detect_kind,
)
from herdr_roll.models import HarnessKind, TerminalSnapshot


def _term(
    kind: HarnessKind,
    *,
    address: str = "t1",
    fg_argv: tuple[str, ...] = (),
    term_argv: tuple[str, ...] = (),
    cwd: str = "/workspace/repo",
    session_id: str | None = "id-1",
    title: str = "",
) -> TerminalSnapshot:
    return TerminalSnapshot(
        address=address,
        kind=kind,
        session_id=session_id,
        cwd=cwd,
        fg_argv=fg_argv,
        term_argv=term_argv,
        pid=100,
        fg_pid=200,
        status_state="idle",
        title=title,
    )


class AdapterTests(unittest.TestCase):
    def test_agy_detection_resume_and_unknown_flag(self) -> None:
        fg = [
            "fnox",
            "--non-interactive",
            "proxy",
            "run",
            "--",
            "/bin/agy",
            "--dangerously-skip-permissions",
            "--conversation",
            "e4b6c8ee-177f-4fc6-8725-861036790dcf",
            "--model",
            "gemini-pro",
            "-i",
            "Initial prompt",
        ]
        self.assertEqual(detect_kind("antigravity", fg, []), HarnessKind.agy)
        adapter = AgyAdapter()
        sid = adapter.session_id(
            address="t3xb",
            tree_pids=[200],
            fg_argv=fg,
            term_argv=[],
            cwd="/workspace/repo",
            title="",
            screen_text="",
        )
        self.assertEqual(sid, "e4b6c8ee-177f-4fc6-8725-861036790dcf")
        assert sid is not None
        snap = _term(HarnessKind.agy, fg_argv=tuple(fg), session_id=sid)
        cmd = adapter.resume_argv(
            snap, session_id=sid, is_coordinator=False, prompt=None, screen_text=""
        )
        self.assertEqual(
            cmd,
            [
                "agy-hc",
                "--conversation",
                sid,
                "--dangerously-skip-permissions",
                "--model",
                "gemini-pro",
            ],
        )

        bad_snap = _term(
            HarnessKind.agy,
            fg_argv=("agy", "--conversation", sid, "--mystery-flag"),
            session_id=sid,
        )
        with self.assertRaises(AdapterSkipError) as ctx:
            adapter.resume_argv(
                bad_snap,
                session_id=sid,
                is_coordinator=False,
                prompt=None,
                screen_text="",
            )
        self.assertEqual(ctx.exception.code, "skipped-unknown-flag")

    def test_codex_deduplicates_wrapper_flags_and_prefers_live_rollout_root(
        self,
    ) -> None:
        # Simulates cos-shadow (t3fs) with 3x duplicated shell_environment_policy.* flags.
        policy = 'shell_environment_policy.include_only=["PATH","HC_*"]'
        fg = (
            "/opt/fnox",
            "proxy",
            "run",
            "--",
            "env",
            "-u",
            "RUST_LOG",
            "codex",
            "--disable",
            "daemon_auto_start",
            "-c",
            'shell_environment_policy.inherit="all"',
            "-c",
            policy,
            "resume",
            "01a1f5ba-9341-7890-976e-75cc768f8dd5",
            "-c",
            'shell_environment_policy.inherit="all"',
            "-c",
            policy,
            "-c",
            'shell_environment_policy.inherit="all"',
            "-c",
            policy,
            "--model",
            "gpt-6.2-sol",
            "-c",
            'model_reasoning_effort="low"',
            "Old prompt",
        )
        adapter = CodexAdapter()
        screen = (
            "› Ask Codex to do anything\n  GPT-6.2-Astra medium · /workspace/repo\n"
        )
        snap = _term(
            HarnessKind.codex,
            fg_argv=fg,
            session_id="01a20000-0000-7000-8000-000000000099",
        )
        cmd = adapter.resume_argv(
            snap,
            session_id=snap.session_id or "",
            is_coordinator=False,
            prompt=None,
            screen_text=screen,
        )
        self.assertEqual(
            cmd,
            [
                "codex-hc",
                "resume",
                "01a20000-0000-7000-8000-000000000099",
                "--model",
                "gpt-6.2-astra",
                "-c",
                'model_reasoning_effort="medium"',
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            rollout = (
                Path(tmp)
                / "rollout-2026-11-23-01a1f5ba-9341-7890-976e-75cc768f8ddd.jsonl"
            )
            rollout.write_text(
                json.dumps(
                    {"payload": {"session_id": "01a1f5ba-9341-7890-976e-75cc768f8aa0"}}
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "herdr_roll.adapters.run_cmd",
                return_value=f"codex 123 txt REG {rollout}\n",
            ):
                found = adapter.session_id(
                    address="t3vv",
                    tree_pids=[123],
                    fg_argv=list(fg),
                    term_argv=[],
                    cwd="/workspace/repo",
                    title="",
                    screen_text="",
                )
        self.assertEqual(found, "01a1f5ba-9341-7890-976e-75cc768f8aa0")

    def test_codex_fail_closed_on_hooks_review_or_trust_dialog(self) -> None:
        adapter = CodexAdapter()
        self.assertIsNotNone(
            adapter.blocking_dialog(
                "Hooks need review\n› 1. Review hooks\n3. Continue without trusting",
                "idle",
            )
        )
        self.assertIsNotNone(
            adapter.blocking_dialog("Do you trust the contents of this folder?", "idle")
        )
        self.assertIsNone(
            adapter.blocking_dialog("› Ask Codex to do anything\n· Ready ·", "idle")
        )

    def test_claude_trailing_json_record_flags_and_in_place_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sess_dir = home / ".claude/sessions"
            sess_dir.mkdir(parents=True)
            (sess_dir / "4242.json").write_text(
                '{"sessionId":"cl-123","status":"idle","bridgeSessionId":null,"nameSource":"auto","startedAt":1700000}trailing',
                encoding="utf-8",
            )
            with mock.patch("pathlib.Path.home", return_value=home):
                adapter = ClaudeAdapter()
                sid = adapter.session_id(
                    address="t1",
                    tree_pids=[4242],
                    fg_argv=["claude"],
                    term_argv=[],
                    cwd="/workspace/repo",
                    title="",
                    screen_text="",
                )
                self.assertEqual(sid, "cl-123")
                self.assertTrue(
                    adapter.can_in_place_restart(
                        4242, [4242], force_clean_env=False, fresh_tab=False
                    )
                )
                self.assertFalse(
                    adapter.can_in_place_restart(
                        4242, [4242], force_clean_env=True, fresh_tab=False
                    )
                )

                snap = _term(
                    HarnessKind.claude,
                    fg_argv=(
                        "claude",
                        "--permission-mode",
                        "auto",
                        "--resume",
                        "old",
                        "-p",
                        "hi",
                    ),
                    session_id="cl-123",
                )
                cmd = adapter.resume_argv(
                    snap,
                    session_id="cl-123",
                    is_coordinator=False,
                    prompt=None,
                    screen_text="",
                )
                self.assertEqual(
                    cmd, ["claude", "--permission-mode", "auto", "--resume", "cl-123"]
                )

    def test_opencode_sqlite_fallback_and_ambiguity_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE session (id TEXT, project_id TEXT, parent_id TEXT, directory TEXT, title TEXT, time_updated INTEGER, time_archived INTEGER)"
            )
            conn.execute(
                "INSERT INTO session VALUES ('ses_abc', 'p1', NULL, ?, 'Kimi setup', 200, NULL)",
                (tmp,),
            )
            conn.commit()
            conn.close()

            adapter = OpenCodeAdapter(db_path=db)
            self.assertEqual(
                adapter.session_id(
                    address="t3v4",
                    tree_pids=[],
                    fg_argv=["opencode", "--standalone"],
                    term_argv=[],
                    cwd=tmp,
                    title="OC | Kimi setup",
                    screen_text="",
                ),
                "ses_abc",
            )

            # Adding a second unrelated active session makes titleless lookup fail closed.
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO session VALUES ('ses_def', 'p1', NULL, ?, 'Another thread', 300, NULL)",
                (tmp,),
            )
            conn.commit()
            conn.close()
            self.assertIsNone(
                adapter.session_id(
                    address="t3v4",
                    tree_pids=[],
                    fg_argv=["opencode", "--standalone"],
                    term_argv=[],
                    cwd=tmp,
                    title="",
                    screen_text="",
                )
            )

    def test_hermes_env_preservation_sqlite_prefix_and_quit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL)")
            conn.execute(
                "INSERT INTO sessions VALUES ('local-d48355ee0efe8e9b909b', 100.0)"
            )
            conn.commit()
            conn.close()

            adapter = HermesAdapter(db_path=db)
            term_argv = [
                "/bin/zsh",
                "-lic",
                '"$@"; exec "$0" -li',
                "/bin/zsh",
                "/usr/bin/env",
                "HERMES_TUI_DIR=/opt/hermes/tui",
                "HERMES_NODE=/opt/node/bin/node",
                "/opt/hermes/bin/hermes",
                "chat",
                "--tui",
                "--provider",
                "nous",
                "--model",
                "qwen/qwen4-max:free",
            ]
            fg_argv = [
                "/opt/hermes/.venv/bin/python",
                "/opt/hermes/bin/hermes",
                "chat",
                "--tui",
                "--provider",
                "nous",
                "--model",
                "qwen/qwen4-max:free",
            ]
            self.assertEqual(detect_kind(None, fg_argv, term_argv), HarnessKind.hermes)
            self.assertEqual(adapter.exit_command(), "/quit")
            self.assertEqual(
                adapter.extra_env(term_argv),
                {
                    "HERMES_TUI_DIR": "/opt/hermes/tui",
                    "HERMES_NODE": "/opt/node/bin/node",
                },
            )
            sid = adapter.session_id(
                address="t3rt",
                tree_pids=[],
                fg_argv=fg_argv,
                term_argv=term_argv,
                cwd="/workspace/repo",
                title="",
                screen_text="Session: local-d48355ee\n❯ ",
            )
            self.assertEqual(sid, "local-d48355ee0efe8e9b909b")
            # Title fallback works when startup banner scrolled off.
            conn = sqlite3.connect(db)
            conn.execute("DROP TABLE sessions")
            conn.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL)"
            )
            conn.execute(
                "INSERT INTO sessions VALUES ('local-8b4926a329df', 'Review amd64 self-hosted compiler wall-time savings', 200.0)"
            )
            conn.commit()
            conn.close()
            self.assertEqual(
                adapter.session_id(
                    address="t3rt",
                    tree_pids=[],
                    fg_argv=fg_argv,
                    term_argv=term_argv,
                    cwd="/workspace/repo",
                    title="✓ Review amd64 self-hosted co… · qwen4-max:free · …",
                    screen_text="❯ ",
                ),
                "local-8b4926a329df",
            )
            assert sid is not None
            snap = _term(
                HarnessKind.hermes,
                fg_argv=tuple(fg_argv),
                term_argv=tuple(term_argv),
                session_id=sid,
            )
            cmd = adapter.resume_argv(
                snap,
                session_id=sid,
                is_coordinator=False,
                prompt=None,
                screen_text="❯ ",
            )
            self.assertEqual(
                cmd,
                [
                    "/opt/hermes/bin/hermes",
                    "chat",
                    "--tui",
                    "--provider",
                    "nous",
                    "--model",
                    "qwen/qwen4-max:free",
                    "--resume",
                    sid,
                ],
            )

    def test_zcode_sqlite_and_resume_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT, time_updated INTEGER)"
            )
            conn.execute(
                "INSERT INTO session VALUES ('sess_z1', ?, 'ZCode Task', 100)", (tmp,)
            )
            conn.commit()
            conn.close()

            adapter = ZCodeAdapter(db_path=db)
            fg = ["zcode", "--mode", "yolo"]
            self.assertEqual(detect_kind(None, fg, []), HarnessKind.zcode)
            sid = adapter.session_id(
                address="t9",
                tree_pids=[],
                fg_argv=fg,
                term_argv=[],
                cwd=tmp,
                title="",
                screen_text="",
            )
            self.assertEqual(sid, "sess_z1")
            snap = _term(HarnessKind.zcode, fg_argv=tuple(fg), session_id="sess_z1")
            self.assertEqual(
                adapter.resume_argv(
                    snap,
                    session_id="sess_z1",
                    is_coordinator=False,
                    prompt=None,
                    screen_text="",
                ),
                ["zcode", "--mode", "yolo", "--resume", "sess_z1"],
            )
            self.assertEqual(
                set(ADAPTERS), {k for k in HarnessKind if k is not HarnessKind.shell}
            )

    def test_codex_positional_break_bottom_footer_and_verify_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir(parents=True)
            rollout_old = (
                sessions_dir
                / "rollout-2026-11-23-01a1f5ba-9341-7890-976e-75cc768f8dd5.jsonl"
            )
            rollout_old.write_text("{}\n", encoding="utf-8")
            adapter = CodexAdapter()

            snap = _term(
                HarnessKind.codex,
                fg_argv=(
                    "codex",
                    "--model",
                    "gpt-5",
                    "Inspect --unknown-flag in foo.py",
                ),
                session_id="01a1f5ba-9341-7890-976e-75cc768f8dd5",
            )
            # Stops parsing flags once the trailing positional prompt starts.
            cmd = adapter.resume_argv(
                snap,
                session_id="01a1f5ba-9341-7890-976e-75cc768f8dd5",
                is_coordinator=False,
                prompt=None,
                screen_text="",
            )
            self.assertEqual(
                cmd,
                [
                    "codex-hc",
                    "resume",
                    "01a1f5ba-9341-7890-976e-75cc768f8dd5",
                    "--model",
                    "gpt-5",
                ],
            )

            # Bottom-most footer wins when earlier scrolled history has another model/effort line.
            older_id = "01999999-1111-7890-976e-75cc768f8dd5"
            newer_id = "01a1f5ba-9341-7890-976e-75cc768f8dd5"
            screen = (
                "  GPT-5.3-Codex low · 80% left\n"
                "some output\n"
                "  GPT-5.4 high · 100% left\n"
                "› "
            )
            snap_footer = _term(
                HarnessKind.codex,
                fg_argv=("codex", "--model", "o3"),
                session_id=newer_id,
            )
            self.assertEqual(
                adapter.resume_argv(
                    snap_footer,
                    session_id=newer_id,
                    is_coordinator=False,
                    prompt=None,
                    screen_text=screen,
                ),
                [
                    "codex-hc",
                    "resume",
                    newer_id,
                    "--model",
                    "gpt-5.4",
                    "-c",
                    'model_reasoning_effort="high"',
                ],
            )

            # verify_resumed fails when live thread differs from target session_id or rollout is missing.
            wrong_rollout = sessions_dir / f"rollout-2026-11-23-{older_id}.jsonl"
            with mock.patch(
                "herdr_roll.adapters.run_cmd",
                return_value=f"n{wrong_rollout}\n",
            ):
                ok, reason = adapter.verify_resumed(
                    old_pid=100,
                    new_pid=200,
                    fg_argv=["codex", "resume", newer_id],
                    tree_pids=[200],
                    status_state="idle",
                    screen_text="› ",
                    session_id=newer_id,
                )
                self.assertFalse(ok)
                self.assertIsNotNone(reason)

            with mock.patch("herdr_roll.adapters.run_cmd", return_value=""):
                ok_missing, _ = adapter.verify_resumed(
                    old_pid=100,
                    new_pid=200,
                    fg_argv=["codex", "resume", newer_id],
                    tree_pids=[200],
                    status_state="idle",
                    screen_text="› ",
                    session_id=newer_id,
                )
                self.assertFalse(ok_missing)

    def test_agy_lsof_brain_fallback(self) -> None:
        adapter = AgyAdapter()
        brain_uuid = "0e69df85-d730-4acc-89e2-e1d11f2b4c25"
        with mock.patch(
            "herdr_roll.adapters.run_cmd",
            return_value=f"n/Users/demo/.gemini/antigravity-cli/brain/{brain_uuid}/task_plan.md\n",
        ):
            sid = adapter.session_id(
                address="t3w9",
                tree_pids=[555],
                fg_argv=["agy"],
                term_argv=[],
                cwd="/workspace/repo",
                title="",
                screen_text="",
            )
            self.assertEqual(sid, brain_uuid)

    def test_zcode_sqlite_filters_archived_deleted_and_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT, parent_id TEXT, time_archived INTEGER, time_deleted INTEGER, time_updated INTEGER)"
            )
            conn.execute(
                "INSERT INTO session VALUES ('sess_sub', ?, 'Subagent', 'sess_root', NULL, NULL, 500)",
                (tmp,),
            )
            conn.execute(
                "INSERT INTO session VALUES ('sess_arch', ?, 'Archived', NULL, 400, NULL, 400)",
                (tmp,),
            )
            conn.execute(
                "INSERT INTO session VALUES ('sess_del', ?, 'Deleted', NULL, NULL, 300, 300)",
                (tmp,),
            )
            conn.execute(
                "INSERT INTO session VALUES ('sess_live', ?, 'Active', NULL, NULL, NULL, 200)",
                (tmp,),
            )
            conn.commit()
            conn.close()

            adapter = ZCodeAdapter(db_path=db)
            self.assertEqual(
                adapter.session_id(
                    address="t9",
                    tree_pids=[],
                    fg_argv=["zcode"],
                    term_argv=[],
                    cwd=tmp,
                    title="",
                    screen_text="",
                ),
                "sess_live",
            )

    def test_hermes_late_resume_arg_and_latest_rejection(self) -> None:
        adapter = HermesAdapter()
        late_fg = [
            "/opt/hermes/.venv/bin/python",
            "/opt/hermes/bin/hermes",
            "chat",
            "--tui",
            "--provider",
            "nous",
            "--model",
            "qwen/qwen4-max:free",
            "-r",
            "local-late1234",
        ]
        self.assertEqual(detect_kind(None, late_fg, []), HarnessKind.hermes)
        with mock.patch.object(
            adapter, "_lookup_sqlite", return_value="local-late1234"
        ):
            self.assertEqual(
                adapter.session_id(
                    address="t3rt",
                    tree_pids=[],
                    fg_argv=late_fg,
                    term_argv=[],
                    cwd="/workspace/repo",
                    title="",
                    screen_text="",
                ),
                "local-late1234",
            )
        # `--resume latest` must not be accepted as a durable session ID.
        with mock.patch.object(adapter, "_lookup_sqlite", return_value=None):
            self.assertIsNone(
                adapter.session_id(
                    address="t3rt",
                    tree_pids=[],
                    fg_argv=["hermes", "chat", "--resume", "latest"],
                    term_argv=[],
                    cwd="/workspace/repo",
                    title="",
                    screen_text="",
                )
            )

    def test_opencode_auto_and_server_flags_preserved(self) -> None:
        adapter = OpenCodeAdapter()
        snap = _term(
            HarnessKind.opencode,
            fg_argv=(
                "opencode",
                "--auto",
                "--server",
                "http://127.0.0.1:4096",
                "--model",
                "deepseek-v4.1-flash-free",
            ),
            session_id="ses_xyz",
        )
        cmd = adapter.resume_argv(
            snap,
            session_id="ses_xyz",
            is_coordinator=False,
            prompt=None,
            screen_text="",
        )
        self.assertEqual(
            cmd,
            [
                "opencode",
                "--auto",
                "--server",
                "http://127.0.0.1:4096",
                "--model",
                "deepseek-v4.1-flash-free",
                "-s",
                "ses_xyz",
            ],
        )


if __name__ == "__main__":
    unittest.main()
