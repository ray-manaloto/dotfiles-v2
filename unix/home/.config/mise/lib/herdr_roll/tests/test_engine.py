"""Engine tests: 2-sample active_jobs, wave scheduling, filelock, clean env quoting, cyclopts CLI, roll:self."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import orjson

from dotfiles_py.time_ns import NANOSECOND_TIMESTAMP_RE
from herdr_roll import cli, engine
from herdr_roll.graph import RefusedError, merge_graph, select_targets
from herdr_roll.herdr import (
    LiveClient,
    adaptive_poll,
    clean_relaunch_command,
    is_transient_or_shell,
    lsof_cwd,
    open_file_paths,
    owned_by_harness,
    processes,
)
from herdr_roll.models import (
    GraphSnapshot,
    HarnessKind,
    HerdrRollErrorCode,
    HerdrRollReturnCode,
    SpaceSnapshot,
    TabSnapshot,
    TerminalSnapshot,
    Timing,
    make_session_outcome,
)


class EngineTests(unittest.TestCase):
    def test_transient_filter_and_two_sample_active_jobs(self) -> None:
        self.assertTrue(is_transient_or_shell("(bash)"))
        self.assertTrue(is_transient_or_shell("(awk)"))
        self.assertTrue(is_transient_or_shell("(git)"))
        self.assertTrue(is_transient_or_shell("-zsh"))
        self.assertTrue(is_transient_or_shell("<defunct>"))
        self.assertFalse(is_transient_or_shell("cargo test --release"))

        live = LiveClient.__new__(LiveClient)
        live._lock = threading.RLock()
        live.max_workers = 1
        live.listed = True
        live.spaces = []
        live.terminals = {"t1": {"address": "t1", "state": "live"}}
        live.info = {"t1": {"foreground": {"pid": 100, "argv": ["codex"]}}}
        sample1 = {
            100: (1, "Mon", "codex"),
            101: (100, "Mon", "git status"),
            102: (100, "Mon", "cargo build"),
        }
        sample2 = {100: (1, "Mon", "codex"), 102: (100, "Mon", "cargo build")}
        live.procs = sample1

        with (
            mock.patch.object(
                live,
                "refresh_terminal",
                side_effect=lambda _address: setattr(live, "procs", sample2),
            ),
            mock.patch("herdr_roll.herdr.time.sleep"),
        ):
            jobs = live.active_jobs(
                "t1",
                HarnessKind.CODEX,
                {HarnessKind.CODEX: ("codex",)},
                confirm_delay=0.1,
            )
        self.assertEqual(jobs, ["cargo build"])

        live.procs = {100: (1, "Mon", "codex"), 101: (100, "Mon", "git status")}
        with (
            mock.patch.object(
                live,
                "refresh_terminal",
                side_effect=lambda _address: setattr(
                    live, "procs", {100: (1, "Mon", "codex")}
                ),
            ),
            mock.patch("herdr_roll.herdr.time.sleep"),
        ):
            jobs_empty = live.active_jobs(
                "t1",
                HarnessKind.CODEX,
                {HarnessKind.CODEX: ("codex",)},
                confirm_delay=0.1,
            )
        self.assertEqual(jobs_empty, [])

    def test_psutil_process_and_open_files_and_tenacity_poll(self) -> None:
        procs = processes()
        self.assertIn(os.getpid(), procs)
        self.assertEqual(
            Path(lsof_cwd(os.getpid()) or "").resolve(),
            Path.cwd().resolve(),
        )
        with tempfile.NamedTemporaryFile() as fh:
            paths = open_file_paths([os.getpid()])
            self.assertIn(
                str(Path(fh.name).resolve()), [str(Path(p).resolve()) for p in paths]
            )

        attempts = {"n": 0}

        def probe() -> str | None:
            attempts["n"] += 1
            return "ok" if attempts["n"] >= 3 else None

        with mock.patch("herdr_roll.herdr.time.sleep"):
            self.assertEqual(
                adaptive_poll(probe, 5.0, min_interval=0.05, max_interval=0.1),
                "ok",
            )
        self.assertEqual(attempts["n"], 3)

    def test_clean_relaunch_command_uses_allowlisted_env_i(self) -> None:
        base_env = {
            "HOME": "/tmp/fakehome",
            "USER": "dev",
            "LOGNAME": "dev",
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "HC_TERMINAL_ID": "t999",
            "HC_SOCKET": "/tmp/hc.sock",
            "CLAUDE_CODE_SESSION_ID": "leaked-should-drop",
            "CODEX_THREAD_ID": "leaked-should-drop",
            "RUST_LOG": "debug",
        }
        cmd = clean_relaunch_command(
            "/tmp/work dir",
            ["codex-hc", "resume", "01a1-uuid"],
            terminal_address="t3fs",
            extra_env={"HERMES_TUI_DIR": "/opt/tui"},
            base_env=base_env,
        )
        self.assertIn("cd '/tmp/work dir' && env -i ", cmd)
        self.assertIn("HC_TERMINAL_ID=t3fs", cmd)
        self.assertIn("HERMES_TUI_DIR=/opt/tui", cmd)
        self.assertIn(
            "/bin/zsh -lc 'exec \"$@\"' /bin/zsh codex-hc resume 01a1-uuid", cmd
        )
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", cmd)
        self.assertNotIn("CODEX_THREAD_ID", cmd)
        self.assertNotIn("RUST_LOG", cmd)

    def test_wave_selection_and_coordinator_failure_aborts_remaining_waves(
        self,
    ) -> None:
        graph = GraphSnapshot(
            taken_at="20261123T120000.000000000-0600",
            coordinator="t_coord",
            spaces=[
                SpaceSnapshot(
                    id="sp1",
                    name="chief-of-staff",
                    tabs=[
                        TabSnapshot(
                            id="tb_coord",
                            name="chief-of-staff",
                            terminals=[
                                TerminalSnapshot(
                                    address="t_coord",
                                    kind=HarnessKind.CODEX,
                                    session_id="s0",
                                    is_coordinator=True,
                                ),
                            ],
                        ),
                    ],
                ),
                SpaceSnapshot(
                    id="sp2",
                    name="dotfiles-v2",
                    tabs=[
                        TabSnapshot(
                            id="tb_lead",
                            name="dotfiles-v2-lead",
                            terminals=[
                                TerminalSnapshot(
                                    address="t_lead",
                                    kind=HarnessKind.AGY,
                                    session_id="s2",
                                ),
                            ],
                        ),
                        TabSnapshot(
                            id="tb_child",
                            name="worker-1",
                            terminals=[
                                TerminalSnapshot(
                                    address="t_child",
                                    kind=HarnessKind.CODEX,
                                    session_id="s1",
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )
        ordered = select_targets(graph, all_targets=True)
        self.assertEqual(
            [t[2].address for t in ordered], ["t_coord", "t_child", "t_lead"]
        )

        calls: list[str] = []

        def fake_restart_one(
            target: tuple[SpaceSnapshot, TabSnapshot, TerminalSnapshot],
            **_kwargs: object,
        ) -> engine.SessionOutcome:
            sp, tb, tm = target
            addr = tm.address
            calls.append(addr)
            return make_session_outcome(
                where=f"{sp.name} / {tb.name}",
                tab_id=tb.id,
                terminal=addr,
                kind=tm.kind,
                session_id=tm.session_id,
                result="failed-verify-timeout" if addr == "t_coord" else "ok",
            )

        with tempfile.TemporaryDirectory() as tmp:
            fake_live = mock.MagicMock()
            fake_live.me = "t_roll"
            fake_live.spaces = [
                {
                    "id": "sp1",
                    "name": "chief-of-staff",
                    "tabs": [
                        {
                            "id": "tb_r",
                            "name": "roll",
                            "terminals": [{"terminal": "t_roll"}],
                        }
                    ],
                }
            ]
            with (
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}),
                mock.patch("herdr_roll.engine.LiveClient", return_value=fake_live),
                mock.patch("herdr_roll.engine.build_graph", return_value=graph),
                mock.patch(
                    "herdr_roll.engine.restart_one", side_effect=fake_restart_one
                ),
            ):
                rc = engine.cmd_restart(all_targets=True, apply=True)
        self.assertEqual(rc, engine.EXIT_FAILED)
        self.assertEqual(calls, ["t_coord"])

    def test_filelock_refuses_concurrent_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "roll.lock"
            with (
                engine.acquire_lock(lock_path),
                self.assertRaises(RefusedError),
                engine.acquire_lock(lock_path),
            ):
                pass

    def test_roll_self_refuses_recursive_roll_tab_and_builds_detached_plan(
        self,
    ) -> None:
        graph = GraphSnapshot(
            taken_at="20261123T000000.000000000-0600",
            spaces=[
                SpaceSnapshot(
                    id="sp-df",
                    name="dotfiles-v2",
                    tabs=[
                        TabSnapshot(
                            id="tb-lead",
                            name="dotfiles-v2-lead",
                            terminals=[
                                TerminalSnapshot(
                                    address="t3xb",
                                    kind=HarnessKind.AGY,
                                    session_id="e4b6c8ee-177f-4fc6-8725-861036790dcf",
                                ),
                            ],
                        ),
                        TabSnapshot(
                            id="tb-shell",
                            name="2",
                            terminals=[
                                TerminalSnapshot(
                                    address="t3xg",
                                    kind=HarnessKind.SHELL,
                                    session_id=None,
                                ),
                            ],
                        ),
                        TabSnapshot(
                            id="tb-nosid",
                            name="fresh-codex",
                            terminals=[
                                TerminalSnapshot(
                                    address="t4x",
                                    kind=HarnessKind.CODEX,
                                    session_id=None,
                                ),
                            ],
                        ),
                        TabSnapshot(
                            id="tb-roll",
                            name="roll",
                            terminals=[
                                TerminalSnapshot(
                                    address="t99",
                                    kind=HarnessKind.SHELL,
                                    session_id=None,
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )
        fake_live = mock.MagicMock(spec=LiveClient)
        with (
            mock.patch("herdr_roll.engine.LiveClient", return_value=fake_live),
            mock.patch("herdr_roll.engine.build_graph", return_value=graph),
        ):
            with (
                mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t99"}),
                self.assertRaises(RefusedError),
            ):
                engine.cmd_self(apply=False)

            with (
                mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t3xg"}),
                self.assertRaises(RefusedError) as shell_ctx,
            ):
                engine.cmd_self(apply=False)
            self.assertIn("roll:restart --tabs t3xb --apply", str(shell_ctx.exception))

            with (
                mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t4x"}),
                self.assertRaises(RefusedError),
            ):
                engine.cmd_self(apply=False)

            with (
                mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t3xb"}),
                mock.patch(
                    "herdr_roll.engine.herdr_app_do",
                    return_value={"tab": "tb-new", "terminal": "t-new"},
                ) as do_mock,
            ):
                rc = engine.cmd_self(apply=True, busy_wait=25.0, settle=40.0)
                self.assertEqual(rc, 0)
                do_mock.assert_called_once()
                call_args = do_mock.call_args[0]
                self.assertEqual(call_args[0], "tab.new")
                argv_idx = call_args.index("--argv") + 1
                spawned_argv = orjson.loads(call_args[argv_idx])
                self.assertEqual(spawned_argv[0], sys.executable)
                self.assertEqual(spawned_argv[1:3], ["-I", "-B"])
                self.assertEqual(
                    spawned_argv[5:],
                    [
                        str(Path(engine.__file__).resolve().parent.parent),
                        "restart",
                        "--tabs",
                        "t3xb",
                        "--busy-wait",
                        "25",
                        "--settle",
                        "40",
                        "--close-self-on-ok",
                        "--apply",
                    ],
                )

    def test_cmd_restart_close_self_on_ok_only_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            target_term = TerminalSnapshot(
                address="t3xb",
                kind=HarnessKind.AGY,
                session_id="e4b6c8ee-177f-4fc6-8725-861036790dcf",
            )
            graph = GraphSnapshot(
                taken_at="20261123T000000.000000000-0600",
                spaces=[
                    SpaceSnapshot(
                        id="sp-df",
                        name="dotfiles-v2",
                        tabs=[
                            TabSnapshot(
                                id="tb-lead",
                                name="dotfiles-v2-lead",
                                terminals=[target_term],
                            ),
                        ],
                    ),
                ],
            )
            roll_spaces = [
                {
                    "id": "sp-df",
                    "name": "dotfiles-v2",
                    "tabs": [
                        {
                            "id": "tb-roll",
                            "name": "roll",
                            "terminals": [{"terminal": "t3xh"}],
                        }
                    ],
                }
            ]
            fake_live = mock.MagicMock(spec=LiveClient)
            fake_live.me = "t3xh"
            ok_outcome = make_session_outcome(
                where="dotfiles-v2 / dotfiles-v2-lead",
                tab_id="tb-lead",
                terminal="t3xb",
                kind=HarnessKind.AGY,
                session_id=target_term.session_id,
                result="ok",
            )
            fail_outcome = make_session_outcome(
                where="dotfiles-v2 / dotfiles-v2-lead",
                tab_id="tb-lead",
                terminal="t3xb",
                kind=HarnessKind.AGY,
                session_id=target_term.session_id,
                result="failed-verify-timeout",
            )
            self.assertEqual(
                fail_outcome.errors[0].code, HerdrRollErrorCode.ERR_VERIFY_TIMEOUT
            )
            self.assertEqual(
                fail_outcome.returns[0].code, HerdrRollReturnCode.RET_FAILED
            )
            self.assertRegex(ok_outcome.timestamp, NANOSECOND_TIMESTAMP_RE)

            with (
                mock.patch("herdr_roll.engine.state_dir", return_value=state_root),
                mock.patch("herdr_roll.engine.LiveClient", return_value=fake_live),
                mock.patch("herdr_roll.engine.build_graph", return_value=graph),
                mock.patch("herdr_roll.engine.herdr_app_ls", return_value=roll_spaces),
                mock.patch("herdr_roll.engine.herdr_app_do") as do_mock,
                mock.patch("herdr_roll.engine.restart_one", return_value=ok_outcome),
            ):
                rc_ok = engine.cmd_restart(
                    tabs=("t3xb",), apply=True, close_self_on_ok=True
                )
                self.assertEqual(rc_ok, 0)
                do_mock.assert_called_once_with(
                    "tab.close", "--id", "tb-roll", "--confirmed", "true"
                )

            with (
                mock.patch("herdr_roll.engine.state_dir", return_value=state_root),
                mock.patch("herdr_roll.engine.LiveClient", return_value=fake_live),
                mock.patch("herdr_roll.engine.build_graph", return_value=graph),
                mock.patch("herdr_roll.engine.herdr_app_ls", return_value=roll_spaces),
                mock.patch("herdr_roll.engine.herdr_app_do") as do_mock,
                mock.patch("herdr_roll.engine.restart_one", return_value=fail_outcome),
            ):
                rc_fail = engine.cmd_restart(
                    tabs=("t3xb",), apply=True, close_self_on_ok=True
                )
                self.assertEqual(rc_fail, 1)
                do_mock.assert_not_called()

    def test_restart_one_keep_env_and_single_verify_turn(self) -> None:
        space = SpaceSnapshot(id="sp1", name="dotfiles-v2")
        tab = TabSnapshot(id="tb1", name="worker")
        term = TerminalSnapshot(
            address="t8",
            kind=HarnessKind.CODEX,
            session_id="01a1f5ba-9341-7890-976e-75cc768f8dd5",
            cwd="/tmp/repo",
            fg_pid=200,
            fg_argv=["codex"],
        )
        with mock.patch("herdr_roll.engine.hc_read", return_value=""):
            clean_plan = engine.restart_one(
                (space, tab, term),
                timing=Timing(),
                run_dir=None,
                apply=False,
                force_clean_env=True,
                verify_turn=True,
            )
            keep_plan = engine.restart_one(
                (space, tab, term),
                timing=Timing(),
                run_dir=None,
                apply=False,
                force_clean_env=False,
                verify_turn=True,
            )
        self.assertEqual(clean_plan.notes[0], "mode=in-place-clean-env")
        self.assertEqual(keep_plan.notes[0], "mode=in-place-keep-env")
        self.assertNotIn("env -i", keep_plan.notes[2])
        self.assertNotIn(engine.VERIFY_PROMPT, clean_plan.notes[2])
        self.assertNotIn(engine.VERIFY_PROMPT, keep_plan.notes[2])
        self.assertEqual(clean_plan.errors, [])
        self.assertEqual(clean_plan.returns[0].code, HerdrRollReturnCode.RET_PLANNED)

    def test_owned_by_harness_unwraps_launchers_and_ignores_prompt(self) -> None:
        self.assertTrue(
            owned_by_harness(
                HarnessKind.CODEX,
                '/bin/zsh -lic \'"$@"; exec "$0" -li\' /bin/zsh fnox --non-interactive proxy run -- codex',
                engine.HARNESS_BINARIES,
            )
        )
        self.assertFalse(
            owned_by_harness(
                HarnessKind.CODEX,
                "python3 -c \"print('codex')\"",
                engine.HARNESS_BINARIES,
            )
        )

    def test_sample_jobs_handles_none_fg_pid(self) -> None:
        live = LiveClient.__new__(LiveClient)
        live._lock = threading.RLock()
        live.terminals = {"t1": {"address": "t1", "state": "live"}}
        live.procs = {100: (1, "Mon", "codex"), 101: (100, "Mon", "cargo test")}
        live.info = {"t1": {"foreground": {"pid": None, "argv": []}}}
        jobs = live._sample_jobs(
            "t1", HarnessKind.CODEX, {HarnessKind.CODEX: ("codex",)}
        )
        self.assertEqual(jobs, {})

    def test_select_targets_exclude_by_tab_name_and_address(self) -> None:
        sp = SpaceSnapshot(
            id="sp1",
            name="dotfiles-v2",
            tabs=[
                TabSnapshot(
                    id="tb1",
                    name="alpha",
                    terminals=[
                        TerminalSnapshot(
                            address="t1", kind=HarnessKind.CODEX, session_id="s1"
                        ),
                    ],
                ),
                TabSnapshot(
                    id="tb2",
                    name="beta",
                    terminals=[
                        TerminalSnapshot(
                            address="t2", kind=HarnessKind.CLAUDE, session_id="s2"
                        ),
                    ],
                ),
                TabSnapshot(
                    id="tb3",
                    name="gamma",
                    terminals=[
                        TerminalSnapshot(
                            address="t3", kind=HarnessKind.AGY, session_id="s3"
                        ),
                    ],
                ),
            ],
        )
        graph = GraphSnapshot(taken_at="20261123T000000.000000000-0600", spaces=[sp])
        selected = select_targets(graph, all_targets=True, exclude=("alpha", "t3"))
        self.assertEqual([t[2].address for t in selected], ["t2"])

    def test_merge_graph_handles_same_name_spaces_and_dedups_live_sessions(
        self,
    ) -> None:
        prev = GraphSnapshot(
            taken_at="20261123T000000.000000000-0600",
            spaces=[
                SpaceSnapshot(
                    id="sp_a",
                    name="shared",
                    tabs=[
                        TabSnapshot(
                            id="tb_old_a",
                            name="worker",
                            terminals=[
                                TerminalSnapshot(
                                    address="t1",
                                    kind=HarnessKind.CODEX,
                                    session_id="sid-moved",
                                ),
                            ],
                        ),
                    ],
                ),
                SpaceSnapshot(
                    id="sp_b",
                    name="shared",
                    tabs=[
                        TabSnapshot(
                            id="tb_old_b",
                            name="closed",
                            terminals=[
                                TerminalSnapshot(
                                    address="t2",
                                    kind=HarnessKind.CODEX,
                                    session_id="sid-moved",
                                ),
                                TerminalSnapshot(
                                    address="t3",
                                    kind=HarnessKind.CLAUDE,
                                    session_id="sid-kept",
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )
        cur = GraphSnapshot(
            taken_at="20261123T010000.000000000-0600",
            spaces=[
                SpaceSnapshot(
                    id="sp_a",
                    name="shared",
                    tabs=[
                        TabSnapshot(
                            id="tb_new_a",
                            name="worker",
                            terminals=[
                                TerminalSnapshot(
                                    address="t10",
                                    kind=HarnessKind.CODEX,
                                    session_id="sid-moved",
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )
        merged = merge_graph(cur, prev)
        self.assertEqual([s.id for s in merged.spaces], ["sp_a", "sp_b"])
        sp_b = merged.spaces[1]
        self.assertTrue(sp_b.kept_from_previous_graph)
        self.assertEqual([tm.session_id for tm in sp_b.tabs[0].terminals], ["sid-kept"])

    def test_restart_one_apply_runs_verify_turn(self) -> None:
        space = SpaceSnapshot(id="sp1", name="dotfiles-v2")
        tab = TabSnapshot(id="tb1", name="worker")
        term = TerminalSnapshot(
            address="t8",
            kind=HarnessKind.CODEX,
            session_id="01a1f5ba-9341-7890-976e-75cc768f8dd5",
            cwd="/tmp/repo",
            fg_pid=200,
            fg_argv=["codex"],
        )
        fake_client = mock.MagicMock(spec=LiveClient)
        fake_client.info = {"t8": {"status": {"state": "idle", "app": "codex"}}}
        fake_client.foreground.return_value = (200, ["codex"])
        fake_client.tree.return_value = [200]
        fake_client.active_jobs.return_value = []

        with (
            mock.patch("herdr_roll.engine.assert_same_tab"),
            mock.patch("herdr_roll.engine._wait_for_exit"),
            mock.patch("herdr_roll.engine.git_status_porcelain", return_value=""),
            mock.patch("herdr_roll.engine.hc_read", return_value="› "),
            mock.patch("herdr_roll.engine.hc_send", return_value=True) as send_mock,
            mock.patch("herdr_roll.engine.verify_target", return_value=(300, "t8")),
        ):
            outcome = engine.restart_one(
                (space, tab, term),
                timing=Timing(busy_wait=0.0, settle=1.0),
                run_dir=None,
                apply=True,
                verify_turn=True,
                force_clean_env=True,
                live=fake_client,
            )
        self.assertEqual(outcome.result, "ok")
        self.assertTrue(
            any(
                call.args == ("t8", engine.VERIFY_PROMPT)
                and call.kwargs.get("wait") is True
                for call in send_mock.call_args_list
            )
        )

    def test_cmd_snapshot_and_verify_preserve_argv_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            sp = SpaceSnapshot(
                id="sp1",
                name="dotfiles-v2",
                tabs=[
                    TabSnapshot(
                        id="tb1",
                        name="worker",
                        terminals=[
                            TerminalSnapshot(
                                address="t1", kind=HarnessKind.CODEX, session_id="sid-1"
                            ),
                        ],
                    ),
                ],
            )
            fake_graph = GraphSnapshot(
                taken_at="20261123T000000.000000000-0600", spaces=[sp]
            )
            with (
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}),
                mock.patch("herdr_roll.engine.state_dir", return_value=state_root),
                mock.patch("herdr_roll.engine.LiveClient") as lc_cls,
                mock.patch("herdr_roll.engine.build_graph", return_value=fake_graph),
                mock.patch(
                    "herdr_roll.engine.acquire_lock", wraps=engine.acquire_lock
                ) as lock_spy,
                mock.patch("herdr_roll.engine.verify_target", return_value=(999, "t1")),
            ):
                lc_cls.return_value.me = None
                rc_snap = engine.cmd_snapshot(apply=True, argv=("snapshot", "--apply"))
                self.assertEqual(rc_snap, 0)
                rc_ver = engine.cmd_verify(
                    tabs=("t1",), apply=True, argv=("verify", "--apply")
                )
                self.assertEqual(rc_ver, 0)
                self.assertGreaterEqual(lock_spy.call_count, 2)
                runs = [
                    orjson.loads(line)
                    for line in (state_root / "runs.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(runs[0]["args"], ["snapshot", "--apply"])
                self.assertEqual(runs[1]["args"], ["verify", "--apply"])
                self.assertRegex(runs[0]["timestamp"], NANOSECOND_TIMESTAMP_RE)
                self.assertIsInstance(runs[0]["timestamp_unix_ns"], int)
                self.assertEqual(runs[0]["returns"][0]["code"], "RET_OK")

                # Verify dual-sink structlog wrote clean JSONL without internal meta keys.
                snap_jsonl = state_root / runs[0]["run_id"] / "snapshot.jsonl"
                self.assertTrue(snap_jsonl.is_file())
                rows = [
                    orjson.loads(line)
                    for line in snap_jsonl.read_text(encoding="utf-8").splitlines()
                ]
                self.assertTrue(rows)
                for row in rows:
                    self.assertNotIn("_record", row)
                    self.assertNotIn("_from_structlog", row)
                    self.assertRegex(row["timestamp"], NANOSECOND_TIMESTAMP_RE)

    def test_cyclopts_cli_dispatches_and_returns_refused_envelope(self) -> None:
        with mock.patch("herdr_roll.cli.cmd_snapshot", return_value=0) as snap_mock:
            rc = cli.main(
                ["snapshot", "--tabs", "t1,t2", "--harnesses", "codex", "--all"]
            )
        self.assertEqual(rc, 0)
        snap_mock.assert_called_once()
        kwargs = snap_mock.call_args.kwargs
        self.assertEqual(kwargs["tabs"], ("t1", "t2"))
        self.assertEqual(kwargs["harnesses"], (HarnessKind.CODEX,))
        self.assertTrue(kwargs["all_targets"])

        rc_bad = cli.main(["snapshot", "--harnesses", "unknown_agent"])
        self.assertEqual(rc_bad, engine.EXIT_REFUSED)


if __name__ == "__main__":
    unittest.main()
