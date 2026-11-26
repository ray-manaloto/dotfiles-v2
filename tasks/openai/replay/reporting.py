"""Render aggregate replay evidence without PR content or identifiers."""

from collections import Counter

import batch
import independent
import triage
from inputs import REPOS, read_json
from spend import totals

PROBS = (0.70, 0.80, 0.85, 0.90, 0.95)
SEVERITIES = (0.0, 1.0, 2.0)


def rate(numerator, denominator):
    return f"{numerator / denominator:.1%}" if denominator else "—"


def metrics(graded, probability, severity):
    accepted = [row for row in graded if row[1] >= probability and row[2] >= severity]
    tp = sum(row[0] == "defect" for row in accepted)
    unknown = sum(row[0] == "insufficient-evidence" for row in accepted)
    fp = len(accepted) - tp - unknown
    positives = sum(row[0] == "defect" for row in graded)
    changed_positives = sum(row[0] == "defect" and row[3] for row in graded)
    return len(accepted), tp, fp, unknown, positives, changed_positives


def thresholds(graded):
    baseline = (triage.MIN_PROBABILITY, triage.MIN_SEVERITY)
    return {
        (p, s): metrics(graded, p, s)
        for p in sorted({*PROBS, baseline[0]})
        for s in sorted({*SEVERITIES, baseline[1]})
    }


def money(amount):
    return f"${amount / 1_000_000:.6f}"


