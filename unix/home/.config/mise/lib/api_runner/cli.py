"""`mise run api:review`: read-only `claude -p` review billed only to linked Console API credits."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import cyclopts
import orjson
from pydantic import ValidationError

from api_runner.runner import (
    RunnerError,
    RunSpec,
    build_argv,
    resolve_claude_bin,
    run,
    validate_spec,
)
from dotfiles_py.generated.api_runner_schema import (
    ApiRunnerEffort,
    ApiRunnerErrorCode,
    ApiRunnerErrorEntry,
    ApiRunnerModel,
    ApiRunnerResponse,
)
from dotfiles_py.generated.common.envelope_schema import CommonReturnCode, ReturnEntry
from dotfiles_py.logging import configure_logging, get_logger
from dotfiles_py.time_ns import now_ns_pair


def resolve_state_dir(override: Path | None = None) -> Path:
    if override is not None:
        return override
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(os.path.expanduser(xdg_state))
        if xdg_state
        else Path.home() / ".local" / "state"
    )
    return base / "dotfiles-v2" / "api-runner"


app = cyclopts.App(name="api-review", version_flags=())


@app.default
def review(
    *,
    prompt_file: Path,
    cwd: Path = Path("."),
    model: ApiRunnerModel = ApiRunnerModel.SONNET,
    effort: ApiRunnerEffort = ApiRunnerEffort.MEDIUM,
    max_turns: int = 20,
    max_budget_usd: float = 2.0,
    timeout_sec: int = 900,
    out: Path | None = None,
    state_dir: Path | None = None,
    dry_run: bool = False,
    json_out: Annotated[bool, cyclopts.Parameter(name="--json", negative="")] = False,
) -> int:
    """Run one isolated read-only review; dry-run needs no credentials."""
    state = resolve_state_dir(state_dir)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    configure_logging(
        jsonl_path=state / "usage.log.jsonl",
        level=logging.WARNING,
        file_level=logging.INFO,
    )
    log = get_logger("api_runner")

    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        if not dry_run:
            sys.stderr.write("api:review: claude CLI not found\n")
            return 2
        claude_bin = "claude"
    try:
        spec = RunSpec(
            model=model,
            effort=effort,
            cwd=str(cwd.resolve()),
            max_turns=max_turns,
            max_budget_usd=float(max_budget_usd),
            timeout_sec=timeout_sec,
            config_dir=str(state / "claude-config"),
            claude_bin=claude_bin,
        )
    except ValidationError as exc:
        sys.stderr.write(f"api:review: invalid input: {exc.errors()[0]['msg']}\n")
        return 2
    timestamp, ns = now_ns_pair()
    base = ApiRunnerResponse(
        mode="review",
        model=model,
        cwd=spec.cwd,
        dry_run=True,
        argv=build_argv(spec),
        timestamp=timestamp,
        timestamp_unix_ns=ns,
        errors=[],
        returns=[ReturnEntry(code=CommonReturnCode.RET_DRY_RUN)],
    )
    try:
        validate_spec(spec)
        prompt = prompt_file.read_text()
        if not prompt.strip():
            raise ValueError("prompt file is empty")
    except (RunnerError, OSError, ValueError) as exc:
        entry = exc.entry if isinstance(exc, RunnerError) else None
        sys.stderr.write(f"api:review: invalid input: {exc}\n")
        if entry is not None:
            log.warning("api_review_rejected", error=entry)
        return 2

    if dry_run:
        response = base
    else:
        outcome = run(spec, prompt, os.environ)
        result_path: str | None = None
        guard_errors = list(outcome.guard_errors)
        if outcome.result_text is not None and not guard_errors:
            target = out or state / f"review-{ns}.md"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(outcome.result_text)
                result_path = str(target)
            except OSError as exc:
                guard_errors.append(
                    ApiRunnerErrorEntry(
                        code=ApiRunnerErrorCode.ERR_COMMAND_FAILED,
                        message=f"review billed but not saved: {exc}",
                    )
                )
        response = base.model_copy(
            update={
                "dry_run": False,
                "api_key_source": outcome.api_key_source,
                "tools": outcome.tools,
                "usage": outcome.usage,
                "result_path": result_path,
                "errors": guard_errors,
                "returns": [
                    ReturnEntry(
                        code=CommonReturnCode.RET_PARTIAL
                        if guard_errors
                        else CommonReturnCode.RET_OK
                    )
                ],
            }
        )
    # Usage metadata only: never the prompt, the key, or the review text.
    log.info("api_review_finished", response=response)
    if json_out:
        sys.stdout.write(orjson.dumps(response.model_dump(mode="json")).decode() + "\n")
    else:
        usage = response.usage
        cost = (
            f"${usage.total_cost_usd:.4f} turns={usage.num_turns}"
            if usage
            else "no usage"
        )
        status = (
            "DRY_RUN" if response.dry_run else ("FAILED" if response.errors else "OK")
        )
        sys.stdout.write(
            f"api:review {status} model={model.value} {cost} result={response.result_path}\n"
        )
        for err in response.errors:
            sys.stdout.write(f"  {err.code.value}: {err.message}\n")
    return 1 if response.errors else 0


def main(argv: Sequence[str] | None = None) -> int:
    return app(argv, result_action="return_value")
