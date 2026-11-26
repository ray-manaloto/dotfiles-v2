"""Entry point for the merged PR review replay."""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import batch
import review
import triage
from filelock import FileLock
from grade import grade, grade_context, production_scores
from inputs import LOGGER, collect, read_json, write_json
from reporting import render
from spend import meter

from dotfiles_py.logging import configure_logging

ROOT = Path(__file__).resolve().parents[3]


def batch_reviews(requests, result):
    returned = result["results"]
    expected = {entry["custom_id"] for entry in requests}
    if set(returned) - expected:
        raise RuntimeError("The Batch output includes unexpected request IDs.")
    output = {
        row_id: entry["response"]["body"]
        for row_id, entry in returned.items()
        if entry.get("response") and entry["response"].get("body", {}).get("usage")
    }
    return output, expected


def candidates_for(output, schema):
    try:
        return review.parse_findings(review.response_text(output), schema)
    except (review.ReviewError, *review.MALFORMED_ERRORS):
        return None


def prepare_evidence(cache, rows, requests, schema, result):
    output, _ = batch_reviews(requests, result)
    counts = Counter()
    for row in rows:
        if row["id"] not in output:
            continue
        for item in candidates_for(output[row["id"]], schema) or ():
            context = grade_context(cache, row, item, json.loads(row["data"]))
            counts["with_context" if context else "without_context"] += 1
    write_json(cache / "evidence-ready.json", dict(counts))
    LOGGER.info("evidence_prepared", **counts)


def analyze(cache, key, rows, requests, schema, result):
    output, expected = batch_reviews(requests, result)
    if result["status"] == "completed" and output.keys() == expected:
        meter(
            cache / "spend-batch.json",
            [row["usage"] for row in output.values()],
            batch=True,
        )
    counts = Counter(missing_or_unmetered_batch=len(expected - output.keys()))
    graded = []
    for row in rows:
        row_id = row["id"]
        if row_id not in output:
            continue
        candidates = candidates_for(output[row_id], schema)
        if candidates is None:
            counts["invalid_or_incomplete_review"] += 1
            continue
        counts["parsed"] += 1
        counts["raw_candidates"] += len(candidates)
        patches = json.loads(row["data"])
        lines = review.changed_lines(patches)
        eligible = [
            item
            for item in candidates
            if item["line"] in lines.get(item["file"], set())
        ]
        counts["changed_line_candidates"] += len(eligible)
        try:
            scores, accepted = production_scores(cache, key, row_id, eligible, patches)
            counts["production_selected"] += len(accepted)
        except review.ReviewError, triage.TriageError:
            counts["production_triage_unavailable"] += 1
            scores = None
        prior = []
        for item in candidates:
            label, context = grade(cache, key, row, item, prior)
            prior.append(item["claim"][:80])
            counts["graded_with_follow_up_sources"] += context
            counts[f"raw_grade_{label}"] += 1
            if scores is not None:
                graded.append((label, *scores.get(item["id"], (-1, -1))))
    return counts, graded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=["prepare", "submit", "poll", "evidence", "finish"]
    )
    args = parser.parse_args()
    plan_id = os.environ.get("PLAN_ID", "")
    plan_root = (ROOT / ".planning" / plan_id).resolve()
    if (
        plan_root.parent != ROOT / ".planning"
        or not (plan_root / "task_plan.md").is_file()
    ):
        parser.error("Run inside an active linked worktree plan.")
    cache = plan_root / "replay-cache"
    os.umask(0o077)
    cache.mkdir(mode=0o700, exist_ok=True)
    configure_logging(jsonl_path=cache / "replay.log")
    with FileLock(cache / "replay.lock", timeout=0):
        schema = read_json(review.FINDINGS_SCHEMA)
        rows = collect(cache, cached_only=args.phase != "prepare")
        requests = batch.prepare(rows, schema)
        estimate = batch.estimate(requests, schema, triage.MAX_REQUEST_BYTES)
        write_json(cache / "estimate.json", estimate)
        LOGGER.info("estimate", prs=len(rows), admitted=len(requests), **estimate)
        if args.phase == "prepare":
            return
        if args.phase == "evidence":
            batch.existing_batch(cache, requests)
            prepare_evidence(
                cache, rows, requests, schema, read_json(cache / "batch-results.json")
            )
            return
        if triage.MODEL != "gpt-6-luna":
            raise RuntimeError(
                "Decisions model changed. Verify prices before running replay."
            )
        key = batch.api_key()
        if args.phase == "submit":
            batch.submit(cache, key, requests, estimate)
            return
        identifier = batch.existing_batch(cache, requests)
        if args.phase == "poll":
            batch.fetch(cache, key, identifier)
            return
        result = read_json(cache / "batch-results.json")
        if not (cache / "evidence-ready.json").exists():
            raise RuntimeError("Collect GitHub grading inputs with the evidence phase.")
        counts, graded = analyze(cache, key, rows, requests, schema, result)
        report = ROOT / "tasks/openai/replay/report.md"
        report.write_text(
            render(cache, rows, estimate, counts, graded, result["status"]),
            encoding="utf-8",
        )
        LOGGER.info(
            "report_written",
            parsed=counts["parsed"],
            raw_candidates=counts["raw_candidates"],
        )


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        # Provider response payloads and subprocess stderr can contain private evidence.
        if type(exc) is RuntimeError:
            LOGGER.error("replay_failed", reason=str(exc))
        else:
            LOGGER.error("replay_failed", error_type=type(exc).__name__)
        sys.exit(1)
