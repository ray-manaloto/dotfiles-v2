"""Offline-safe collector for `env:stats`, `agy-sessions/*.json`, and Herdr sync state."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import orjson
import psutil

from dotfiles_py.generated import (
    AgySessionStatus,
    EnvHostMetrics,
    EnvStatsErrorCode,
    EnvStatsErrorEntry,
    EnvStatsReturnCode,
    EnvStatsReturnEntry,
    EnvStatsSnapshotResponse,
    EnvTargetName,
    EnvTargetState,
    HerdrModSyncState,
)
from dotfiles_py.logging import get_logger
from dotfiles_py.time_ns import now_ns_pair

logger = get_logger("env_stats.collector")

DEFAULT_TARGETS: tuple[EnvTargetName, ...] = (
    EnvTargetName.HOST,
    EnvTargetName.PC_HOST,
    EnvTargetName.PC,
    EnvTargetName.MAC,
    EnvTargetName.VM,
)


def resolve_cache_dir(cache_home: Path | None = None) -> Path:
    """Resolve `$XDG_CACHE_HOME` cleanly without falling back to empty env strings."""
    if cache_home is not None:
        return cache_home
    raw = os.environ.get("XDG_CACHE_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache"


def resolve_herdr_home(herdr_home: Path | None = None) -> Path:
    """Resolve `$HERDR_APP_HOME` or `~/.config/herdr-app`."""
    if herdr_home is not None:
        return herdr_home
    raw = os.environ.get("HERDR_APP_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "herdr-app"


def load_agy_sessions(
    sessions_dir: Path,
    *,
    errors: list[EnvStatsErrorEntry],
) -> list[AgySessionStatus]:
    """Read and validate `agy-sessions/*.json` snapshots sorted newest-first."""
    if not sessions_dir.is_dir():
        return []
    out: list[AgySessionStatus] = []
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            raw = orjson.loads(path.read_bytes())
        except (OSError, orjson.JSONDecodeError) as exc:
            logger.warning("agy_session_parse_failed", path=str(path), error=str(exc))
            errors.append(
                EnvStatsErrorEntry(
                    code=EnvStatsErrorCode.ERR_PARSE_FAILED,
                    message=f"Failed parsing {path.name}: {exc}",
                    detail={"path": path.name},
                )
            )
            continue
        if not isinstance(raw, dict):
            continue
        try:
            normalized = dict(raw)
            for float_key in ("used_percentage", "compaction_used_percentage"):
                if float_key in normalized and normalized[float_key] is not None:
                    normalized[float_key] = float(normalized[float_key])
            if (
                "total_cost_usd" in normalized
                and normalized["total_cost_usd"] is not None
            ):
                normalized["total_cost_usd"] = float(normalized["total_cost_usd"])
            out.append(AgySessionStatus.model_validate(normalized))
        except Exception as exc:
            logger.warning("agy_session_invalid", path=str(path), error=str(exc))
            errors.append(
                EnvStatsErrorEntry(
                    code=EnvStatsErrorCode.ERR_PARSE_FAILED,
                    message=f"Invalid schema in {path.name}: {exc}",
                    detail={"path": path.name},
                )
            )
    out.sort(key=lambda item: item.updated_at, reverse=True)
    return out


def collect_local_host_metrics() -> EnvHostMetrics:
    """Collect non-blocking local host load and memory metrics via `psutil`."""
    try:
        load_1m, load_5m, _ = os.getloadavg()
    except OSError:
        load_1m, load_5m = 0.0, 0.0
    cpus = psutil.cpu_count(logical=True) or 1
    vm = psutil.virtual_memory()
    cpu_pct = float(min(100.0, max(0.0, (load_1m / max(1, cpus)) * 100.0)))
    return EnvHostMetrics(
        target=EnvTargetName.HOST,
        status=EnvTargetState.READY,
        cpu_percent=round(cpu_pct, 2),
        load_1m=round(float(load_1m), 3),
        load_5m=round(float(load_5m), 3),
        cpu_count=int(cpus),
        mem_used_bytes=int(vm.used),
        mem_total_bytes=int(vm.total),
        mem_used_percent=round(float(vm.percent), 2),
        top_command=["mise", "run", "env:top", "host"],
        detail="local host via psutil + btm --basic",
    )


def inspect_herdr_sync_state(
    herdr_home: Path,
    *,
    realtime_space_id: str | None = None,
) -> HerdrModSyncState:
    """Inspect installed `usage-limits`, `env-stats`, and `main-stats.json` layout state."""
    mods_dir = herdr_home / "mods"
    ul_ok = (mods_dir / "usage-limits" / "main.js").is_file()
    es_ok = (mods_dir / "env-stats" / "main.js").is_file()
    layout_name = (
        "main-stats" if (herdr_home / "layouts" / "main-stats.json").is_file() else None
    )
    return HerdrModSyncState(
        usage_limits_installed=ul_ok,
        env_stats_installed=es_ok,
        layout_name=layout_name,
        realtime_stats_space_id=realtime_space_id,
    )


def collect_env_stats(
    *,
    cache_home: Path | None = None,
    herdr_home: Path | None = None,
    realtime_space_id: str | None = None,
    remote_states: dict[EnvTargetName, EnvTargetState] | None = None,
    host_metrics_fn: Callable[[], EnvHostMetrics] | None = None,
) -> EnvStatsSnapshotResponse:
    """Assemble typed `EnvStatsSnapshotResponse` for CLI and tests."""
    ts, ns = now_ns_pair()
    errors: list[EnvStatsErrorEntry] = []
    resolved_cache = resolve_cache_dir(cache_home)
    resolved_herdr = resolve_herdr_home(herdr_home)

    sessions = load_agy_sessions(
        resolved_cache / "usage-telemetry" / "agy-sessions",
        errors=errors,
    )

    host_entry = (host_metrics_fn or collect_local_host_metrics)()
    environments: list[EnvHostMetrics] = [host_entry]
    state_map: dict[EnvTargetName, EnvTargetState] = dict(remote_states or {})
    for target in DEFAULT_TARGETS:
        if target == EnvTargetName.HOST:
            continue
        detail = (
            "/usr/bin/btop via ssh pc"
            if target == EnvTargetName.PC_HOST
            else f"btm --basic via env:top {target.value}"
        )
        environments.append(
            EnvHostMetrics(
                target=target,
                status=state_map.get(target, EnvTargetState.UNKNOWN),
                top_command=["mise", "run", "env:top", target.value],
                detail=detail,
            )
        )

    herdr_state = inspect_herdr_sync_state(
        resolved_herdr,
        realtime_space_id=realtime_space_id,
    )
    ret_code = EnvStatsReturnCode.RET_PARTIAL if errors else EnvStatsReturnCode.RET_OK
    returns = [
        EnvStatsReturnEntry(
            code=ret_code,
            message=(
                f"Collected {len(environments)} environment(s) and {len(sessions)} active agy session(s)"
            ),
            detail={"environments": len(environments), "agy_sessions": len(sessions)},
        )
    ]
    rc = 1 if errors else 0
    logger.info(
        "env_stats_collected",
        environments=len(environments),
        agy_sessions=len(sessions),
        usage_limits=herdr_state.usage_limits_installed,
        env_stats=herdr_state.env_stats_installed,
        rc=rc,
    )
    return EnvStatsSnapshotResponse(
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=errors,
        returns=returns,
        rc=rc,
        environments=environments,
        agy_sessions=sessions,
        herdr=herdr_state,
    )
