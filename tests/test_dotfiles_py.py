"""Tests for `dotfiles_py` shared core: 9-digit ns timestamps, typed envelopes, dual-sink structlog."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

import orjson
import pytest
from pydantic import ValidationError

from dotfiles_py import (
    NANOSECOND_TIMESTAMP_RE,
    build_common_error,
    build_common_return,
    build_envelope_metadata,
    configure_logging,
    format_ns_timestamp,
    get_logger,
    now_ns_pair,
)
from dotfiles_py.generated import (
    CommonErrorCode,
    CommonReturnCode,
    HarnessKind,
)
from herdr_roll.models import make_session_outcome


def test_nanosecond_timestamp_zero_padded_nine_digits() -> None:
    ts_zero = format_ns_timestamp(1_700_000_000_000_000_007, utc=True)
    assert ts_zero.endswith(".000000007+0000")
    assert NANOSECOND_TIMESTAMP_RE.fullmatch(ts_zero)

    ts_now, epoch_ns = now_ns_pair()
    assert epoch_ns > 0
    assert NANOSECOND_TIMESTAMP_RE.fullmatch(ts_now)

    with pytest.raises(ValueError):
        format_ns_timestamp(-1)


def test_generated_envelope_models_are_frozen_strict_and_forbid_extra() -> None:
    err = build_common_error(
        CommonErrorCode.ERR_TIMEOUT, message="timed out", detail={"s": 5}
    )
    ret = build_common_return(CommonReturnCode.RET_SKIPPED, message="skipped")
    meta = build_envelope_metadata(errors=[err], returns=[ret])

    assert NANOSECOND_TIMESTAMP_RE.fullmatch(meta.timestamp)
    assert meta.errors[0].code is CommonErrorCode.ERR_TIMEOUT
    assert meta.returns[0].code is CommonReturnCode.RET_SKIPPED

    with pytest.raises(ValidationError):
        meta.model_validate({**meta.model_dump(), "extra_key": 1})

    with pytest.raises(ValidationError):
        meta.model_validate({**meta.model_dump(), "timestamp": "2026-11-23T12:00:00Z"})

    with pytest.raises(ValidationError):
        meta.model_validate({**meta.model_dump(), "timestamp_unix_ns": True})

    with pytest.raises(ValidationError):
        meta.timestamp_unix_ns = 0  # ty: ignore[invalid-assignment]


def test_dual_sink_structlog_emits_console_and_orjson_jsonl_with_foreign_chain() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        jsonl_path = Path(tmp) / "logs" / "dual.jsonl"
        console_buf = io.StringIO()
        logger = configure_logging(
            jsonl_path=jsonl_path,
            console_stream=console_buf,
            colors=False,
            logger_name="dotfiles_py_test",
        )
        try:
            outcome = make_session_outcome(
                where="dotfiles-v2 / worker",
                tab_id="tb1",
                terminal="t1",
                kind=HarnessKind.CODEX,
                session_id="01a1f5ba-9341-7890-976e-75cc768f8dd5",
                result="failed-verify-timeout",
                notes=("verify deadline exceeded",),
            )
            log = get_logger("dotfiles_py_test")
            log.info("outcome_recorded", outcome=outcome, harness=HarnessKind.CODEX)

            # Foreign stdlib log also routes through shared_processors via foreign_pre_chain.
            logging.getLogger("dotfiles_py_test.foreign").warning("foreign_event")  # noqa: TID251
        finally:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

        console_out = console_buf.getvalue()
        assert "outcome_recorded" in console_out
        assert "foreign_event" in console_out
        assert "ERR_VERIFY_TIMEOUT" in console_out

        rows = [
            orjson.loads(line)
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 2

        first, second = rows
        assert first["event"] == "outcome_recorded"
        assert first["harness"] == "codex"
        assert first["outcome"]["errors"][0]["code"] == "ERR_VERIFY_TIMEOUT"
        assert first["outcome"]["returns"][0]["code"] == "RET_FAILED"
        for row in rows:
            assert "_record" not in row
            assert "_from_structlog" not in row
            assert NANOSECOND_TIMESTAMP_RE.fullmatch(row["timestamp"])
            assert isinstance(row["timestamp_unix_ns"], int)
        assert second["event"] == "foreign_event"


def test_datamodel_codegen_check_passes() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    codegen_bin = Path(sys.executable).with_name("datamodel-codegen")
    proc = subprocess.run(
        [str(codegen_bin), "--check"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
