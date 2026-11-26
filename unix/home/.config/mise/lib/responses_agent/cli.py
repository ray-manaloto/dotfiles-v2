"""Run the local coding profile and emit a metadata-only envelope."""

from __future__ import annotations

import argparse
import asyncio
import logging.config
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic_ns

from agents.exceptions import MaxTurnsExceeded
from openai import APIConnectionError, APIStatusError
from pydantic import ValidationError

from dotfiles_py.generated.common.envelope_schema import (
    CommonErrorCode,
    CommonReturnCode,
    ReturnEntry,
)
from dotfiles_py.generated.responses_agent_schema import (
    CodingArtifacts,
    CodingError,
    CodingRequest,
    CodingResponse,
)
from dotfiles_py.logging import configure_logging, get_logger
from dotfiles_py.time_ns import now_ns_pair
from responses_agent.runner import (
    ENDPOINT,
    MAX_OUTPUT_TOKENS,
    MAX_RUN_OUTPUT_TOKENS,
    MAX_TURNS,
    MODEL,
    PLACEHOLDER,
    TIMEOUT_SECONDS,
    CredentialError,
    OutputBudgetError,
    UsageTracker,
    proxy_client,
    run,
)
from responses_agent.worktree import ScopeError, Worktree, bounded_text, open_worktree

MAX_PROMPT_BYTES = 8192
LOGGER = "responses_agent"


def read_prompt(path: Path) -> str:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ScopeError("The prompt must be a regular file.")
        prompt = bounded_text(stream, MAX_PROMPT_BYTES)
    if not prompt.strip():
        raise ScopeError("The prompt must contain text.")
    return prompt


def _configure_live_logging(worktree: Worktree) -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    directory = (state / "dotfiles-v2" / "responses-agent").resolve()
    if directory.is_relative_to(worktree.root):
        raise ScopeError(
            "Place the coding state directory outside the target worktree."
        )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Third-party diagnostic logs may carry request data or proxy credentials.
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": True,
            "handlers": {"discard": {"class": "logging.NullHandler"}},
            "root": {"handlers": ["discard"]},
        }
    )
    configure_logging(jsonl_path=directory / "usage.log.jsonl", logger_name=LOGGER)
    return directory


def _save_artifacts(
    directory: Path, worktree: Worktree, summary: str | None
) -> CodingArtifacts:
    checks = directory / "checks.txt"
    patch = directory / "changes.patch"
    checks.write_text(
        ("\n".join(worktree.results) or "No approved checks were run.").replace(
            PLACEHOLDER, "[redacted]"
        )
        + "\n",
        encoding="utf-8",
    )
    patch.write_bytes(worktree.patch())
    summary_path = None
    if summary is not None:
        output = directory / "summary.txt"
        output.write_text(summary.replace(PLACEHOLDER, "[redacted]"), encoding="utf-8")
        summary_path = str(output)
    return CodingArtifacts(patch=str(patch), results=str(checks), summary=summary_path)


def _provider_error(exc: APIStatusError) -> str:
    if exc.code in {"insufficient_quota", "billing_hard_limit_reached"}:
        return f"OpenAI Responses returned HTTP {exc.status_code} ({exc.code})."
    return f"OpenAI Responses returned HTTP {exc.status_code}."


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="api:code", allow_abbrev=False)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--files", required=True)
    parser.add_argument("--zizmor", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--credit-confirmed", action="store_true")
    args = parser.parse_args(argv)

    started = monotonic_ns()
    tracker = UsageTracker()
    error: CodingError | None = None
    attempted = False
    target_commit: str | None = None
    artifacts: CodingArtifacts | None = None
    try:
        request = CodingRequest(
            worktree=args.worktree,
            prompt_file=args.prompt_file,
            files=args.files.split(","),
            dry_run=args.dry_run,
            credit_confirmed=args.credit_confirmed,
        )
        with open_worktree(
            Path(request.worktree), request.files, args.zizmor
        ) as worktree:
            target_commit = worktree.commit
            prompt = read_prompt(Path(request.prompt_file))
            if not request.dry_run:
                if not request.credit_confirmed:
                    raise CredentialError(
                        "Verify the linked credit balance and billing controls before running."
                    )
                directory = Path(
                    mkdtemp(prefix="run-", dir=_configure_live_logging(worktree))
                )
                client = proxy_client(os.environ)
                attempted = True
                summary = None
                try:
                    summary = asyncio.run(run(worktree, prompt, client, tracker))
                finally:
                    artifacts = _save_artifacts(directory, worktree, summary)
    except CredentialError as exc:
        error = CodingError(
            code=CommonErrorCode.ERR_PRECONDITION_FAILED, message=str(exc)
        )
    except ScopeError as exc:
        error = CodingError(code=CommonErrorCode.ERR_INVALID_INPUT, message=str(exc))
    except ValidationError, OSError, subprocess.TimeoutExpired:
        error = CodingError(
            code=CommonErrorCode.ERR_INVALID_INPUT,
            message="Could not validate coding arguments, files, or local configuration.",
        )
    except TimeoutError, MaxTurnsExceeded:
        error = CodingError(
            code=CommonErrorCode.ERR_TIMEOUT,
            message="The coding run exceeded its wall-clock or turn cap.",
        )
    except OutputBudgetError as exc:
        error = CodingError(
            code=CommonErrorCode.ERR_SUBPROCESS_FAILED, message=str(exc)
        )
    except APIStatusError as exc:
        error = CodingError(
            code=CommonErrorCode.ERR_SUBPROCESS_FAILED, message=_provider_error(exc)
        )
    except APIConnectionError:
        error = CodingError(
            code=CommonErrorCode.ERR_SUBPROCESS_FAILED,
            message="Could not reach OpenAI Responses through the coding fnox proxy.",
        )
    except Exception:
        error = CodingError(
            code=CommonErrorCode.ERR_UNKNOWN, message="The coding SDK run failed."
        )

    elapsed_ms = (monotonic_ns() - started) // 1_000_000
    usage = tracker.snapshot()
    if attempted:
        get_logger(LOGGER).info(
            "coding_run", model=MODEL, elapsed_ms=elapsed_ms, usage=usage
        )
    timestamp, unix_ns = now_ns_pair()
    if error is not None:
        code = (
            CommonReturnCode.RET_PARTIAL if attempted else CommonReturnCode.RET_SKIPPED
        )
    else:
        code = CommonReturnCode.RET_DRY_RUN if args.dry_run else CommonReturnCode.RET_OK
    response = CodingResponse(
        timestamp=timestamp,
        timestamp_unix_ns=unix_ns,
        model=MODEL,
        endpoint=ENDPOINT,
        dry_run=args.dry_run,
        max_turns=MAX_TURNS,
        max_output_tokens_per_request=MAX_OUTPUT_TOKENS,
        max_output_tokens_per_run=MAX_RUN_OUTPUT_TOKENS,
        target_commit=target_commit,
        artifacts=artifacts,
        timeout_seconds=TIMEOUT_SECONDS,
        elapsed_ms=elapsed_ms,
        usage=usage,
        errors=[error] if error else [],
        returns=[ReturnEntry(code=code)],
    )
    sys.stdout.write(response.model_dump_json(exclude_none=False) + "\n")
    return 1 if error else 0
