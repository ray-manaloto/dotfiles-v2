"""Offline tests for the harness quota board parsing and rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from env_board import board

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def test_family_of_maps_models():
    assert board.family_of("claude-opus-5-5") == "Claude"
    assert board.family_of("gpt-6.2-sol") == "OpenAI/Codex"
    assert board.family_of("Gemini 4 Neon (XHigh)") == "Gemini/agy"
    assert board.family_of("glm-5") == "Other"


def test_bar_bounds_and_unknown():
    assert board.bar(None, 4) == "----"
    assert board.bar(0, 4) == "...."
    assert board.bar(100, 4) == "####"
    assert board.bar(250, 4) == "####"
    assert board.bar(50, 4) == "##.."


def test_fmt_helpers():
    assert board.fmt_tokens(950) == "950"
    assert board.fmt_tokens(12_400) == "12k"
    assert board.fmt_tokens(3_400_000) == "3.4M"
    assert board.fmt_tokens(1_200_000_000) == "1.2B"
    assert board.fmt_usd(1234.5) == "$1,234.50"
    assert board.fmt_usd(None) == "n/a"


def test_fmt_until():
    assert board.fmt_until("2026-11-30T13:00:00+00:00", NOW) == "3d1h"
    assert board.fmt_until("2026-11-27T17:12:00+00:00", NOW) == "5h12m"
    assert board.fmt_until("2026-11-27T12:07:00+00:00", NOW) == "7m"
    assert board.fmt_until("2026-11-27T11:00:00+00:00", NOW) == "now"
    assert board.fmt_until("1970-01-01T00:00:00+00:00", NOW) == "?"
    assert board.fmt_until(None, NOW) == "?"
    assert board.fmt_until("garbage", NOW) == "?"


def test_parse_quota_flattens_windows_and_empty_readings():
    payload = {
        "readings": [
            {
                "harness": "claude",
                "quota": {
                    "available": True,
                    "probe_ok": True,
                    "windows": [
                        {"label": "Weekly", "remaining_percent": 80.0, "reset": "x"}
                    ],
                },
            },
            {
                "harness": "opencode",
                "quota": {
                    "available": True,
                    "probe_ok": True,
                    "windows": [],
                    "reason": "free",
                },
            },
        ]
    }
    rows = board.parse_quota(payload)
    assert [r["harness"] for r in rows] == ["claude", "opencode"]
    assert rows[0]["remaining"] == 80.0
    assert rows[1]["remaining"] is None and rows[1]["note"] == "free"


def test_parse_usage_groups_today_and_window():
    payload = {
        "daily": [
            {
                "date": "2026-11-26",
                "modelBreakdowns": [
                    {
                        "modelName": "claude-opus-5-5",
                        "inputTokens": 1,
                        "outputTokens": 2,
                        "cacheCreationTokens": 3,
                        "cacheReadTokens": 4,
                        "cost": {"microdollars": 2_000_000},
                    }
                ],
            },
            {
                "date": "2026-11-27",
                "modelBreakdowns": [
                    {
                        "modelName": "gpt-6.2-sol",
                        "inputTokens": 10,
                        "outputTokens": 0,
                        "cacheCreationTokens": 0,
                        "cacheReadTokens": 0,
                        "cost": {"microdollars": 500_000},
                    }
                ],
            },
        ]
    }
    usage = board.parse_usage(payload, "2026-11-27")
    assert usage["Claude"]["win_tok"] == 10 and usage["Claude"]["today_tok"] == 0
    assert usage["Claude"]["win_usd"] == 2.0
    assert usage["OpenAI/Codex"]["today_tok"] == 10
    assert usage["OpenAI/Codex"]["today_usd"] == 0.5


def test_parse_ledger_sums_finished_only_and_tolerates_junk():
    lines = [
        json.dumps({"event": "launch", "ts": "a"}),
        json.dumps({"event": "finished", "cost_usd": 1.25, "ts": "b"}),
        "not json",
        json.dumps({"event": "finished", "cost_usd": None, "ts": "c"}),
        json.dumps({"event": "finished", "cost_usd": 0.75, "ts": "d"}),
        "",
    ]
    ledger = board.parse_ledger(lines)
    assert ledger == {"spent_usd": 2.0, "runs": 2, "last_ts": "d"}


def test_find_ledger_picks_newest(tmp_path: Path):
    assert board.find_ledger(tmp_path) is None
    for name in ("api-credit-2026-11-26", "api-credit-2026-11-27"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "ledger.jsonl").write_text("{}\n")
    newest = board.find_ledger(tmp_path)
    assert newest is not None
    assert newest.parent.name == "api-credit-2026-11-27"


def test_render_labels_unavailable_balances_and_has_no_secret_material():
    rows = [
        {
            "harness": "claude",
            "label": "Weekly",
            "remaining": 80.0,
            "reset": "2026-11-30T13:00:00+00:00",
            "ok": True,
            "available": True,
            "note": "",
        },
        {
            "harness": "agy",
            "label": "x",
            "remaining": None,
            "reset": None,
            "ok": False,
            "available": False,
            "note": "probe failed",
        },
    ]
    usage = board.parse_usage({"daily": []}, "2026-11-27")
    text = board.render(
        now=NOW,
        quota_rows=rows,
        quota_age_s=3,
        usage=usage,
        ledger={"spent_usd": 258.42, "runs": 81, "last_ts": "t"},
        window_days=7,
        errors=["usage: TimeoutExpired"],
    )
    assert "80.0%" in text and "resets 3d1h" in text
    assert "UNKNOWN" in text
    assert "$258.42" in text and "derived" in text
    assert "Admin key" in text
    assert "! usage: TimeoutExpired" in text
    assert "sk-" not in text


def test_render_pending_states():
    text = board.render(
        now=NOW,
        quota_rows=None,
        quota_age_s=None,
        usage=None,
        ledger=None,
        window_days=7,
    )
    assert "probe pending" in text and "AgentsView unavailable" in text
    assert "ledger not found" in text


def test_parse_usage_tolerates_null_token_fields():
    # Review finding: a null token field used to raise TypeError and blank the whole usage poll.
    payload = {
        "daily": [
            {
                "date": "2026-11-27",
                "modelBreakdowns": [
                    {
                        "modelName": "claude-opus-5-5",
                        "inputTokens": None,
                        "outputTokens": 5,
                        "cacheCreationTokens": None,
                        "cacheReadTokens": None,
                        "cost": {"microdollars": 0},
                    }
                ],
            }
        ]
    }
    usage = board.parse_usage(payload, "2026-11-27")
    assert usage["Claude"]["today_tok"] == 5


# --- API-credit balance lane -------------------------------------------------

import fcntl  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import timedelta  # noqa: E402

from env_board import balance  # noqa: E402

FAKE_OK = {
    "ok": True,
    "total_usd": 12.5,
    "purchased_usd": 2.5,
    "auto_reload": False,
    "promo": [
        {
            "name": "fake-grant",
            "program": "fake_program",
            "remaining_usd": 10,
            "granted_usd": 20,
            "granted_at": "2026-11-01T00:00:00Z",
            "expires_at": "2026-11-30T13:00:00+00:00",
            "expired": False,
        },
        {
            "name": "old-grant",
            "program": None,
            "remaining_usd": 1,
            "granted_usd": 1,
            "granted_at": None,
            "expires_at": "2026-12-30T00:00:00+00:00",
            "expired": False,
        },
    ],
    "promo_left_usd": 11,
    "read_at": "2026-11-27T11:50:00+00:00",
}


class Harness:
    """Scheduler with injected runner/availability/clock and a tmp state dir."""

    def __init__(
        self, tmp_path: Path, now: datetime = NOW, available: str | None = None
    ):
        self.now = now
        self.calls = 0
        self.result: tuple = (None, "boom")
        self.available = available
        self.sched = balance.BalanceScheduler(
            state=tmp_path / "env-board",
            repo_root=tmp_path,
            runner=self._run,
            availability=lambda _root: self.available,
            clock=lambda: self.now,
        )

    def _run(self, _root: Path):
        self.calls += 1
        return self.result

    def ok(self, read_at: datetime | None = None):
        payload = dict(FAKE_OK)
        if read_at is not None:
            payload["read_at"] = balance.iso(read_at)
        reading, error = balance.parse_task_output(json.dumps(payload))
        assert error is None
        self.result = (reading, None)

    def write_settings(self, **fields):
        self.sched.state.mkdir(parents=True, exist_ok=True)
        self.sched.settings_path.write_text(json.dumps(fields))


def test_first_tick_calls_then_fresh_cache_skips(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    assert h.sched.tick() is True and h.calls == 1
    h.now = NOW + timedelta(minutes=9)
    assert h.sched.tick() is False and h.calls == 1
    h.now = NOW + timedelta(minutes=10, seconds=1)
    assert h.sched.tick() is True and h.calls == 2


def test_restart_with_fresh_cache_does_not_call(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    h.sched.tick()
    again = Harness(tmp_path)  # new process, same state dir
    again.now = NOW + timedelta(minutes=2)
    assert again.sched.tick() is False and again.calls == 0


def test_error_keeps_last_good_and_backs_off_full_interval(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    h.sched.tick()
    h.now = NOW + timedelta(minutes=11)
    h.result = (None, "console login required")
    assert h.sched.tick() is True
    cache = balance.load_cache(h.sched.cache_path)
    assert cache.read is not None and cache.read.total_usd == 12.5
    assert cache.last_error == "console login required"
    h.now += timedelta(minutes=9)
    assert h.sched.tick() is False and h.calls == 2
    h.now += timedelta(minutes=2)
    assert h.sched.tick() is True and h.calls == 3


def test_runner_exception_is_recorded_not_raised(tmp_path):
    h = Harness(tmp_path)

    def boom(_root):
        raise RuntimeError("x")

    h.sched.runner = boom
    assert h.sched.tick() is True
    assert balance.load_cache(h.sched.cache_path).last_error == (
        "api:balance failed: RuntimeError"
    )


def test_unavailable_never_spawns(tmp_path):
    h = Harness(tmp_path, available=balance.UNAVAILABLE_BROWSE)
    assert h.sched.tick() is False and h.calls == 0
    assert h.sched.view()["unavailable"] == "unavailable: browse CLI not installed"
    assert not h.sched.cache_path.exists()


def test_check_available_reports_missing_task_and_browse(tmp_path, monkeypatch):
    assert balance.check_available(tmp_path) == balance.UNAVAILABLE_TASK
    (tmp_path / "tasks" / "api").mkdir(parents=True)
    (tmp_path / "tasks" / "api" / "balance").write_text("#!/bin/sh\n")
    monkeypatch.setattr(balance.shutil, "which", lambda _name: None)
    assert balance.check_available(tmp_path) == balance.UNAVAILABLE_BROWSE
    monkeypatch.setattr(balance.shutil, "which", lambda _name: "/bin/browse")
    assert balance.check_available(tmp_path) is None


def test_single_flight_lockfile_and_in_process_lock(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    h.sched.state.mkdir(parents=True)
    fd = os.open(h.sched.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert h.sched.tick() is False and h.calls == 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    with h.sched._lock:
        assert h.sched.tick() is False and h.calls == 0
    assert h.sched.tick() is True and h.calls == 1


def test_cache_file_is_0600_and_dir_0700(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    h.sched.tick()
    assert stat.S_IMODE(h.sched.cache_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(h.sched.state.stat().st_mode) == 0o700
    assert not [p for p in h.sched.state.iterdir() if p.name.startswith(".balance")]


def test_settings_clamp_malformed_and_partial(tmp_path):
    path = tmp_path / "settings.json"
    default = balance.DEFAULT_SETTINGS
    assert balance.load_settings(path) == default  # missing
    path.write_text("{not json")
    assert balance.load_settings(path) == default
    path.write_text(json.dumps({"balance_interval_min": 1}))
    assert balance.load_settings(path).balance_interval_min == 5
    path.write_text(json.dumps({"balance_interval_min": 9999}))
    assert balance.load_settings(path).balance_interval_min == 240
    good = balance.load_settings(
        _write(path, {"balance_interval_min": 30, "balance_paused": True})
    )
    assert (good.balance_interval_min, good.balance_paused) == (30, True)
    path.write_text(json.dumps({"balance_interval_min": "x", "balance_paused": 1}))
    assert balance.load_settings(path, good) == good  # bad types keep last good
    path.write_text(json.dumps({"balance_interval_min": True}))
    assert balance.load_settings(path, good).balance_interval_min == 30
    path.write_text("[1, 2]")
    assert balance.load_settings(path, good) == good


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload))
    return path


def test_paused_blocks_automatic_calls(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    h.write_settings(balance_paused=True)
    assert h.sched.tick() is False and h.calls == 0
    assert h.sched.view()["paused"] is True


def test_refresh_request_runs_now_but_rate_limited(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    h.sched.tick()
    h.now = NOW + timedelta(seconds=30)
    h.write_settings(refresh_requested_at=balance.iso(h.now))
    assert h.sched.tick() is False and h.calls == 1  # within 60 s of the attempt
    h.now = NOW + timedelta(seconds=61)
    assert h.sched.tick() is True and h.calls == 2
    h.now += timedelta(seconds=120)
    assert h.sched.tick() is False  # request is older than the attempt: consumed
    h.write_settings(balance_paused=True, refresh_requested_at=balance.iso(h.now))
    assert h.sched.tick() is True and h.calls == 3  # refresh works while paused


def test_interval_change_applies_without_restart(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    h.sched.tick()
    h.now = NOW + timedelta(minutes=6)
    assert h.sched.tick() is False
    h.write_settings(balance_interval_min=5)
    assert h.sched.tick() is True and h.calls == 2
    h.now += timedelta(minutes=6)
    h.write_settings(balance_interval_min=60)
    assert h.sched.tick() is False and h.sched.view()["interval_min"] == 60


def test_parse_task_output_variants():
    reading, error = balance.parse_task_output(json.dumps(FAKE_OK))
    assert error is None and reading is not None and reading.promo_left_usd == 11
    assert balance.parse_task_output("noise\n" + json.dumps(FAKE_OK))[0] is not None
    assert balance.parse_task_output('{"ok":false,"error":"nope"}') == (None, "nope")
    assert balance.parse_task_output("garbage")[1] == "api:balance returned no JSON"
    assert (
        balance.parse_task_output('{"ok":true}')[1] == "api:balance JSON shape changed"
    )


def test_run_balance_task_timeout_terminates_and_parses_nonzero_exit(tmp_path):
    marker = tmp_path / "trapped"
    script = f"trap 'echo done > {marker}; exit 0' TERM; sleep 30 & wait"
    real = subprocess.Popen

    def fake_popen(_argv, **kwargs):
        return real(["/bin/sh", "-c", script], **kwargs)

    reading, error = balance.run_balance_task(tmp_path, timeout_s=0.5, popen=fake_popen)
    assert reading is None and "timed out" in (error or "")
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists()

    def fail_popen(_argv, **kwargs):
        cmd = 'printf \'%s\' \'{"ok":false,"error":"nope"}\'; exit 1'
        return real(["/bin/sh", "-c", cmd], **kwargs)

    assert balance.run_balance_task(tmp_path, popen=fail_popen) == (None, "nope")

    def missing(_argv, **_kwargs):
        raise FileNotFoundError

    assert "could not start" in (
        balance.run_balance_task(tmp_path, popen=missing)[1] or ""
    )


def test_ticker_thread_applies_changes(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    ticker = balance.BalanceTicker(h.sched, period_s=0.05)
    ticker.start()
    deadline = time.monotonic() + 3
    while h.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    ticker.stop()
    assert h.calls == 1


def _view(tmp_path, **kw):
    h = Harness(tmp_path, **kw)
    return h


def test_render_balance_ok_stale_and_paused(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW - timedelta(minutes=7))
    h.sched.tick()
    h.now = NOW
    text = board.render(
        now=NOW,
        quota_rows=None,
        quota_age_s=None,
        usage=None,
        ledger=None,
        window_days=7,
        balance=h.sched.view(),
    )
    assert "$12.50 total" in text and "promo left $11.00 (expires in 3d1h)" in text
    assert "purchased $2.50" in text and "auto-reload off" in text
    assert "read 7m ago" in text and "every 10m" in text and "next 10m" in text
    assert "stale" not in text and "n/a (no public" not in text
    assert "OpenAI     live balance/spend: n/a" in text

    h.write_settings(balance_paused=True)
    h.sched.tick()
    h.sched.cache_path.write_text(
        balance.load_cache(h.sched.cache_path)
        .model_copy(update={"last_error": "console login required"})
        .model_dump_json()
    )
    text = board.render(
        now=NOW,
        quota_rows=None,
        quota_age_s=None,
        usage=None,
        ledger=None,
        window_days=7,
        balance=h.sched.view(),
    )
    assert "PAUSED (every 10m)" in text and "stale: console login required" in text
    assert "$12.50 total" in text  # last good values still shown


def test_render_balance_states_without_reading(tmp_path):
    base = {
        "reading": None,
        "error": None,
        "interval_min": 10,
        "paused": False,
        "next_due": None,
        "running": False,
        "unavailable": None,
    }

    def render_with(balance_view=None):
        return board.render(
            now=NOW,
            quota_rows=None,
            quota_age_s=None,
            usage=None,
            ledger=None,
            window_days=7,
            balance=balance_view,
        )

    un = render_with({**base, "unavailable": balance.UNAVAILABLE_TASK})
    assert "live balance: unavailable: api:balance task not installed" in un
    err = render_with({**base, "error": "timed out"})
    assert "live balance: error: timed out" in err
    pend = render_with(base)
    assert "pending first read" in pend
    legacy = render_with()
    assert "live balance: n/a" in legacy


def test_fmt_ago():
    assert board.fmt_ago("2026-11-27T11:59:30+00:00", NOW) == "just now"
    assert board.fmt_ago("2026-11-27T11:53:00+00:00", NOW) == "7m ago"
    assert board.fmt_ago("2026-11-27T09:55:00+00:00", NOW) == "2h05m ago"
    assert board.fmt_ago("2026-11-20T12:00:00+00:00", NOW) == "7d ago"
    assert board.fmt_ago(None, NOW) == "?" and board.fmt_ago("x", NOW) == "?"


def test_default_state_dir_honours_xdg():
    assert balance.state_dir({"XDG_STATE_HOME": "/x"}) == Path("/x/env-board")
    assert balance.state_dir({}).parts[-3:] == (".local", "state", "env-board")
    assert sys.version_info >= (3, 12)


def test_future_refresh_request_is_ignored_even_when_paused(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    h.write_settings(balance_paused=True, refresh_requested_at="2099-01-01T00:00:00Z")
    for _ in range(3):
        assert h.sched.tick() is False
    assert h.calls == 0
    h.write_settings(
        refresh_requested_at="2099-01-01T00:00:00Z"
    )  # unpaused, fresh cache
    h.sched.settings = balance.DEFAULT_SETTINGS
    h.sched.tick()
    assert h.calls == 1  # first read only (no cache); never re-triggered by 2099
    h.now += timedelta(minutes=2)
    assert h.sched.tick() is False and h.calls == 1


def test_valid_refresh_request_is_served_exactly_once(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    h.sched.tick()
    h.now = NOW + timedelta(minutes=2)
    h.write_settings(balance_paused=True, refresh_requested_at=balance.iso(h.now))
    assert h.sched.tick() is True and h.calls == 2
    for minutes in (1, 5, 30):
        h.now += timedelta(minutes=minutes)
        assert h.sched.tick() is False
    assert h.calls == 2
    cache = balance.load_cache(h.sched.cache_path)
    assert cache.last_served_request == h.sched.settings.refresh_requested_at


def test_request_slightly_in_future_within_slack_is_valid(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    h.sched.tick()
    h.now = NOW + timedelta(minutes=2)
    skew = h.now + timedelta(seconds=90)
    h.write_settings(refresh_requested_at=balance.iso(skew))
    assert h.sched.tick() is True


def test_fmt_helpers_survive_naive_and_overflow_timestamps():
    assert board.fmt_until("2026-11-27T12:07:00", NOW) == "7m"
    assert board.fmt_ago("2026-11-27T11:53:00", NOW) == "7m ago"
    assert board.fmt_until("9999-12-31T23:59:59+00:00", NOW) == "?"
    assert board.fmt_until("9999-12-31T23:59:59-23:59", NOW) == "?"
    assert board.fmt_ago("0001-01-01T00:00:00+00:00", NOW)  # does not raise
    assert board.fmt_until("2026-11-27T12:07:00", datetime(2026, 11, 27, 12, 0)) == "7m"


def test_render_survives_bad_balance_snapshot():
    bad = {
        "unavailable": None,
        "reading": {"total_usd": "x"},  # missing keys / wrong types
        "error": None,
    }
    text = board.render(
        now=NOW,
        quota_rows=None,
        quota_age_s=None,
        usage=None,
        ledger=None,
        window_days=7,
        balance=bad,
    )
    assert "balance: render error" in text and "OpenAI" in text


def test_cli_frame_survives_render_exception(monkeypatch):
    from env_board import cli

    pollers = {n: cli.Poller(n, lambda: None, 1) for n in ("quota", "usage", "ledger")}

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(pollers["quota"], "age", boom)
    assert "render error" in cli.frame(pollers, 7, 10)


def test_stop_group_grace_and_timeout_constants():
    assert balance.TERM_GRACE_S == 20 and balance.TASK_TIMEOUT_S == 90


def test_state_dir_symlink_refused_and_perms_fixed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert balance.ensure_private_dir(link) is False
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    assert balance.ensure_private_dir(loose) is True
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700
    h = Harness(tmp_path / "x")
    h.sched.state = link
    h.ok(NOW)
    assert h.sched.tick() is False and h.calls == 0


def test_lockfile_symlink_not_followed(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW)
    h.sched.state.mkdir(mode=0o700)
    target = tmp_path / "victim"
    target.write_text("keep")
    h.sched.lock_path.symlink_to(target)
    with __import__("pytest").raises(OSError):
        h.sched.tick()
    assert target.read_text() == "keep"


def test_balance_lines_fit_narrow_pane(tmp_path):
    h = Harness(tmp_path)
    h.ok(NOW - timedelta(minutes=7))
    h.sched.tick()
    h.now = NOW
    lines = board.balance_lines(h.sched.view(), NOW)
    assert max(len(line) for line in lines) <= 80


def test_settings_tightened_to_owner_only(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"balance_interval_min": 15}', encoding="utf-8")
    os.chmod(path, 0o644)
    assert balance.load_settings(path).balance_interval_min == 15
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_settings_tighten_skips_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"balance_interval_min": 15}', encoding="utf-8")
    os.chmod(target, 0o644)
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    balance.tighten_settings_mode(link)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_widget_chmods_settings_after_write():
    main_js = (
        Path(__file__).resolve().parents[1]
        / "unix/home/.config/herdr-app/mods/env-board-controls/main.js"
    ).read_text(encoding="utf-8")
    write = main_js.index("await writeTextFile(path")
    chmod = main_js.index('exec("/bin/chmod", ["-h", "600", path]')
    assert 0 < chmod - write < 300


def test_render_says_loading_while_pollers_pending():
    text = board.render(
        now=NOW,
        quota_rows=None,
        quota_age_s=None,
        usage=None,
        ledger=None,
        window_days=7,
        pending=("usage", "ledger"),
    )
    assert "ledger loading" in text and "(loading...)" in text
    assert "ledger not found" not in text and "AgentsView unavailable" not in text


def test_scheduler_reads_live_settings_before_first_tick(tmp_path):
    state = tmp_path / "env-board"
    state.mkdir()
    (state / "settings.json").write_text(
        json.dumps({"balance_interval_min": 15, "balance_paused": True})
    )
    sched = balance.BalanceScheduler(
        state=state, repo_root=tmp_path, availability=lambda _root: None
    )
    view = sched.view()
    assert view["interval_min"] == 15 and view["paused"] is True


def test_cli_frame_marks_unreported_pollers_pending():
    from env_board import cli

    pollers = {n: cli.Poller(n, lambda: None, 1) for n in ("quota", "usage", "ledger")}
    text = cli.frame(pollers, 7, 10)
    assert "ledger loading" in text and "ledger not found" not in text
    pollers["ledger"].updated = time.monotonic()  # reported: no ledger on disk
    assert "ledger not found" in cli.frame(pollers, 7, 10)


def test_wait_for_returns_once_poller_reports():
    from env_board import cli

    poller = cli.Poller("ledger", lambda: {"x": 1}, 60)
    poller.start()
    started = time.monotonic()
    cli.wait_for(poller, 5)
    assert poller.updated is not None and time.monotonic() - started < 2
