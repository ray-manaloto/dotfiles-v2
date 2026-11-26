"""Conservatively reserve provider spend before any billable request."""

import json
import math

from inputs import read_json, write_json

from dotfiles_py.generated.openai_replay_schema import ReplaySpend

# USD per million tokens equals microdollars per token.
BATCH_INPUT = 1.00
BATCH_OUTPUT = 5.00
BATCH_CACHED = 0.05
DECISIONS_INPUT = 0.10
DECISIONS_CACHED = 0.01
DECISIONS_WRITE = 0.125
CAP = 15_000_000
MAX_GRADE_BYTES = 10_000
OVERHEAD_TOKENS = 2_048


def token_bound(body):
    return len(json.dumps(body, ensure_ascii=True).encode("utf-8")) + OVERHEAD_TOKENS


def upper_cost(input_tokens, output_tokens=0, *, batch=False):
    if batch:
        return math.ceil(
            1.2 * (input_tokens * BATCH_INPUT + output_tokens * BATCH_OUTPUT)
        )
    # Allows for cache writes and regional premiums on short requests.
    return math.ceil(input_tokens * 0.25)


def totals(cache):
    return [
        ReplaySpend.model_validate(read_json(path))
        for path in sorted(cache.glob("spend-*.json"))
    ]


def charge(entry):
    if entry.actual_microusd is None:
        return entry.reserved_microusd
    return entry.actual_microusd


def reserve(cache, request_id, amount, *, retry_reserved=False):
    path = cache / f"spend-{request_id}.json"
    if path.exists() and not retry_reserved:
        raise RuntimeError(
            "A request already has a reservation. Use --retry-reserved to charge another attempt."
        )
    attempt = 0
    while path.exists():
        attempt += 1
        path = cache / f"spend-{request_id}-retry-{attempt}.json"
    if sum(map(charge, totals(cache))) + amount > CAP:
        raise RuntimeError("Replay stopped at the $15 spend ceiling.")
    write_json(path, ReplaySpend(reserved_microusd=amount).model_dump())
    return path


def meter(path, usages, *, batch=False):
    in_tokens = cached = out_tokens = written = 0
    for usage in usages:
        if not isinstance(usage, dict) or type(usage.get("input_tokens")) is not int:
            raise RuntimeError(
                "Missing API usage. The full reservation remains charged."
            )
        if batch and type(usage.get("output_tokens")) is not int:
            raise RuntimeError(
                "Missing Batch output usage. The full reservation remains charged."
            )
        details = usage.get("input_tokens_details") or {}
        in_tokens += usage["input_tokens"]
        cached += details.get("cached_tokens") or 0
        written += details.get("cache_write_tokens") or 0
        out_tokens += usage.get("output_tokens") or 0
    if cached + written > in_tokens:
        raise RuntimeError("API token usage did not reconcile.")
    if batch:
        cost = (
            (in_tokens - cached - written) * BATCH_INPUT
            + cached * BATCH_CACHED
            + written * (BATCH_INPUT * 1.25)
            + out_tokens * BATCH_OUTPUT
        )
    else:
        cost = (
            (in_tokens - cached - written) * DECISIONS_INPUT
            + cached * DECISIONS_CACHED
            + written * DECISIONS_WRITE
        )
    reservation = ReplaySpend.model_validate(read_json(path))
    write_json(
        path,
        ReplaySpend(
            reserved_microusd=reservation.reserved_microusd,
            actual_microusd=math.ceil(cost),
            input_tokens=in_tokens,
            cached_tokens=cached,
            cache_write_tokens=written,
            output_tokens=out_tokens,
        ).model_dump(),
    )
