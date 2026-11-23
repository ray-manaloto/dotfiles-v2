"""Engine tests: 2-sample active_jobs, wave scheduling, flock lock, clean env quoting, git parity, roll:self."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from herdr_roll import engine
from herdr_roll.graph import RefusedError, merge_graph, select_targets
from herdr_roll.herdr import (
    LiveClient,
    clean_relaunch_command,
    is_transient_or_shell,
    owned_by_harness,
)
from herdr_roll.models import (
    GraphSnapshot,
    HarnessKind,
    SpaceSnapshot,
    TabSnapshot,
    TerminalSnapshot,
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
        # First sample sees both a short-lived script (pid 101) and a long build (pid 102).
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
                HarnessKind.codex,
                {HarnessKind.codex: ("codex",)},
                confirm_delay=0.1,
            )
        self.assertEqual(jobs, ["cargo build"])

        # Transient only in sample 1 returns empty list on sample 2.
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
                HarnessKind.codex,
                {HarnessKind.codex: ("codex",)},
                confirm_delay=0.1,
            )
        self.assertEqual(jobs_empty, [])

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
            taken_at="2026-11-23T12:00:00Z",
            coordinator="t_coord",
            spaces=(
                SpaceSnapshot(
                    id="sp1",
                    name="chief-of-staff",
                    tabs=(
                        TabSnapshot(
                            id="tb_coord",
                            name="chief-of-staff",
                            terminals=(
                                TerminalSnapshot(
                                    address="t_coord",
                                    kind=HarnessKind.codex,
                                    session_id="s0",
                                    is_coordinator=True,
                                ),
                            ),
                        ),
                    ),
                ),
                SpaceSnapshot(
                    id="sp2",
                    name="dotfiles-v2",
                    tabs=(
                        TabSnapshot(
                            id="tb_lead",
                            name="dotfiles-v2-lead",
                            terminals=(
                                TerminalSnapshot(
                                    address="t_lead",
                                    kind=HarnessKind.agy,
                                    session_id="s2",
                                ),
                            ),
                        ),
                        TabSnapshot(
                            id="tb_child",
                            name="worker-1",
                            terminals=(
                                TerminalSnapshot(
                                    address="t_child",
                                    kind=HarnessKind.codex,
                                    session_id="s1",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        ordered = select_targets(graph, all_targets=True)
        self.assertEqual(
            [t[2].address for t in ordered], ["t_coord", "t_child", "t_lead"]
        )

        # When Wave 0 coordinator fails, Wave 1 and Wave 2 are never called.
        calls: list[str] = []

        def fake_restart_one(target, **_kwargs):  # type: ignore[no-untyped-def]
            addr = target[2].address
            calls.append(addr)
            return engine.SessionOutcome(
                where=f"{target[0].name} / {target[1].name}",
                tab_id=target[1].id,
                terminal=addr,
                kind=target[2].kind,
                session_id=target[2].session_id,
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

    def test_flock_refuses_concurrent_apply(self) -> None:
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
        spaces = [
            {
                "id": "sp-df",
                "name": "dotfiles-v2",
                "tabs": [
                    {
                        "id": "tb-lead",
                        "name": "dotfiles-v2-lead",
                        "terminals": [{"terminal": "t3xb"}],
                    }
                ],
            },
            {
                "id": "sp-roll",
                "name": "dotfiles-v2",
                "tabs": [
                    {
                        "id": "tb-roll",
                        "name": "roll",
                        "terminals": [{"terminal": "t99"}],
                    }
                ],
            },
        ]
        with mock.patch("herdr_roll.engine.herdr_app_ls", return_value=spaces):
            with (
                mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t99"}),
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
                rc = engine.cmd_self(apply=True, busy_wait=25, settle=40)
                self.assertEqual(rc, 0)
                do_mock.assert_called_once()
                call_args = do_mock.call_args[0]
                self.assertEqual(call_args[0], "tab.new")
                argv_idx = call_args.index("--argv") + 1
                spawned_argv = json.loads(call_args[argv_idx])
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
                        "--apply",
                    ],
                )

    def test_restart_one_keep_env_and_single_verify_turn(self) -> None:
        space = SpaceSnapshot(id="sp1", name="dotfiles-v2")
        tab = TabSnapshot(id="tb1", name="worker")
        term = TerminalSnapshot(
            address="t8",
            kind=HarnessKind.codex,
            session_id="01a1f5ba-9341-7890-976e-75cc768f8dd5",
            cwd="/tmp/repo",
            fg_pid=200,
            fg_argv=("codex",),
        )
        with mock.patch("herdr_roll.engine.hc_read", return_value=""):
            clean_plan = engine.restart_one(
                (space, tab, term),
                timing=engine.Timing(),
                run_dir=None,
                apply=False,
                force_clean_env=True,
                verify_turn=True,
            )
            keep_plan = engine.restart_one(
                (space, tab, term),
                timing=engine.Timing(),
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

    def test_owned_by_harness_unwraps_launchers_and_ignores_prompt(self) -> None:
        self.assertTrue(
            owned_by_harness(
                HarnessKind.codex,
                '/bin/zsh -lic \'"$@"; exec "$0" -li\' /bin/zsh fnox --non-interactive proxy run -- codex',
                engine.HARNESS_BINARIES,
            )
        )
        self.assertFalse(
            owned_by_harness(
                HarnessKind.codex,
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
            "t1", HarnessKind.codex, {HarnessKind.codex: ("codex",)}
        )
        self.assertEqual(jobs, {})

    def test_select_targets_exclude_by_tab_name_and_address(self) -> None:
        sp = SpaceSnapshot(
            id="sp1",
            name="dotfiles-v2",
            tabs=(
                TabSnapshot(
                    id="tb1",
                    name="alpha",
                    terminals=(
                        TerminalSnapshot(
                            address="t1", kind=HarnessKind.codex, session_id="s1"
                        ),
                    ),
                ),
                TabSnapshot(
                    id="tb2",
                    name="beta",
                    terminals=(
                        TerminalSnapshot(
                            address="t2", kind=HarnessKind.claude, session_id="s2"
                        ),
                    ),
                ),
                TabSnapshot(
                    id="tb3",
                    name="gamma",
                    terminals=(
                        TerminalSnapshot(
                            address="t3", kind=HarnessKind.agy, session_id="s3"
                        ),
                    ),
                ),
            ),
        )
        graph = GraphSnapshot(taken_at="2026-11-23T00:00:00Z", spaces=(sp,))
        selected = select_targets(graph, all_targets=True, exclude=("alpha", "t3"))
        self.assertEqual([t[2].address for t in selected], ["t2"])

    def test_merge_graph_handles_same_name_spaces_and_dedups_live_sessions(
        self,
    ) -> None:
        prev = GraphSnapshot(
            taken_at="2026-11-23T00:00:00Z",
            spaces=(
                SpaceSnapshot(
                    id="sp_a",
                    name="shared",
                    tabs=(
                        TabSnapshot(
                            id="tb_old_a",
                            name="worker",
                            terminals=(
                                TerminalSnapshot(
                                    address="t1",
                                    kind=HarnessKind.codex,
                                    session_id="sid-moved",
                                ),
                            ),
                        ),
                    ),
                ),
                SpaceSnapshot(
                    id="sp_b",
                    name="shared",
                    tabs=(
                        TabSnapshot(
                            id="tb_old_b",
                            name="closed",
                            terminals=(
                                TerminalSnapshot(
                                    address="t2",
                                    kind=HarnessKind.codex,
                                    session_id="sid-moved",
                                ),
                                TerminalSnapshot(
                                    address="t3",
                                    kind=HarnessKind.claude,
                                    session_id="sid-kept",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        cur = GraphSnapshot(
            taken_at="2026-11-23T01:00:00Z",
            spaces=(
                SpaceSnapshot(
                    id="sp_a",
                    name="shared",
                    tabs=(
                        TabSnapshot(
                            id="tb_new_a",
                            name="worker",
                            terminals=(
                                TerminalSnapshot(
                                    address="t10",
                                    kind=HarnessKind.codex,
                                    session_id="sid-moved",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        merged = merge_graph(cur, prev)
        self.assertEqual([s.id for s in merged.spaces], ["sp_a", "sp_b"])
        sp_b = merged.spaces[1]
        self.assertTrue(sp_b.kept_from_previous_graph)
        # Only `sid-kept` should remain in the closed space (`sid-moved` is live in sp_a).
        self.assertEqual([tm.session_id for tm in sp_b.tabs[0].terminals], ["sid-kept"])

    def test_restart_one_apply_runs_verify_turn(self) -> None:
        space = SpaceSnapshot(id="sp1", name="dotfiles-v2")
        tab = TabSnapshot(id="tb1", name="worker")
        term = TerminalSnapshot(
            address="t8",
            kind=HarnessKind.codex,
            session_id="01a1f5ba-9341-7890-976e-75cc768f8dd5",
            cwd="/tmp/repo",
            fg_pid=200,
            fg_argv=("codex",),
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
                timing=engine.Timing(busy_wait=0.0, settle=1.0),
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
                tabs=(
                    TabSnapshot(
                        id="tb1",
                        name="worker",
                        terminals=(
                            TerminalSnapshot(
                                address="t1", kind=HarnessKind.codex, session_id="sid-1"
                            ),
                        ),
                    ),
                ),
            )
            fake_graph = GraphSnapshot(taken_at="2026-11-23T00:00:00Z", spaces=(sp,))
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
                    json.loads(line)
                    for line in (state_root / "runs.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(runs[0]["args"], ["snapshot", "--apply"])
                self.assertEqual(runs[1]["args"], ["verify", "--apply"])


if __name__ == "__main__":
    unittest.main()
