"""Entry point for the merged PR review replay."""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import batch
import independent
import review
import triage
from filelock import FileLock
from grade import PayloadMismatch, grade, grade_context, production_scores
from inputs import (
    LOGGER,
    collect,
    read_json,
    refresh_visibility,
    require_private_opt_in,
    write_json,
)
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


def analyze(
    cache,
    key,
    rows,
    requests,
    schema,
    result,
    *,
    retry_reserved=False,
    regrade=False,
    allow_legacy=False,
):
    output, expected = batch_reviews(requests, result)
    if result["status"] == "completed" and output.keys() == expected:
        meter(
            batch.reservation_path(cache),
            [row["usage"] for row in output.values()],
            batch=True,
        )
    counts = Counter(missing_or_unmetered_batch=len(expected - output.keys()))
    graded = []
    records = []
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
        eligible_ids = {item["id"] for item in eligible}
        counts["changed_line_candidates"] += len(eligible)
        try:
            scores, accepted = production_scores(
                cache,
                key,
                row_id,
                eligible,
                patches,
                retry_reserved=retry_reserved,
                allow_legacy=allow_legacy,
            )
            counts["production_selected"] += len(accepted)
        except PayloadMismatch:
            raise
        except (
            review.ReviewError,
            triage.TriageError,
            RuntimeError,
            OSError,
            *review.MALFORMED_ERRORS,
        ):
            if regrade:
                raise RuntimeError(
                    "Re-grade requires cached production triage."
                ) from None
            counts["production_triage_unavailable"] += 1
            scores = {}
        prior = []
        for item in candidates:
            try:
                label, context = grade(
                    cache,
                    key,
                    row,
                    item,
                    prior,
                    retry_reserved=retry_reserved,
                    allow_legacy=allow_legacy,
                )
            except PayloadMismatch:
                raise
            except (
                review.ReviewError,
                RuntimeError,
                OSError,
                *review.MALFORMED_ERRORS,
            ):
                if regrade:
                    raise RuntimeError(
                        "Re-grade requires cached Luna grades."
                    ) from None
                counts["grader_unavailable"] += 1
                label, context = "insufficient-evidence", False
            score = scores.get(item["id"], (-1, -1))
            graded.append((label, *score, item["id"] in eligible_ids))
            if regrade:
                records.append(
                    {
                        "id": f"{row_id}-{item['id']}",
                        "luna": label,
                        "scores": score,
                        "finding": item,
                        "prior": list(prior),
                    }
                )
            prior.append(item["claim"][:80])
            counts["graded_with_follow_up_sources"] += context
            counts[f"raw_grade_{label}"] += 1
    return counts, graded, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=["prepare", "submit", "poll", "evidence", "finish", "regrade"]
    )
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument(
        "--visibility-only", action="store_true", help=argparse.SUPPRESS
    )
    visibility.add_argument(
        "--visibility-checked", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("--allow-private", action="store_true")
    parser.add_argument("--cached-only", action="store_true")
    parser.add_argument("--retry-reserved", action="store_true")
    args = parser.parse_args()
    if args.cached_only and args.phase not in {"finish", "regrade"}:
        parser.error("--cached-only requires finish or regrade.")
    if args.retry_reserved and (
        args.cached_only or args.phase not in {"submit", "finish", "regrade"}
    ):
        parser.error("--retry-reserved requires a live submit, finish, or regrade.")
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
        rows = collect(
            cache, cached_only=args.phase != "prepare", allow_private=args.allow_private
        )
        if args.cached_only:
            require_private_opt_in(rows, allow_private=args.allow_private)
        elif (
            args.phase in {"submit", "evidence", "finish", "regrade"}
            and not args.visibility_checked
        ):
            refresh_visibility(
                (row["repo"] for row in rows), allow_private=args.allow_private
            )
        if args.visibility_only:
            return
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
        key = None if args.cached_only else batch.api_key()
        if args.phase == "submit":
            batch.submit(
                cache, key, requests, estimate, retry_reserved=args.retry_reserved
            )
            return
        identifier = batch.existing_batch(cache, requests)
        if args.phase == "poll":
            batch.fetch(cache, key, identifier)
            return
        result = read_json(cache / "batch-results.json")
        if not (cache / "evidence-ready.json").exists():
            raise RuntimeError("Collect GitHub grading inputs with the evidence phase.")
        regrade = args.phase == "regrade"
        counts, graded, records = analyze(
            cache,
            None if regrade else key,
            rows,
            requests,
            schema,
            result,
            retry_reserved=args.retry_reserved,
            regrade=regrade,
            allow_legacy=args.cached_only,
        )
        if regrade:
            independent.regrade(cache, key, records, retry_reserved=args.retry_reserved)
            return
        report = ROOT / "tasks/openai/replay/report.md"
        report.write_text(
            render(
                cache,
                rows,
                estimate,
                counts,
                graded,
                result["status"],
                report.read_text(encoding="utf-8") if report.exists() else "",
            ),
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
    except (OSError, RuntimeError, review.ReviewError, *review.MALFORMED_ERRORS) as exc:
        # Provider response payloads and subprocess stderr can contain private evidence.
        if type(exc) in (RuntimeError, PayloadMismatch):
            LOGGER.error("replay_failed", reason=str(exc))
        else:
            LOGGER.error("replay_failed", error_type=type(exc).__name__)
        sys.exit(1)
