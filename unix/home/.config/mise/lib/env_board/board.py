"""Pure parsing and rendering helpers for the harness quota board.

Everything here is stdlib-only and side-effect free so it can be unit tested
offline. Collection (subprocess, filesystem) lives in ``collect`` below the
render helpers and is injected into ``cli`` so tests never touch the network.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

BAR_WIDTH = 12
FAMILIES = ("Claude", "OpenAI/Codex", "Gemini/agy", "Other")


def family_of(model: str) -> str:
    """Map a model name from AgentsView to a harness-family label."""
    name = model.lower()
    if name.startswith("claude"):
        return "Claude"
    if name.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "OpenAI/Codex"
    if name.startswith("gemini"):
        return "Gemini/agy"
    return "Other"


def bar(percent: float | None, width: int = BAR_WIDTH) -> str:
    """Return a fixed-width remaining-quota bar; unknown renders as dashes."""
    if percent is None:
        return "-" * width
    bounded = max(0.0, min(100.0, float(percent)))
    filled = round(bounded / 100.0 * width)
    return "#" * filled + "." * (width - filled)


def fmt_tokens(count: int | float | None) -> str:
    """Compact token count: 950, 12k, 3.4M, 1.2B."""
    value = float(count or 0)
    if value >= 1e9:
        return f"{value / 1e9:.1f}B"
    if value >= 1e6:
        return f"{value / 1e6:.1f}M"
    if value >= 1e3:
        return f"{round(value / 1e3)}k"
    return str(int(value))


def fmt_usd(amount: float | None) -> str:
    """Dollar amount with cents; None renders as n/a."""
    if amount is None:
        return "n/a"
    return f"${amount:,.2f}"


def fmt_until(reset_iso: str | None, now: datetime) -> str:
    """Time until an ISO reset instant: 3d4h, 5h12m, 7m, now, or ?."""
    if not reset_iso:
        return "?"
    try:
        reset = datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    if reset.year < 2000:
        return "?"
    seconds = int((reset - now).total_seconds())
    if seconds <= 0:
        return "now"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{max(minutes, 1)}m"


def parse_quota(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten an EnvQuotaResponse dump into one row per harness window."""
    rows: list[dict[str, Any]] = []
    for reading in payload.get("readings", []):
        quota = reading.get("quota", {})
        harness = reading.get("harness")
        windows = quota.get("windows") or []
        if not windows:
            rows.append(
                {
                    "harness": harness,
                    "label": "-",
                    "remaining": None,
                    "reset": None,
                    "ok": bool(quota.get("probe_ok")),
                    "available": bool(quota.get("available")),
                    "note": quota.get("reason") or "",
                }
            )
        for window in windows:
            rows.append(
                {
                    "harness": harness,
                    "label": window.get("label") or "-",
                    "remaining": window.get("remaining_percent"),
                    "reset": window.get("reset"),
                    "ok": bool(quota.get("probe_ok")),
                    "available": bool(quota.get("available")),
                    "note": "",
                }
            )
    return rows


def parse_usage(payload: Mapping[str, Any], today: str) -> dict[str, dict[str, Any]]:
    """Aggregate AgentsView ``report usage daily`` JSON by model family.

    Returns per-family ``today`` and ``window`` token totals and list-price
    cost estimates in USD (AgentsView computes cost from public prices, so for
    subscription harnesses it is an estimate, not billed spend).
    """
    out: dict[str, dict[str, Any]] = {
        fam: {"today_tok": 0, "today_usd": 0.0, "win_tok": 0, "win_usd": 0.0}
        for fam in FAMILIES
    }
    for day in payload.get("daily", []):
        is_today = day.get("date") == today
        for model in day.get("modelBreakdowns", []):
            fam = family_of(str(model.get("modelName", "")))
            tokens = (
                int(model.get("inputTokens", 0))
                + int(model.get("outputTokens", 0))
                + int(model.get("cacheCreationTokens", 0))
                + int(model.get("cacheReadTokens", 0))
            )
            usd = (model.get("cost") or {}).get("microdollars", 0) / 1e6
            out[fam]["win_tok"] += tokens
            out[fam]["win_usd"] += usd
            if is_today:
                out[fam]["today_tok"] += tokens
                out[fam]["today_usd"] += usd
    return out


