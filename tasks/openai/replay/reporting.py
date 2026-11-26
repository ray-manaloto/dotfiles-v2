"""Render aggregate replay evidence without PR content or identifiers."""

from collections import Counter

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
    fn = positives - tp
    f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
    return len(accepted), tp, fp, unknown, positives, f1


def thresholds(graded):
    baseline = (triage.MIN_PROBABILITY, triage.MIN_SEVERITY)
    grid = {
        (p, s): metrics(graded, p, s)
        for p in sorted({*PROBS, baseline[0]})
        for s in sorted({*SEVERITIES, baseline[1]})
    }
    old = grid[baseline]
    eligible = (
        point
        for point, measured in grid.items()
        if old[4] >= 10 and measured[-1] - old[-1] >= 0.05 and measured[2] <= old[2]
    )
    best = max(eligible, key=lambda point: grid[point][-1], default=baseline)
    return grid, best


def money(amount):
    return f"${amount / 1_000_000:.6f}"


def render(cache, rows, estimate, response_counts, graded, batch_status):
    decline = Counter(row["decline"] for row in rows if "decline" in row)
    skipped = Counter()
    for row in rows:
        skipped.update(row.get("skipped", {}))
    labels = Counter(row[0] for row in graded)
    grid, recommended = thresholds(graded)
    entries = totals(cache)
    batch = read_json(cache / "spend-batch.json")
    batch_cost = batch["actual_microusd"] or 0
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
        "## Spend",
        "",
        f"- Pre-submit upper estimate {money(estimate['total'])}, comprising Batch "
        f"{money(estimate['batch'])} and Decisions {money(estimate['decisions'])}.",
        f"- Metered usage at published rates {money(actual)}, comprising Batch "
        f"{money(batch_cost)} and Decisions {money(actual - batch_cost)}. "
        f"Unreconciled reservation {money(unmetered)}.",
        f"- Metered cost per parsed PR {per_pr}.",
        f"- Batch output usage records {batch['input_tokens']:,} input tokens "
        f"(including {batch['cached_tokens']:,} cached) and {batch['output_tokens']:,} output tokens.",
        f"- Decisions usage records {sum(item.input_tokens for item in entries) - batch['input_tokens']:,} "
        f"input tokens (including {sum(item.cached_tokens for item in entries) - batch['cached_tokens']:,} "
        f"cached reads and {sum(item.cache_write_tokens for item in entries) - batch['cache_write_tokens']:,} cache writes).",
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
        "Precision excludes insufficient-evidence labels. Recall measures recovery of labeled defects "
        "in the raw candidate pool only. "
        "The grid excludes PRs with unavailable production triage.",
        "",
        "| Probability | Severity | Selected | True | False | Unknown | Precision | Recall |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for (p, s), (selected, tp, fp, unknown, positives, _) in grid.items():
        lines.append(
            f"| {p:.2f} | {s:.1f} | {selected} | {tp} | {fp} | {unknown} | "
            f"{rate(tp, tp + fp)} | {rate(tp, positives)} |"
        )
    p, s = recommended
    base = (triage.MIN_PROBABILITY, triage.MIN_SEVERITY)
    base_selected, base_tp, _, base_unknown, positives, _ = grid[base]
    rationale = (
        "The conservative change rule requires at least 10 labeled defects, "
        "an F1 gain of at least 0.05, and zero additional labeled false positives."
        if recommended == base
        else "This grid point improves F1 by at least 0.05 without additional false positives. "
        "The gain is in-sample and needs independent confirmation."
    )
    lines.extend(
        [
            "",
            f"The production thresholds selected {base_selected} candidates. "
            f"The grader labeled {base_unknown} inconclusive and recovered {base_tp} "
            f"of the {positives} labeled defects in the scored pool.",
            "",
            f"Recommendation is `MIN_PROBABILITY = {p:.2f}` and `MIN_SEVERITY = {s:.1f}`. {rationale}",
            "",
            "## Recurring false positives",
            "",
            *[
                f"- {label.replace('-', ' ')} — {count}"
                for label, count in labels.most_common()
                if label not in {"defect", "insufficient-evidence"}
            ],
        ]
    )
    if all(label in {"defect", "insufficient-evidence"} for label in labels):
        lines.append("The replay has no classified false positives in the scored pool.")
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
    return "\n".join(lines) + "\n"
