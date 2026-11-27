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