def parse_ledger(lines: Iterable[str]) -> dict[str, Any]:
    """Sum per-run API-credit spend from the fleet ledger JSONL."""
    spent = 0.0
    runs = 0
    last_ts = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        cost = event.get("cost_usd")
        if event.get("event") == "finished" and isinstance(cost, (int, float)):
            spent += float(cost)
            runs += 1
        last_ts = event.get("ts", last_ts)
    return {"spent_usd": spent, "runs": runs, "last_ts": last_ts}


def find_ledger(root: Path) -> Path | None:
    """Newest ``api-credit-*/ledger.jsonl`` under ``root`` (None if absent)."""
    candidates = sorted(root.glob("api-credit-*/ledger.jsonl"))
    return candidates[-1] if candidates else None


def render(
    *,
    now: datetime,
    quota_rows: Sequence[Mapping[str, Any]] | None,
    quota_age_s: int | None,
    usage: Mapping[str, Mapping[str, Any]] | None,
    ledger: Mapping[str, Any] | None,
    window_days: int,
    errors: Sequence[str] = (),
    interval_s: int = 10,
) -> str:
    """Render the whole board as plain text (no ANSI) for one frame."""
    stamp = now.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"AI HARNESS QUOTAS & USAGE   {stamp}   refresh {interval_s}s", ""]

    age = f"probed {quota_age_s}s ago" if quota_age_s is not None else "pending"
    lines.append(f"SUBSCRIPTION QUOTA REMAINING  [live, zero-token probe, {age}]")
    if quota_rows is None:
        lines.append("  (probe pending...)")
    else:
        for row in quota_rows:
            name = str(row["harness"] or "?")
            if not row["ok"]:
                lines.append(f"  {name:<9} UNKNOWN  {row['note']}")
                continue
            if row["remaining"] is None:
                state = "ok" if row["available"] else "LIMITED"
                lines.append(f"  {name:<9} {state:<8} {row['note'][:60]}")
                continue
            pct = float(row["remaining"])
            lines.append(
                f"  {name:<9} {row['label'][:30]:<30} [{bar(pct)}] {pct:5.1f}%"
                f"  resets {fmt_until(row['reset'], now)}"
            )
    lines.append("")

    lines.append(
        "LOCAL USAGE  [AgentsView transcripts; $ = list-price estimate, not billed]"
    )
    lines.append(
        f"  {'family':<13} {'today tok':>10} {'today $':>9} {f'{window_days}d tok':>10} {f'{window_days}d $':>10}"
    )
    if usage is None:
        lines.append("  (AgentsView unavailable)")
    else:
        for fam in FAMILIES:
            row = usage[fam]
            if not (row["win_tok"] or row["today_tok"]):
                continue
            lines.append(
                f"  {fam:<13} {fmt_tokens(row['today_tok']):>10} {fmt_usd(row['today_usd']):>9}"
                f" {fmt_tokens(row['win_tok']):>10} {fmt_usd(row['win_usd']):>10}"
            )
    lines.append("")

    lines.append("API CREDIT")
    if ledger is None:
        lines.append("  Anthropic  ledger not found; no local spend record")
    else:
        lines.append(
            f"  Anthropic  ledger spend {fmt_usd(ledger['spent_usd'])} over {ledger['runs']} runs"
            f" (derived, last event {ledger['last_ts'] or '?'})"
        )
    lines.append(
        "             live balance: n/a (no public balance endpoint; cost_report needs Admin key)"
    )
    lines.append(
        "  OpenAI     live balance/spend: n/a (organization costs API needs Admin key)"
    )
    lines.append(
        "             overlay keys are scoped to POST /v1/responses; no metadata route"
    )
    for err in errors:
        lines.append(f"! {err}")
    return "\n".join(lines) + "\n"
