"""Core `codex_trust` fingerprinting, trust audit/repair, and pinned plugin updates."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import orjson

from codex_trust.rpc import (
    CodexAppServerClient,
    CodexRpcError,
    parse_semver,
    resolve_codex_binary,
    semver_ge,
)
from dotfiles_py.generated.codex_trust_schema import (
    CodexHookFinding,
    CodexHookStatus,
    CodexHookSurfaceFile,
    CodexTrustCommand,
    CodexTrustErrorCode,
    CodexTrustErrorEntry,
    CodexTrustResponse,
    CodexTrustReturnCode,
    CodexTrustReturnEntry,
    HookSurfaceChangeKind,
)
from dotfiles_py.logging import get_logger
from dotfiles_py.time_ns import now_ns_pair

_LOG = get_logger(__name__)

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
_SKIP_BUNDLE_NAMES = frozenset({"__pycache__", ".venv"})
_CATALOG_PATHS = (
    ".agents/plugins/marketplace.json",
    ".agents/plugins/api_marketplace.json",
    ".claude-plugin/marketplace.json",
    ".cursor-plugin/marketplace.json",
)
_PLUGIN_ROOT_VAR_RE = re.compile(
    r"(?:\$\{?(?:PLUGIN_ROOT|CLAUDE_PLUGIN_ROOT)(?::-[^}]*)?\}?/([^\s\"']+))"
)


def _decl_hash_from_current(current_hash: str) -> str:
    return current_hash.partition("/")[0].strip()


def _validate_ident(value: str, label: str) -> None:
    if not value or value in {".", ".."} or not _SAFE_IDENT_RE.fullmatch(value):
        raise CodexRpcError(
            f"invalid {label}: {value!r}",
            code="ERR_INVALID_INPUT",
        )


def _validate_version(raw_ver: str) -> None:
    if (
        not raw_ver
        or raw_ver in {".", ".."}
        or raw_ver.startswith(".")
        or not _SAFE_VERSION_RE.fullmatch(raw_ver)
    ):
        raise CodexRpcError(
            f"invalid plugin.json version: {raw_ver!r}",
            code="ERR_INVALID_INPUT",
        )


def _version_sort_key(ver: str) -> tuple[int, tuple[int, int, int], str]:
    parsed = parse_semver(ver)
    if ver != "local" and parsed is not None:
        return (1, parsed, ver)
    return (0, (0, 0, 0), ver)


def default_codex_home() -> Path:
    """Resolve active CODEX_HOME (defaults to `~/.codex`)."""
    raw = os.environ.get("CODEX_HOME", "").strip()
    return Path(os.path.expanduser(raw or "~/.codex")).resolve(strict=False)


def plugin_content_hash(root: Path) -> str:
    """Compute exact Codex `codex-plugin-bundle-v1` whole-bundle SHA-256 digest."""
    if root.is_symlink():
        raise CodexRpcError(f"plugin root must not be a symlink: {root}")
    resolved = root.resolve(strict=False)
    if resolved.is_symlink() or not resolved.is_dir():
        raise CodexRpcError(f"plugin root is not a directory: {root}")
    hasher = hashlib.sha256()
    hasher.update(b"codex-plugin-bundle-v1")
    pending: list[Path] = [resolved]
    while pending:
        path = pending.pop()
        rel_bytes = b"" if path == resolved else os.fsencode(path.relative_to(resolved))
        if path != resolved and (
            path.name in _SKIP_BUNDLE_NAMES or path.name.endswith((".pyc", ".egg-info"))
        ):
            continue
        hasher.update(len(rel_bytes).to_bytes(8, "little"))
        hasher.update(rel_bytes)
        if path.is_symlink():
            raise CodexRpcError(f"unsupported symlink in plugin bundle: {path}")
        if path.is_dir():
            hasher.update(b"directory")
            entries = sorted(path.iterdir(), key=lambda child: os.fsencode(child.name))
            pending.extend(reversed(entries))
        elif path.is_file():
            data = path.read_bytes()
            hasher.update(b"file")
            hasher.update(len(data).to_bytes(8, "little"))
            hasher.update(data)
        else:
            raise CodexRpcError(f"unsupported plugin entry type: {path}")
    return f"sha256:{hasher.hexdigest()}"


def legacy_trust_path(user_config_path: Path, plugin_id: str) -> Path:
    """Compute Codex `~/.codex/.hook-trust/<sha256>` path for `(config.toml, plugin_id)`."""
    identity = hashlib.sha256()
    for part in (os.fsencode(user_config_path), os.fsencode(plugin_id)):
        identity.update(len(part).to_bytes(8, "little"))
        identity.update(part)
    return user_config_path.parent / ".hook-trust" / identity.hexdigest()


def _infer_plugin_root(
    source_path: str, plugin_id: str, codex_home: Path
) -> Path | None:
    p = Path(source_path)
    plugin, sep, mkt = plugin_id.partition("@")
    if sep:
        cache_base = codex_home / "plugins" / "cache" / mkt / plugin
        try:
            rel = p.resolve(strict=False).relative_to(cache_base.resolve(strict=False))
            if rel.parts:
                return cache_base / rel.parts[0]
        except ValueError:
            pass
    for parent in p.parents:
        if (parent / ".codex-plugin" / "plugin.json").is_file() or (
            parent / ".claude-plugin" / "plugin.json"
        ).is_file():
            return parent
    return p.parent.parent if p.parent.name == "hooks" else None


def _bundle_hash_from_current(
    current_hash: str, plugin_root: Path | None
) -> str | None:
    if "/" in current_hash:
        tail = current_hash.rsplit("/", 1)[-1]
        if tail.startswith("sha256:"):
            return tail
    if plugin_root is not None and plugin_root.is_dir():
        try:
            return plugin_content_hash(plugin_root)
        except CodexRpcError:
            return None
    return None


def _read_codex_config(config_path: Path) -> tuple[dict[str, Any], str]:
    if not config_path.is_file():
        return {}, ""
    raw = config_path.read_text(encoding="utf-8")
    return tomllib.loads(raw), raw


def _write_atomic_mode_0600(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _toml_key_quote(key: str) -> str:
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_key_literal_quote(key: str) -> str:
    return f"'{key}'"


def _toml_section_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml_key_quote(key)


def _toml_key_alts_regex(key: str) -> str:
    alts = [re.escape(_toml_key_quote(key))]
    if "'" not in key and "\n" not in key:
        alts.append(re.escape(_toml_key_literal_quote(key)))
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        alts.append(re.escape(key))
    return "(?:" + "|".join(dict.fromkeys(alts)) + ")"


def _toml_str_quote(val: str) -> str:
    escaped = val.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _upsert_kv_preserving_comments(body: str, key: str, value_toml: str) -> str:
    pat = re.compile(
        rf"(?m)^([ \t]*{re.escape(key)}[ \t]*=[ \t]*)([^#\r\n]*?)([ \t]*(?:#[^\r\n]*)?)$"
    )
    if pat.search(body):
        return pat.sub(lambda m: f"{m.group(1)}{value_toml}{m.group(3)}", body, count=1)
    insert = f"{key} = {value_toml}\n"
    if body.strip() == "":
        return insert + body
    # Insert right after initial property lines, before trailing blank/comment block
    lines = body.splitlines(keepends=True)
    idx = len(lines)
    while idx > 0 and (
        lines[idx - 1].strip() == "" or lines[idx - 1].lstrip(" \t").startswith("#")
    ):
        idx -= 1
    return "".join(lines[:idx]) + insert + "".join(lines[idx:])


def _upsert_toml_trust_and_orphans(
    config_path: Path,
    *,
    upserts: dict[str, tuple[str, str | None]],
    prune_keys: set[str],
    marketplace_ref_update: tuple[str, str] | None = None,
) -> None:
    """Fallback atomic mode-0600 TOML editor when `config/batchWrite` is unavailable or pruning keys."""
    _, raw = _read_codex_config(config_path)
    text = raw

    if marketplace_ref_update is not None:
        mkt, new_sha = marketplace_ref_update
        mkt_alts = _toml_key_alts_regex(mkt)
        sec_pat = re.compile(
            rf"(?ms)^([ \t]*\[marketplaces\.{mkt_alts}\][ \t]*(?:#[^\n]*)?\r?\n)(.*?)(?=^[ \t]*\[|\Z)"
        )
        m = sec_pat.search(text)
        if m:
            body = m.group(2)
            body = re.sub(r"(?m)^[ \t]*ref_type[ \t]*=[^\n]*\r?\n?", "", body)
            body = _upsert_kv_preserving_comments(body, "ref", _toml_str_quote(new_sha))
            text = text[: m.start()] + m.group(1) + body + text[m.end() :]
        else:
            mkt_sec = _toml_section_key(mkt)
            sep = "\n\n" if text.strip() else ""
            text = (
                text.rstrip()
                + f"{sep}[marketplaces.{mkt_sec}]\nref = {_toml_str_quote(new_sha)}\n"
            )

    for pkey in sorted(prune_keys):
        key_alts = _toml_key_alts_regex(pkey)
        block_pat = re.compile(
            rf"(?ms)^[ \t]*\[hooks\.state\.{key_alts}\][ \t]*(?:#[^\n]*)?\r?\n.*?(?=^[ \t]*(?:\[|#)|\Z)"
        )
        text = block_pat.sub("", text)
        inline_pat = re.compile(
            rf"(?m)^[ \t]*{key_alts}[ \t]*=[ \t]*\{{[^\n]*\}}[ \t]*(?:#[^\n]*)?\r?\n?"
        )
        text = inline_pat.sub("", text)

    for ukey, (t_hash, c_hash) in sorted(upserts.items()):
        qkey_raw = _toml_key_quote(ukey)
        key_alts = _toml_key_alts_regex(ukey)
        block_pat = re.compile(
            rf"(?ms)^([ \t]*\[hooks\.state\.{key_alts}\][ \t]*(?:#[^\n]*)?\r?\n)(.*?)(?=^[ \t]*\[|\Z)"
        )
        m = block_pat.search(text)
        if m:
            body = m.group(2)
            body = _upsert_kv_preserving_comments(
                body, "trusted_hash", _toml_str_quote(t_hash)
            )
            if c_hash:
                body = _upsert_kv_preserving_comments(
                    body, "trusted_content_hash", _toml_str_quote(c_hash)
                )
            text = text[: m.start()] + m.group(1) + body + text[m.end() :]
        else:
            inline_pat = re.compile(
                rf"(?m)^[ \t]*{key_alts}[ \t]*=[ \t]*\{{[^\n]*\}}[ \t]*(?:#[^\n]*)?\r?\n?"
            )
            if inline_pat.search(text):
                text = inline_pat.sub("", text)
            lines = [f"trusted_hash = {_toml_str_quote(t_hash)}"]
            if c_hash:
                lines.append(f"trusted_content_hash = {_toml_str_quote(c_hash)}")
            block_text = f"[hooks.state.{qkey_raw}]\n" + "\n".join(lines) + "\n"
            sep = "\n\n" if text.strip() else ""
            text = text.rstrip() + sep + block_text

    # Collapse excessive blank lines cleanly and verify TOML parses
    text = re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"
    tomllib.loads(text)
    _write_atomic_mode_0600(config_path, text)


def _apply_hook_trust_edits(
    client: Any,
    config_path: Path,
    upserts: dict[str, tuple[str, str | None]],
    prune_keys: set[str],
    *,
    marketplace_ref_update: tuple[str, str] | None = None,
) -> None:
    edits: list[dict[str, Any]] = []
    if marketplace_ref_update is not None:
        mkt, new_sha = marketplace_ref_update
        mkt_kp = (
            mkt
            if re.fullmatch(r"[A-Za-z0-9_-]+", mkt)
            else orjson.dumps(mkt).decode("utf-8")
        )
        edits.append(
            {
                "keyPath": f"marketplaces.{mkt_kp}.ref",
                "value": new_sha,
                "mergeStrategy": "upsert",
            }
        )
    for key, (t_hash, c_hash) in sorted(upserts.items()):
        qkey = orjson.dumps(key).decode("utf-8")
        edits.append(
            {
                "keyPath": f"hooks.state.{qkey}.trusted_hash",
                "value": t_hash,
                "mergeStrategy": "upsert",
            }
        )
        if c_hash:
            edits.append(
                {
                    "keyPath": f"hooks.state.{qkey}.trusted_content_hash",
                    "value": c_hash,
                    "mergeStrategy": "upsert",
                }
            )

    rpc_ok = False
    if edits and hasattr(client, "config_batch_write"):
        try:
            client.config_batch_write(
                edits, file_path=config_path, reload_user_config=True
            )
            rpc_ok = True
        except CodexRpcError as exc:
            _LOG.warning(
                "config/batchWrite fallback to atomic TOML edit", error=str(exc)
            )

    if prune_keys or not rpc_ok:
        # Either orphan table removal is needed or config/batchWrite fell back
        _upsert_toml_trust_and_orphans(
            config_path,
            upserts={} if rpc_ok else upserts,
            prune_keys=prune_keys,
            marketplace_ref_update=None if rpc_ok else marketplace_ref_update,
        )


def _is_plugin_state_key(key: str) -> tuple[bool, str | None]:
    prefix, sep, _ = key.strip().partition(":")
    if sep and "@" in prefix and not prefix.startswith("/"):
        return True, prefix
    return False, None


def inspect_hook_trust(
    *,
    cwd: Path | None = None,
    codex_home: Path | None = None,
    client: Any = None,
) -> CodexTrustResponse:
    """Audit enabled user hooks and plugin trust via native `hooks/list` RPC."""
    return _run_hook_trust(
        repair=False,
        cwd=cwd or Path.cwd(),
        codex_home=codex_home,
        client=client,
    )


def repair_hook_trust(
    *,
    cwd: Path | None = None,
    codex_home: Path | None = None,
    client: Any = None,
) -> CodexTrustResponse:
    """Repair modified/untrusted/unmigrated plugin hook states and prune orphaned entries."""
    return _run_hook_trust(
        repair=True,
        cwd=cwd or Path.cwd(),
        codex_home=codex_home,
        client=client,
    )


def _run_hook_trust(
    *,
    repair: bool,
    cwd: Path,
    codex_home: Path | None,
    client: Any,
) -> CodexTrustResponse:
    ts, ns = now_ns_pair()
    resolved_home = (codex_home or default_codex_home()).resolve(strict=False)
    resolved_cwd = cwd.resolve(strict=False)
    config_path = resolved_home / "config.toml"

    errors: list[CodexTrustErrorEntry] = []
    returns: list[CodexTrustReturnEntry] = []
    findings: list[CodexHookFinding] = []
    orphaned_keys: list[str] = []
    unmigrated_keys: list[str] = []
    repaired_keys: list[str] = []
    pruned_orphans: list[str] = []
    legacy_baselines_pruned: list[str] = []

    try:
        rpc_client = client or CodexAppServerClient(codex_home=resolved_home)
        rpc_res = rpc_client.hooks_list(resolved_cwd)
    except CodexRpcError as exc:
        err_code = (
            CodexTrustErrorCode.ERR_UNSUPPORTED_CODEX_VERSION
            if exc.code == "ERR_UNSUPPORTED_CODEX_VERSION"
            else CodexTrustErrorCode.ERR_RPC_FAILED
        )
        return CodexTrustResponse(
            command=CodexTrustCommand.HOOK_TRUST,
            rc=1,
            codex_version=getattr(locals().get("rpc_client"), "version", None),
            codex_home=str(resolved_home),
            cwd=str(resolved_cwd),
            applied=False,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[CodexTrustErrorEntry(code=err_code, message=str(exc))],
            returns=[
                CodexTrustReturnEntry(
                    code=CodexTrustReturnCode.RET_FAILED, message=str(exc)
                )
            ],
        )

    codex_ver = getattr(rpc_client, "version", None)
    if codex_ver is not None and not semver_ge(codex_ver):
        return CodexTrustResponse(
            command=CodexTrustCommand.HOOK_TRUST,
            rc=1,
            codex_version=codex_ver,
            codex_home=str(resolved_home),
            cwd=str(resolved_cwd),
            applied=False,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                CodexTrustErrorEntry(
                    code=CodexTrustErrorCode.ERR_UNSUPPORTED_CODEX_VERSION,
                    message=f"Codex version {codex_ver} < 0.171.0",
                )
            ],
            returns=[CodexTrustReturnEntry(code=CodexTrustReturnCode.RET_FAILED)],
        )

    cfg, _ = _read_codex_config(config_path)
    state_tbl = (cfg.get("hooks") or {}).get("state")
    if not isinstance(state_tbl, dict):
        state_tbl = {}

    data_rows = rpc_res.get("data") or []
    scope = data_rows[0] if isinstance(data_rows, list) and data_rows else {}
    live_hooks = scope.get("hooks") if isinstance(scope, dict) else []
    if not isinstance(live_hooks, list):
        live_hooks = []

    live_plugin_keys: set[str] = set()
    upserts: dict[str, tuple[str, str | None]] = {}
    legacy_paths_to_clean: list[Path] = []

    for row in live_hooks:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        key = str(row.get("key") or "")
        if not key:
            continue
        source_path = str(row.get("sourcePath") or "")
        trust_status = str(row.get("trustStatus") or "untrusted").lower()
        current_hash = str(row.get("currentHash") or "")
        if (
            source == "user"
            and not repair
            and row.get("enabled")
            and trust_status in {"modified", "untrusted"}
        ):
            findings.append(
                CodexHookFinding(
                    key=key,
                    event_name=row.get("eventName"),
                    source_path=source_path or None,
                    status=CodexHookStatus(trust_status),
                    issue="review the user hook source and re-approve it in Codex /hooks",
                    current_hash=current_hash or None,
                )
            )
        if source != "plugin":
            continue
        live_plugin_keys.add(key)
        plugin_id = str(row.get("pluginId") or "")

        raw_entry = state_tbl.get(key)
        state_entry: dict[str, Any] = raw_entry if isinstance(raw_entry, dict) else {}
        t_hash = state_entry.get("trusted_hash")
        c_hash = state_entry.get("trusted_content_hash")
        plugin_root = _infer_plugin_root(source_path, plugin_id, resolved_home)
        expected_bundle = _bundle_hash_from_current(current_hash, plugin_root)

        needs_repair = False
        if trust_status in {"modified", "untrusted"}:
            status_enum = (
                CodexHookStatus.MODIFIED
                if trust_status == "modified"
                else CodexHookStatus.UNTRUSTED
            )
            findings.append(
                CodexHookFinding(
                    key=key,
                    event_name=row.get("eventName"),
                    plugin_id=plugin_id or None,
                    source_path=source_path or None,
                    status=status_enum,
                    issue=f"hook trustStatus is {trust_status}",
                    current_hash=current_hash or None,
                    expected_bundle_hash=expected_bundle,
                    trusted_hash=str(t_hash) if t_hash is not None else None,
                    trusted_content_hash=str(c_hash) if c_hash is not None else None,
                )
            )
            needs_repair = True
        elif trust_status == "trusted" and c_hash is None:
            unmigrated_keys.append(key)
            findings.append(
                CodexHookFinding(
                    key=key,
                    event_name=row.get("eventName"),
                    plugin_id=plugin_id or None,
                    source_path=source_path or None,
                    status=CodexHookStatus.UNMIGRATED,
                    issue="missing trusted_content_hash in [hooks.state]",
                    current_hash=current_hash or None,
                    expected_bundle_hash=expected_bundle,
                    trusted_hash=str(t_hash) if t_hash is not None else None,
                    trusted_content_hash=None,
                )
            )
            needs_repair = True

        if plugin_id and expected_bundle:
            leg = legacy_trust_path(config_path, plugin_id)
            if leg.is_file():
                try:
                    baseline = leg.read_text(encoding="utf-8").strip()
                except OSError:
                    baseline = ""
                if baseline and baseline != expected_bundle:
                    legacy_paths_to_clean.append(leg)

        if needs_repair and current_hash:
            upserts[key] = (_decl_hash_from_current(current_hash), expected_bundle)

    # Detect orphaned plugin keys in [hooks.state.*] absent from live hooks/list
    for skey, sval in sorted(state_tbl.items()):
        is_plugin, pid = _is_plugin_state_key(skey)
        if not is_plugin or skey in live_plugin_keys:
            continue
        orphaned_keys.append(skey)
        s_dict = sval if isinstance(sval, dict) else {}
        findings.append(
            CodexHookFinding(
                key=skey,
                plugin_id=pid,
                status=CodexHookStatus.ORPHANED,
                issue="stale plugin [hooks.state] entry no longer discovered by hooks/list",
                trusted_hash=str(s_dict["trusted_hash"])
                if s_dict.get("trusted_hash")
                else None,
                trusted_content_hash=(
                    str(s_dict["trusted_content_hash"])
                    if s_dict.get("trusted_content_hash")
                    else None
                ),
            )
        )

    legacy_drift_paths = sorted(set(legacy_paths_to_clean))
    for leg_path in legacy_drift_paths:
        if repair:
            leg_path.unlink(missing_ok=True)
            legacy_baselines_pruned.append(str(leg_path))

    if any(
        f.status in {CodexHookStatus.MODIFIED, CodexHookStatus.UNTRUSTED}
        for f in findings
    ):
        errors.append(
            CodexTrustErrorEntry(
                code=CodexTrustErrorCode.ERR_UNTRUSTED_OR_MODIFIED,
                message="One or more Codex hooks are modified or untrusted.",
            )
        )
    if unmigrated_keys:
        errors.append(
            CodexTrustErrorEntry(
                code=CodexTrustErrorCode.ERR_UNMIGRATED_STATE,
                message=f"{len(unmigrated_keys)} plugin hook state entry(s) lack trusted_content_hash.",
            )
        )
    if orphaned_keys:
        errors.append(
            CodexTrustErrorEntry(
                code=CodexTrustErrorCode.ERR_ORPHANED_STATE,
                message=f"{len(orphaned_keys)} orphaned plugin hook state entry(s) present.",
            )
        )
    if legacy_drift_paths and not repair:
        errors.append(
            CodexTrustErrorEntry(
                code=CodexTrustErrorCode.ERR_LEGACY_BASELINE_DRIFT,
                message=f"{len(legacy_drift_paths)} legacy .hook-trust baseline file(s) drift from current bundle hash.",
            )
        )

    applied = False
    if repair and (upserts or orphaned_keys or legacy_baselines_pruned):
        try:
            if upserts or orphaned_keys:
                _apply_hook_trust_edits(
                    rpc_client,
                    config_path,
                    upserts,
                    set(orphaned_keys),
                )
            repaired_keys = sorted(upserts)
            pruned_orphans = sorted(orphaned_keys)
            applied = True
            errors = []
            returns = [
                CodexTrustReturnEntry(
                    code=CodexTrustReturnCode.RET_REPAIRED,
                    message=f"Repaired {len(repaired_keys)} hook(s), pruned {len(pruned_orphans)} orphan(s), and cleaned {len(legacy_baselines_pruned)} legacy baseline(s).",
                )
            ]
        except (CodexRpcError, OSError, ValueError) as exc:
            return CodexTrustResponse(
                command=CodexTrustCommand.HOOK_TRUST,
                rc=1,
                codex_version=codex_ver,
                codex_home=str(resolved_home),
                cwd=str(resolved_cwd),
                findings=findings,
                orphaned_keys=orphaned_keys,
                unmigrated_keys=unmigrated_keys,
                legacy_baselines_pruned=legacy_baselines_pruned,
                applied=False,
                timestamp=ts,
                timestamp_unix_ns=ns,
                errors=[
                    CodexTrustErrorEntry(
                        code=CodexTrustErrorCode.ERR_IO_FAILURE, message=str(exc)
                    )
                ],
                returns=[
                    CodexTrustReturnEntry(
                        code=CodexTrustReturnCode.RET_FAILED, message=str(exc)
                    )
                ],
            )
    elif repair:
        returns = [
            CodexTrustReturnEntry(
                code=CodexTrustReturnCode.RET_NO_CHANGES,
                message="All plugin hooks are already trusted and migrated.",
            )
        ]
    elif findings or legacy_drift_paths:
        returns = [
            CodexTrustReturnEntry(
                code=CodexTrustReturnCode.RET_FAILED,
                message=f"Found {len(findings)} hook issue(s) and {len(legacy_drift_paths)} legacy baseline drift(s).",
            )
        ]
    else:
        returns = [
            CodexTrustReturnEntry(
                code=CodexTrustReturnCode.RET_OK,
                message="Enabled user hooks and plugin hook trust passed.",
            )
        ]

    rc = 0 if ((not findings and not legacy_drift_paths) or applied) else 1
    return CodexTrustResponse(
        command=CodexTrustCommand.HOOK_TRUST,
        rc=rc,
        codex_version=codex_ver,
        codex_home=str(resolved_home),
        cwd=str(resolved_cwd),
        findings=findings,
        orphaned_keys=orphaned_keys,
        unmigrated_keys=unmigrated_keys,
        repaired_keys=repaired_keys,
        pruned_orphans=pruned_orphans,
        legacy_baselines_pruned=legacy_baselines_pruned,
        applied=applied,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=errors,
        returns=returns,
    )


def _normalize_plugin_manifest(raw_bytes: bytes) -> bytes:
    """Strip ephemeral `version` from `.codex-plugin/plugin.json` when checking hook surface."""
    try:
        parsed = orjson.loads(raw_bytes)
        if isinstance(parsed, dict):
            parsed = {k: v for k, v in parsed.items() if k != "version"}
            return (
                orjson.dumps(parsed, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2)
                + b"\n"
            )
    except ValueError:
        pass
    return raw_bytes


def _is_safe_bundle_entry(path: Path, root: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        resolved = path.resolve(strict=False)
    except OSError, RuntimeError:
        return False
    return resolved.is_relative_to(root)


def collect_hook_surface(plugin_root: Path) -> dict[str, bytes]:
    """Collect normalized hook-surface files (`hooks/**`, normalized `plugin.json`, referenced scripts)."""
    if plugin_root.is_symlink():
        raise CodexRpcError(f"plugin root must not be a symlink: {plugin_root}")
    surface: dict[str, bytes] = {}
    root = plugin_root.resolve(strict=False)

    plugin_dir = root / ".codex-plugin"
    plugin_manifest = plugin_dir / "plugin.json"
    if (
        not plugin_dir.is_symlink()
        and _is_safe_bundle_entry(plugin_manifest, root)
        and plugin_manifest.is_file()
    ):
        surface[".codex-plugin/plugin.json"] = _normalize_plugin_manifest(
            plugin_manifest.read_bytes()
        )

    hooks_dir = root / "hooks"
    referenced_rel_paths: set[str] = set()
    if _is_safe_bundle_entry(hooks_dir, root) and hooks_dir.is_dir():
        for fpath in sorted(hooks_dir.rglob("*")):
            if not _is_safe_bundle_entry(fpath, root) or not fpath.is_file():
                continue
            rel = fpath.relative_to(root).as_posix()
            raw = fpath.read_bytes()
            surface[rel] = raw
            if fpath.suffix == ".json":
                try:
                    data = orjson.loads(raw)
                except ValueError:
                    continue
                _extract_referenced_paths(data, referenced_rel_paths)

    # Include referenced scripts and sibling scripts inside referenced hook script directories (e.g. `.codex/hooks/`)
    seen_dirs: set[Path] = set()
    for rel_str in sorted(referenced_rel_paths):
        clean_rel = rel_str.lstrip("/")
        if not clean_rel or clean_rel in {".", ".."}:
            continue
        cursor = root
        symlink_hit = False
        for part in Path(clean_rel).parts:
            if part in {"", "."}:
                continue
            cursor = cursor / part
            if cursor.is_symlink():
                symlink_hit = True
                break
        if symlink_hit:
            continue
        cand = cursor.resolve(strict=False)
        if cand == root or not cand.is_relative_to(root):
            continue
        if cand.is_file():
            surface[cand.relative_to(root).as_posix()] = cand.read_bytes()
            parent = cand.parent
            if parent != root and parent not in seen_dirs:
                seen_dirs.add(parent)
                for sib in sorted(parent.rglob("*")):
                    if (
                        _is_safe_bundle_entry(sib, root)
                        and sib.is_file()
                        and sib.name not in _SKIP_BUNDLE_NAMES
                        and not sib.name.endswith(".pyc")
                        and "__pycache__" not in sib.parts
                    ):
                        surface[sib.relative_to(root).as_posix()] = sib.read_bytes()
        elif cand.is_dir() and cand not in seen_dirs:
            seen_dirs.add(cand)
            for sib in sorted(cand.rglob("*")):
                if (
                    _is_safe_bundle_entry(sib, root)
                    and sib.is_file()
                    and sib.name not in _SKIP_BUNDLE_NAMES
                    and not sib.name.endswith(".pyc")
                    and "__pycache__" not in sib.parts
                ):
                    surface[sib.relative_to(root).as_posix()] = sib.read_bytes()

    return surface


def _extract_referenced_paths(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "command" and isinstance(v, str):
                for m in _PLUGIN_ROOT_VAR_RE.finditer(v):
                    out.add(m.group(1))
                for tok in re.findall(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", v):
                    if not tok.startswith("/"):
                        norm = tok[2:] if tok.startswith("./") else tok
                        if norm:
                            out.add(norm)
            else:
                _extract_referenced_paths(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _extract_referenced_paths(item, out)


def diff_hook_surface(
    old_root: Path, new_root: Path
) -> tuple[bool, list[CodexHookSurfaceFile], str]:
    """Compare strictly the hook surface between *old_root* and *new_root*."""
    old_map = collect_hook_surface(old_root)
    new_map = collect_hook_surface(new_root)
    all_keys = sorted(set(old_map) | set(new_map))
    changes: list[CodexHookSurfaceFile] = []
    diff_chunks: list[str] = []

    for rel in all_keys:
        in_old = rel in old_map
        in_new = rel in new_map
        if in_old and not in_new:
            changes.append(
                CodexHookSurfaceFile(path=rel, change=HookSurfaceChangeKind.REMOVED)
            )
        elif in_new and not in_old:
            changes.append(
                CodexHookSurfaceFile(path=rel, change=HookSurfaceChangeKind.ADDED)
            )
        elif old_map[rel] == new_map[rel]:
            continue
        else:
            changes.append(
                CodexHookSurfaceFile(path=rel, change=HookSurfaceChangeKind.MODIFIED)
            )

        old_lines = (
            old_map.get(rel, b"")
            .decode("utf-8", errors="replace")
            .splitlines(keepends=True)
        )
        new_lines = (
            new_map.get(rel, b"")
            .decode("utf-8", errors="replace")
            .splitlines(keepends=True)
        )
        diff_chunks.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )

    return not changes, changes, "".join(diff_chunks)


def _git_clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def _resolve_pin_sha(source: str, ref: str) -> str:
    stripped = ref.strip()
    if _HEX40_RE.fullmatch(stripped):
        return stripped.lower()
    if Path(source).exists():
        # Local repository checkout
        for candidate in (f"{stripped}^{{commit}}", stripped):
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    source,
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    candidate,
                ],
                env=_git_clean_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0 and _HEX40_RE.fullmatch(proc.stdout.strip()):
                return proc.stdout.strip().lower()
    proc = subprocess.run(
        [
            "git",
            "ls-remote",
            "--",
            source,
            f"refs/tags/{stripped}^{{}}",
            f"refs/tags/{stripped}",
            f"refs/heads/{stripped}",
        ],
        env=_git_clean_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CodexRpcError(
            f"unable to resolve ref {ref!r} to a 40-char commit SHA from {source}",
            code="ERR_GIT_FAILED",
        )
    by_name: dict[str, str] = {}
    for ln in proc.stdout.strip().splitlines():
        parts = ln.split()
        if len(parts) >= 2 and _HEX40_RE.fullmatch(parts[0]):
            by_name.setdefault(parts[1], parts[0].lower())
    for preferred in (
        f"refs/tags/{stripped}^{{}}",
        f"refs/tags/{stripped}",
        f"refs/heads/{stripped}",
    ):
        if preferred in by_name:
            return by_name[preferred]
    for sha in by_name.values():
        return sha
    raise CodexRpcError(
        f"ref {ref!r} did not resolve to a valid 40-char commit SHA",
        code="ERR_GIT_FAILED",
    )


def _checkout_pinned_plugin_source(
    source: str, sha: str, checkout_dir: Path, plugin_name: str
) -> tuple[Path, str]:
    """Fetch exact 40-char *sha* in an isolated temp dir and return `(plugin_src_dir, version)`."""
    env = _git_clean_env()
    checkout_dir.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", "--", source],
        ["git", "fetch", "--depth=1", "origin", "--", sha],
        ["git", "checkout", "--detach", "-q", sha],
    ):
        proc = subprocess.run(
            cmd,
            cwd=checkout_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise CodexRpcError(
                f"git command {' '.join(cmd)} failed: {proc.stderr.strip()}",
                code="ERR_GIT_FAILED",
            )

    manifest_path = next(
        (
            checkout_dir / rel
            for rel in _CATALOG_PATHS
            if (checkout_dir / rel).is_file()
        ),
        None,
    )
    if manifest_path is None:
        raise CodexRpcError(
            f"marketplace catalog not found in {source}@{sha}",
            code="ERR_GIT_FAILED",
        )
    catalog = orjson.loads(manifest_path.read_bytes())
    entry = next(
        (p for p in catalog.get("plugins") or [] if p.get("name") == plugin_name),
        None,
    )
    if entry is None:
        raise CodexRpcError(
            f"plugin {plugin_name!r} not found in marketplace catalog",
            code="ERR_INVALID_INPUT",
        )
    local_src = entry.get("source")
    if isinstance(local_src, dict):
        local_src = local_src.get("path") or "."
    rel_path = Path(str(local_src or "."))
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise CodexRpcError(f"invalid plugin source path: {rel_path}")
    checkout_resolved = checkout_dir.resolve(strict=False)
    cursor = checkout_dir
    for part in rel_path.parts:
        if part in {"", "."}:
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            raise CodexRpcError(f"plugin source path traverses symlink: {cursor}")
    src_root = cursor.resolve(strict=False)
    if not src_root.is_relative_to(checkout_resolved):
        raise CodexRpcError(f"plugin source escapes checkout directory: {rel_path}")

    new_version = "0.0.0"
    plugin_meta_dir = src_root / ".codex-plugin"
    pj = plugin_meta_dir / "plugin.json"
    if (
        plugin_meta_dir.is_symlink()
        or pj.is_symlink()
        or (pj.exists() and not pj.is_file())
    ):
        raise CodexRpcError(
            f"invalid or symlinked .codex-plugin/plugin.json in {src_root}",
            code="ERR_INVALID_INPUT",
        )
    if pj.is_file():
        pj_data = orjson.loads(pj.read_bytes())
        raw_ver = pj_data.get("version")
        if raw_ver is not None:
            if not isinstance(raw_ver, str):
                raise CodexRpcError(
                    f"invalid plugin.json version type: {raw_ver!r}",
                    code="ERR_INVALID_INPUT",
                )
            if raw_ver:
                _validate_version(raw_ver)
                new_version = raw_ver
    return src_root, new_version


def _copy_clean_bundle(src_root: Path, dst_root: Path) -> None:
    """Copy clean plugin tree without `.git` or `__pycache__` or symlinks."""
    dst_root.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in {".git", "__pycache__", ".venv"} and not d.endswith(".egg-info")
        )
        cur_dir = Path(dirpath)
        rel_dir = cur_dir.relative_to(src_root)
        target_dir = dst_root / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in sorted(filenames):
            if fname.endswith(".pyc"):
                continue
            src_file = cur_dir / fname
            if src_file.is_symlink():
                # Skip or fail? Skip non-hook repo symlinks cleanly if not executable hook, else fail
                continue
            if src_file.is_file():
                dst_file = target_dir / fname
                shutil.copy2(src_file, dst_file)


def _sync_agy_pwf(
    repo_root: Path | None = None, *, codex_home: Path | None = None
) -> bool:
    candidates: list[Path] = []
    if repo_root is not None:
        candidates.append(
            repo_root
            / "unix"
            / "home"
            / ".gemini"
            / "config"
            / "hooks"
            / "agy-pwf-sync.py"
        )
    candidates.append(Path.home() / ".gemini" / "config" / "hooks" / "agy-pwf-sync.py")
    sync_script = next((p for p in candidates if p.is_file()), None)
    if sync_script is None:
        return False
    py_exe = shutil.which("python3") or "/usr/bin/python3"
    cmd = [py_exe, "-I", "-B", str(sync_script)]
    if codex_home is not None:
        cmd.extend(["--codex-home", str(codex_home)])
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0


def update_plugin(
    plugin: str,
    *,
    ref: str | None = None,
    apply: bool = False,
    trust_hook_diff: bool = False,
    cwd: Path | None = None,
    codex_home: Path | None = None,
    client_factory: Callable[[Path], Any] | None = None,
    stage_checkout_fn: Callable[[str, str, Path, str], tuple[Path, str]] | None = None,
    sync_pwf_fn: Callable[[], bool] | None = None,
) -> CodexTrustResponse:
    """Safely stage, diff-gate hook surface, additively install, and trust a pinned Codex plugin."""
    ts, ns = now_ns_pair()
    resolved_home = (codex_home or default_codex_home()).resolve(strict=False)
    resolved_cwd = (cwd or Path.cwd()).resolve(strict=False)
    config_path = resolved_home / "config.toml"

    plugin_name, sep, marketplace = plugin.partition("@")
    if not sep:
        marketplace = plugin_name
    plugin_id = f"{plugin_name}@{marketplace}"
    try:
        _validate_ident(plugin_name, "plugin name")
        _validate_ident(marketplace, "marketplace name")
    except CodexRpcError as exc:
        return CodexTrustResponse(
            command=CodexTrustCommand.PLUGIN_UPDATE,
            rc=1,
            codex_home=str(resolved_home),
            cwd=str(resolved_cwd),
            plugin_id=plugin_id,
            applied=False,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                CodexTrustErrorEntry(
                    code=CodexTrustErrorCode.ERR_INVALID_INPUT,
                    message=str(exc),
                )
            ],
            returns=[
                CodexTrustReturnEntry(
                    code=CodexTrustReturnCode.RET_FAILED, message=str(exc)
                )
            ],
        )

    cfg, _ = _read_codex_config(config_path)
    mkts = cfg.get("marketplaces") or {}
    mkt_cfg = mkts.get(marketplace) if isinstance(mkts, dict) else None
    if not isinstance(mkt_cfg, dict) or not mkt_cfg.get("source"):
        return CodexTrustResponse(
            command=CodexTrustCommand.PLUGIN_UPDATE,
            rc=1,
            codex_home=str(resolved_home),
            cwd=str(resolved_cwd),
            plugin_id=plugin_id,
            applied=False,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                CodexTrustErrorEntry(
                    code=CodexTrustErrorCode.ERR_INVALID_INPUT,
                    message=f"marketplace {marketplace!r} missing source in {config_path}",
                )
            ],
            returns=[CodexTrustReturnEntry(code=CodexTrustReturnCode.RET_FAILED)],
        )

    source = str(mkt_cfg["source"])
    old_ref = str(mkt_cfg.get("ref") or mkt_cfg.get("last_revision") or "") or None
    if not ref:
        return CodexTrustResponse(
            command=CodexTrustCommand.PLUGIN_UPDATE,
            rc=1,
            codex_home=str(resolved_home),
            cwd=str(resolved_cwd),
            plugin_id=plugin_id,
            old_ref=old_ref,
            applied=False,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                CodexTrustErrorEntry(
                    code=CodexTrustErrorCode.ERR_PIN_REQUIRED,
                    message="Explicit --ref <40-char-sha-or-tag> required; never float hook-bearing marketplaces.",
                )
            ],
            returns=[CodexTrustReturnEntry(code=CodexTrustReturnCode.RET_FAILED)],
        )

    try:
        new_sha = _resolve_pin_sha(source, ref)
    except CodexRpcError as exc:
        return CodexTrustResponse(
            command=CodexTrustCommand.PLUGIN_UPDATE,
            rc=1,
            codex_home=str(resolved_home),
            cwd=str(resolved_cwd),
            plugin_id=plugin_id,
            old_ref=old_ref,
            applied=False,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                CodexTrustErrorEntry(
                    code=CodexTrustErrorCode.ERR_GIT_FAILED, message=str(exc)
                )
            ],
            returns=[
                CodexTrustReturnEntry(
                    code=CodexTrustReturnCode.RET_FAILED, message=str(exc)
                )
            ],
        )

    # Locate existing cached plugin version(s)
    plugin_cache_dir = resolved_home / "plugins" / "cache" / marketplace / plugin_name
    existing_versions = (
        sorted(
            (
                d.name
                for d in plugin_cache_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            ),
            key=_version_sort_key,
        )
        if plugin_cache_dir.is_dir()
        else []
    )
    old_version = existing_versions[-1] if existing_versions else None
    old_root = plugin_cache_dir / old_version if old_version else None

    mk_client = client_factory or (lambda h: CodexAppServerClient(codex_home=h))
    checkout_fn = stage_checkout_fn or _checkout_pinned_plugin_source

    with tempfile.TemporaryDirectory(prefix="codex-plugin-stage-") as tmp_str:
        tmp_dir = Path(tmp_str)
        stage_home = tmp_dir / "codex_home"
        stage_checkout = tmp_dir / "checkout"
        try:
            src_root, new_version = checkout_fn(
                source, new_sha, stage_checkout, plugin_name
            )
            _validate_version(new_version)
        except CodexRpcError as exc:
            err_code = (
                CodexTrustErrorCode.ERR_INVALID_INPUT
                if exc.code == "ERR_INVALID_INPUT"
                else CodexTrustErrorCode.ERR_GIT_FAILED
            )
            return CodexTrustResponse(
                command=CodexTrustCommand.PLUGIN_UPDATE,
                rc=1,
                codex_home=str(resolved_home),
                cwd=str(resolved_cwd),
                plugin_id=plugin_id,
                old_ref=old_ref,
                new_ref=new_sha,
                old_version=old_version,
                applied=False,
                timestamp=ts,
                timestamp_unix_ns=ns,
                errors=[CodexTrustErrorEntry(code=err_code, message=str(exc))],
                returns=[
                    CodexTrustReturnEntry(
                        code=CodexTrustReturnCode.RET_FAILED, message=str(exc)
                    )
                ],
            )

        staged_bundle = (
            stage_home / "plugins" / "cache" / marketplace / plugin_name / new_version
        )
        _copy_clean_bundle(src_root, staged_bundle)
        install_receipt = {
            "source_type": "git",
            "source": source,
            "ref_name": None if _HEX40_RE.fullmatch(ref.strip()) else ref.strip(),
            "sparse_paths": [],
            "revision": new_sha,
        }
        (staged_bundle / ".codex-marketplace-install.json").write_text(
            orjson.dumps(install_receipt, option=orjson.OPT_INDENT_2).decode("utf-8")
            + "\n",
            encoding="utf-8",
        )

        # Write minimal isolated CODEX_HOME config for native discovery on stage_home
        stage_cfg_text = (
            f"[plugins.{_toml_str_quote(plugin_id)}]\nenabled = true\n\n"
            f'[marketplaces.{_toml_section_key(marketplace)}]\nsource_type = "git"\nsource = {_toml_str_quote(source)}\nref = {_toml_str_quote(new_sha)}\n'
        )
        _write_atomic_mode_0600(stage_home / "config.toml", stage_cfg_text)

        # Compare old SHA hook surface vs staged hook surface
        if old_root is None or not old_root.is_dir():
            surface_unchanged, surface_files, surface_diff = diff_hook_surface(
                tmp_dir / "empty_old", staged_bundle
            )
        else:
            surface_unchanged, surface_files, surface_diff = diff_hook_surface(
                old_root, staged_bundle
            )

        if not surface_unchanged and not trust_hook_diff:
            return CodexTrustResponse(
                command=CodexTrustCommand.PLUGIN_UPDATE,
                rc=1,
                codex_home=str(resolved_home),
                cwd=str(resolved_cwd),
                plugin_id=plugin_id,
                old_ref=old_ref,
                new_ref=new_sha,
                old_version=old_version,
                new_version=new_version,
                hook_surface_unchanged=False,
                hook_surface_files=surface_files,
                hook_surface_diff=surface_diff,
                preserved_cache_versions=existing_versions,
                applied=False,
                timestamp=ts,
                timestamp_unix_ns=ns,
                errors=[
                    CodexTrustErrorEntry(
                        code=CodexTrustErrorCode.ERR_HOOK_SURFACE_CHANGED,
                        message="Plugin hook surface changed; inspect hook_surface_diff and re-run with --trust-hook-diff.",
                    )
                ],
                returns=[
                    CodexTrustReturnEntry(
                        code=CodexTrustReturnCode.RET_CONFIRMATION_REQUIRED,
                        message="Re-run with --trust-hook-diff to approve hook changes.",
                    )
                ],
            )

        # Probe isolated stage CODEX_HOME via native hooks/list to obtain exact Codex hashes
        try:
            stage_client = mk_client(stage_home)
            stage_hooks_res = stage_client.hooks_list(resolved_cwd)
        except CodexRpcError as exc:
            err_code = (
                CodexTrustErrorCode.ERR_UNSUPPORTED_CODEX_VERSION
                if exc.code == "ERR_UNSUPPORTED_CODEX_VERSION"
                else CodexTrustErrorCode.ERR_RPC_FAILED
            )
            return CodexTrustResponse(
                command=CodexTrustCommand.PLUGIN_UPDATE,
                rc=1,
                codex_version=getattr(stage_client, "version", None)
                if "stage_client" in locals()
                else None,
                codex_home=str(resolved_home),
                cwd=str(resolved_cwd),
                plugin_id=plugin_id,
                old_ref=old_ref,
                new_ref=new_sha,
                old_version=old_version,
                new_version=new_version,
                hook_surface_unchanged=surface_unchanged,
                hook_surface_files=surface_files,
                hook_surface_diff=surface_diff or None,
                applied=False,
                timestamp=ts,
                timestamp_unix_ns=ns,
                errors=[CodexTrustErrorEntry(code=err_code, message=str(exc))],
                returns=[
                    CodexTrustReturnEntry(
                        code=CodexTrustReturnCode.RET_FAILED, message=str(exc)
                    )
                ],
            )

        bundle_hash = plugin_content_hash(staged_bundle)
        stage_scope = (stage_hooks_res.get("data") or [{}])[0]
        stage_hooks = [
            row
            for row in (stage_scope.get("hooks") or [])
            if isinstance(row, dict) and row.get("pluginId") == plugin_id
        ]
        upserts: dict[str, tuple[str, str | None]] = {}
        for row in stage_hooks:
            k = str(row.get("key") or "")
            ch = str(row.get("currentHash") or "")
            if k and ch:
                upserts[k] = (
                    _decl_hash_from_current(ch),
                    _bundle_hash_from_current(ch, staged_bundle) or bundle_hash,
                )

        codex_ver = getattr(stage_client, "version", None)
        if not apply:
            return CodexTrustResponse(
                command=CodexTrustCommand.PLUGIN_UPDATE,
                rc=0,
                codex_version=codex_ver,
                codex_home=str(resolved_home),
                cwd=str(resolved_cwd),
                plugin_id=plugin_id,
                old_ref=old_ref,
                new_ref=new_sha,
                old_version=old_version,
                new_version=new_version,
                bundle_hash=bundle_hash,
                hook_surface_unchanged=surface_unchanged,
                hook_surface_files=surface_files,
                hook_surface_diff=surface_diff or None,
                preserved_cache_versions=existing_versions,
                repaired_keys=sorted(upserts),
                applied=False,
                timestamp=ts,
                timestamp_unix_ns=ns,
                errors=[],
                returns=[
                    CodexTrustReturnEntry(
                        code=CodexTrustReturnCode.RET_DRY_RUN,
                        message="Staged cleanly; re-run with --apply to install additively and update trust.",
                    )
                ],
            )

        # Additive installation preserving old cached version directories
        plugin_cache_dir.mkdir(parents=True, exist_ok=True)
        live_dest = plugin_cache_dir / new_version
        if live_dest.exists():
            # Replace only the target new_version dir if re-applying same version; never delete old_version
            staging_tmp = plugin_cache_dir / f".tmp-{new_version}-{os.getpid()}"
            if staging_tmp.exists():
                shutil.rmtree(staging_tmp)
            shutil.copytree(staged_bundle, staging_tmp)
            backup_old = plugin_cache_dir / f".bak-{new_version}-{os.getpid()}"
            live_dest.rename(backup_old)
            try:
                staging_tmp.rename(live_dest)
            except Exception:
                backup_old.rename(live_dest)
                raise
            shutil.rmtree(backup_old, ignore_errors=True)
        else:
            shutil.copytree(staged_bundle, live_dest)

        # Update marketplace ref and upsert hook trust hashes
        try:
            live_client = mk_client(resolved_home)
            _apply_hook_trust_edits(
                live_client,
                config_path,
                upserts,
                set(),
                marketplace_ref_update=(marketplace, new_sha),
            )
        except (CodexRpcError, OSError, ValueError) as exc:
            err_code = (
                CodexTrustErrorCode.ERR_RPC_FAILED
                if isinstance(exc, CodexRpcError)
                else CodexTrustErrorCode.ERR_IO_FAILURE
            )
            return CodexTrustResponse(
                command=CodexTrustCommand.PLUGIN_UPDATE,
                rc=1,
                codex_version=codex_ver,
                codex_home=str(resolved_home),
                cwd=str(resolved_cwd),
                plugin_id=plugin_id,
                old_ref=old_ref,
                new_ref=new_sha,
                old_version=old_version,
                new_version=new_version,
                bundle_hash=bundle_hash,
                hook_surface_unchanged=surface_unchanged,
                hook_surface_files=surface_files,
                hook_surface_diff=surface_diff or None,
                applied=False,
                timestamp=ts,
                timestamp_unix_ns=ns,
                errors=[CodexTrustErrorEntry(code=err_code, message=str(exc))],
                returns=[
                    CodexTrustReturnEntry(
                        code=CodexTrustReturnCode.RET_FAILED, message=str(exc)
                    )
                ],
            )

        # Clean stale legacy baseline if superseded by trusted_content_hash
        leg = legacy_trust_path(config_path, plugin_id)
        legacy_pruned: list[str] = []
        if leg.is_file():
            leg.unlink(missing_ok=True)
            legacy_pruned.append(str(leg))

        pwf_synced = False
        if plugin_name == "planning-with-files":
            pwf_synced = (
                sync_pwf_fn()
                if sync_pwf_fn
                else _sync_agy_pwf(codex_home=resolved_home)
            )

        preserved = sorted(
            (
                d.name
                for d in plugin_cache_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".") and d.name != new_version
            ),
            key=_version_sort_key,
        )
        return CodexTrustResponse(
            command=CodexTrustCommand.PLUGIN_UPDATE,
            rc=0,
            codex_version=codex_ver,
            codex_home=str(resolved_home),
            cwd=str(resolved_cwd),
            plugin_id=plugin_id,
            old_ref=old_ref,
            new_ref=new_sha,
            old_version=old_version,
            new_version=new_version,
            bundle_hash=bundle_hash,
            hook_surface_unchanged=surface_unchanged,
            hook_surface_files=surface_files,
            hook_surface_diff=surface_diff or None,
            repaired_keys=sorted(upserts),
            legacy_baselines_pruned=legacy_pruned,
            preserved_cache_versions=preserved,
            pwf_synced=pwf_synced,
            applied=True,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[],
            returns=[
                CodexTrustReturnEntry(
                    code=CodexTrustReturnCode.RET_UPDATED,
                    message=f"Updated {plugin_id} to {new_version} ({new_sha[:12]}) cleanly.",
                )
            ],
        )


def check_codex_binary_available() -> bool:
    """Return True when a native Codex binary is resolvable without raising."""
    try:
        resolve_codex_binary()
        return True
    except CodexRpcError:
        return False