def independent_section(summary, graded):
    rows = summary["rows"]
    accepted = [row for row in rows if row["selected"]]
    held = [row for row in rows if not row["selected"]]
    baseline = [row for row in accepted if row["baseline"]]
    weight = summary["total_rejected"] / len(held) if held else 0
    snapshot = independent.saved_thresholds(summary)
    p, s = snapshot["proposed"]
    baseline_p, baseline_s = snapshot["baseline"]
    lines = [
        "## Independent re-grade",
        "",
        f"GPT-6 Sol graded all {len(accepted)} candidates selected at probability {p:.2f} "
        f"and severity {s:.1f}, plus a simple random sample of {len(held)} of the "
        f"{summary['total_rejected']} rejects. Sampling sorted internal IDs used seed "
        f"`{summary['seed']}`. Sol received the same excerpts and rubric. Luna labels and "
        f"triage scores were withheld. {sum(row['completed'] for row in rows)} of "
        f"{len(rows)} Responses requests completed with `store: false`.",
        "",
        "| Grader | Selected defects / nondefects / inconclusive | Sampled reject defects / nondefects / inconclusive | Resolved precision | Precision lower bound | Estimated raw recall |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for grader, field in (("GPT-6 Luna", "luna"), ("GPT-6 Sol", "verdict")):
        tp, fp, unknown = independent.breakdown(accepted, field)
        missed, rejected_fp, rejected_unknown = independent.breakdown(held, field)
        positives = tp + weight * missed
        lines.append(
            f"| {grader} | {tp} / {fp} / {unknown} | {missed} / {rejected_fp} / "
            f"{rejected_unknown} | {rate(tp, tp + fp)} | {rate(tp, len(accepted))} | "
            f"{rate(tp, positives)} |"
        )

    base_tp, base_fp, base_unknown = independent.breakdown(baseline, "verdict")
    full = metrics(graded, p, s)
    pairs = [(row["luna"], row["verdict"]) for row in rows]
    resolved = [(a, b) for a, b in pairs if "insufficient-evidence" not in (a, b)]
    exact = sum(a == b for a, b in pairs)
    binary = sum((a == "defect") == (b == "defect") for a, b in resolved)
    jointly_unknown = pairs.count(("insufficient-evidence", "insufficient-evidence"))
    upgraded = pairs.count(("insufficient-evidence", "defect"))
    tokens, cost, unmetered = independent.usage_totals(rows)
    lines.extend(
        [
            "",
            f"Each sampled reject represents {summary['total_rejected']}/{len(held)} rejects "
            "when estimating raw recall."
            if held
            else "The comparison has no sampled rejects.",
            f"Luna's full-cohort recall was {rate(full[1], full[4])} across all raw candidates "
            f"and {rate(full[1], full[5])} among changed-line candidates. Under Sol, production "
            f"selected {len(baseline)} candidates (defects {base_tp}, nondefects {base_fp}, "
            f"inconclusive {base_unknown}). Estimated raw recall was {rate(base_tp, positives)}.",
            "",
            f"The graders assigned the same six-way label to {exact} of {len(rows)} candidates "
            f"({rate(exact, len(rows))}), including {jointly_unknown} jointly inconclusive cases. "
            f"They agreed on defect versus nondefect for {binary} of the {len(resolved)} cases "
            f"both resolved ({rate(binary, len(resolved))}). Sol called {upgraded} of Luna's "
            "inconclusive cases defects.",
            "",
            f"The follow-up upper estimate was **{money(summary['estimate'])}** against a "
            f"**$10** added cap. Metered usage cost **{money(cost)}** at standard rates. "
            f"The responses recorded {tokens['input']:,} input tokens, including {tokens['reads']:,} cached reads and "
            f"{tokens['writes']:,} 30-minute cache writes, and {tokens['output']:,} output tokens. "
            "[OpenAI API pricing](https://developers.openai.com/api/docs/pricing)",
        ]
    )
    if amount := summary.get("unreconciled"):
        lines.append(
            f"Unreconciled independent reservations still count against the cap: {money(amount)}."
        )
    if unmetered:
        lines.append(f"Usage was unavailable for {unmetered} responses.")
    if tp > base_tp and p == baseline_p and s < baseline_s:
        lines.extend(
            [
                "",
                "### Revised recommendation",
                "",
                f"Keep `MIN_PROBABILITY = {p:.2f}` and lower `MIN_SEVERITY` to `{s:.1f}` "
                f"for the next calibration rollout. Sol's estimated raw recall rises from "
                f"{rate(base_tp, positives)} to {rate(tp, positives)}. The lower floor recovered "
                f"{tp - base_tp} additional Sol-labeled defects and selected {fp - base_fp} "
                "additional labeled nondefects. Drop the zero-additional-false-positive rule. "
                f"Selection grew from {len(baseline)} to {len(accepted)}. "
                "Validate that volume in shadow mode before enabling automatic posting.",
            ]
        )
    return lines


def render(
    cache, rows, estimate, response_counts, graded, batch_status, previous_report
):
    decline = Counter(row["decline"] for row in rows if "decline" in row)
    skipped = Counter()
    for row in rows:
        skipped.update(row.get("skipped", {}))
    labels = Counter(row[0] for row in graded)
    grid = thresholds(graded)
    entries = totals(cache)
    batch_usage = read_json(batch.reservation_path(cache))
    batch_cost = batch_usage["actual_microusd"] or 0
    actual = sum(item.actual_microusd or 0 for item in entries)
    unmetered = sum(
        item.reserved_microusd for item in entries if item.actual_microusd is None
    )
    passed = sum("data" in row for row in rows)
    per_pr = (
        money(actual / response_counts["parsed"]) if response_counts["parsed"] else "—"
    )
    lines = [
        "# Review replay report",
        "",
        f"Batch status is **{batch_status}**. Collected **{len(rows)}** merged PRs. "
        + ", ".join(
            f"{repo.rsplit('/', 1)[1]} {sum(row['repo'] == repo for row in rows)}"
            for repo in REPOS
        )
        + ".",
        f"Production patch checks admitted **{passed}** and declined **{sum(decline.values())}**.",
        f"Successfully parsed **{response_counts['parsed']}** Batch reviews.",
        "",
        "Live replay requires `--allow-private` to send private repository patches or "
        "evidence to OpenAI. Cache-only report reproduction reads local receipts.",
        "",
        "## Spend",
        "",
        f"- Pre-submit upper estimate {money(estimate['total'])}, comprising Batch "
        f"{money(estimate['batch'])} and Decisions {money(estimate['decisions'])}.",
        f"- Metered usage at published rates {money(actual)}, comprising Batch "
        f"{money(batch_cost)} and Decisions {money(actual - batch_cost)}. "
        f"Unreconciled reservation {money(unmetered)}.",
        f"- Metered cost per parsed PR {per_pr}.",
        f"- Batch output usage records {batch_usage['input_tokens']:,} input tokens "
        f"(including {batch_usage['cached_tokens']:,} cached) and {batch_usage['output_tokens']:,} output tokens.",
        f"- Decisions usage records {sum(item.input_tokens for item in entries) - batch_usage['input_tokens']:,} "
        f"input tokens (including {sum(item.cached_tokens for item in entries) - batch_usage['cached_tokens']:,} "
        f"cached reads and {sum(item.cache_write_tokens for item in entries) - batch_usage['cache_write_tokens']:,} cache writes).",
        "",
        "## Declines and coverage",
        "",
        *[
            f"- {count} PRs — {reason.split(';')[0].removesuffix('.')}."
            for reason, count in decline.most_common()
        ],
        *[
            f"- {count} skipped files — {reason}"
            for reason, count in skipped.most_common()
        ],
        *[
            f"- {key.replace('_', ' ')} — {count}"
            for key, count in sorted(response_counts.items())
        ],
        "",
        "## Triage thresholds",
        "",
        "Resolved precision excludes inconclusive labels. The precision lower bound counts "
        "every inconclusive selection as a false positive. Raw recall uses all labeled defects "
        "in the raw candidate pool. Changed-line recall uses only labeled defects on lines "
        "that can reach production triage. Candidates with unavailable triage are unselected.",
        "",
        "| Probability | Severity | Selected | True | False | Unknown | Resolved precision | Precision lower bound | Raw recall | Changed-line recall |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for (p, s), (
        selected,
        tp,
        fp,
        unknown,
        positives,
        changed_positives,
    ) in grid.items():
        lines.append(
            f"| {p:.2f} | {s:.1f} | {selected} | {tp} | {fp} | {unknown} | "
            f"{rate(tp, tp + fp)} | {rate(tp, selected)} | {rate(tp, positives)} | "
            f"{rate(tp, changed_positives)} |"
        )
    base = (triage.MIN_PROBABILITY, triage.MIN_SEVERITY)
    base_selected, base_tp, _, base_unknown, positives, changed_positives = grid[base]
    lines.extend(
        [
            "",
            f"The production thresholds selected {base_selected} candidates. "
            f"The grader labeled {base_unknown} inconclusive and recovered {base_tp} "
            f"of {positives} labeled defects across all raw candidates. "
            f"{changed_positives} labeled defects were on changed lines.",
            "",
            "## False-positive labels in the raw candidate pool",
            "",
            *[
                f"- {label.replace('-', ' ')} — {count}"
                for label, count in labels.most_common()
                if label not in {"defect", "insufficient-evidence"}
            ],
        ]
    )
    if all(label in {"defect", "insufficient-evidence"} for label in labels):
        lines.append(
            "The replay has no classified false positives in the raw candidate pool."
        )
    prompt = {
        "preexisting": "Require the candidate to identify the behavior made incorrect by an added or modified line.",
        "incorrect": "Require direct patch evidence for each factual claim and omit candidates that depend on missing implementation.",
        "unsupported-impact": "Require a supported input or workflow and an observable consequence.",
        "duplicate": "Return at most one finding for each underlying defect.",
    }
    leading = next(
        (
            label
            for label, count in labels.most_common()
            if label in prompt and count >= 2
        ),
        None,
    )
    lines.extend(
        [
            "",
            "For the review prompt, "
            + (
                prompt[leading][0].lower() + prompt[leading][1:]
                if leading
                else "keep the current wording until the replay identifies a recurring class."
            ),
            "",
            "## Limitations",
            "",
            "The grader is a model, and shares its model with production triage. "
            "Agreement can reflect shared blind spots. Follow-up source and patches are byte-bounded excerpts. "
            "A later edit is corroboration only. A lack of a later edit is inconclusive. Path renames, "
            "squashes, and unavailable context can hide a real fix. The sample includes only the two chosen repositories "
            "and only findings returned on their admitted patches.",
        ]
    )
    summary = cache / "independent-summary.json"
    if summary.is_file():
        lines.extend(["", *independent_section(read_json(summary), graded)])
    start_marker = "<!-- BEGIN REPLAY SPOT CHECKS -->"
    end_marker = "<!-- END REPLAY SPOT CHECKS -->"
    _, start, remainder = previous_report.partition(start_marker)
    block, end, _ = remainder.partition(end_marker)
    if start and end:
        lines.extend(["", start + block + end])
    return "\n".join(lines) + "\n"
