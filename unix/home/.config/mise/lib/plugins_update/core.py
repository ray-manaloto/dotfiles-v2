"""Resolve, gate, and apply tracked plugin updates for Codex and Claude user scope.

Every update is gated by a strict whole-tree comparison (`plugins_update.surface`): the
only auto-approved differences are version strings and unreferenced docs. Anything else
yields `needs-review` (exit 3); the owner inspects the diff and approves that exact diff
with `--approve <sha256>`.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

import orjson

from dotfiles_py.generated import (
    PluginsTracked,
    PluginsUpdateErrorCode,
    PluginsUpdateErrorEntry,
    PluginsUpdateResponse,
    PluginsUpdateReturnCode,
    PluginsUpdateReturnEntry,
    PluginsUpdateStatus,
    PluginUpdateResult,
    TrackedPlugin,
)
from dotfiles_py.logging import get_logger
from dotfiles_py.time_ns import now_ns_pair
from plugins_update import drift, surface
from plugins_update.drift import LookupFailure, Runner

RC_OK = 0
RC_ERROR = 1
RC_NEEDS_REVIEW = 3
_MAX_TARBALL_BYTES = 200 * 1024 * 1024
_CODEX_ARTIFACT = ".codex-marketplace-install.json"

log = get_logger("plugins_update")

TarballFetcher = Callable[[str, str], bytes]


def load_tracked(path: Path) -> PluginsTracked:
    return PluginsTracked.model_validate(orjson.loads(path.read_bytes()))


def fetch_tarball(repo: str, ref: str) -> bytes:
    """Download the tarball for *ref* (a commit SHA) via `gh api` (binary stdout)."""
    import shutil
    import subprocess

    if shutil.which("gh") is None:
        raise LookupFailure("gh not found on PATH", "ERR_TOOL_MISSING")
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/tarball/{ref}"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LookupFailure(
            f"gh tarball download failed: {exc}", "ERR_GITHUB_LOOKUP_FAILED"
        ) from exc
    if proc.returncode != 0 or not proc.stdout or len(proc.stdout) > _MAX_TARBALL_BYTES:
        raise LookupFailure(
            f"gh tarball download for {repo}@{ref} failed or exceeded the size cap",
            "ERR_GITHUB_LOOKUP_FAILED",
        )
    return proc.stdout


MAX_TAR_MEMBERS = 20_000
MAX_TAR_BYTES = 400 * 1024 * 1024

GateOutcome = tuple[
    str, list[str], list[PluginsUpdateErrorEntry], str | None, dict[str, str]
]


def _extract_release(data: bytes, dest: Path) -> Path:
    """Safely extract a GitHub tarball into *dest* and return its single top-level dir."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        members = tf.getmembers()
        if len(members) > MAX_TAR_MEMBERS:
            raise LookupFailure(
                f"release tarball has {len(members)} members (cap {MAX_TAR_MEMBERS})",
                "ERR_SURFACE_COMPARE_FAILED",
            )
        if sum(max(m.size, 0) for m in members) > MAX_TAR_BYTES:
            raise LookupFailure(
                f"release tarball exceeds {MAX_TAR_BYTES} uncompressed bytes",
                "ERR_SURFACE_COMPARE_FAILED",
            )
        tf.extractall(dest, members=members, filter="data")
    tops = [p for p in dest.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise LookupFailure(
            "release tarball did not contain exactly one top-level directory",
            "ERR_SURFACE_COMPARE_FAILED",
        )
    return tops[0]


def _err(code: PluginsUpdateErrorCode, message: str) -> PluginsUpdateErrorEntry:
    return PluginsUpdateErrorEntry(code=code, message=message)


def _code(
    exc: LookupFailure, default: PluginsUpdateErrorCode
) -> PluginsUpdateErrorCode:
    return (
        PluginsUpdateErrorCode(exc.code)
        if exc.code in PluginsUpdateErrorCode._value2member_map_
        else default
    )


def _verdict(
    label: str,
    plugin: str,
    plugin_id: str,
    old_tree: surface.Tree,
    new_tree: surface.Tree,
    old_ver: str,
    new_ver: str,
    pin: str,
    approve: str | None,
) -> tuple[str, list[str], str | None]:
    """Gate *old_tree* (version-substituted) against *new_tree*.

    Returns `(state, notes, review_command)` with state `clean`, `approved` or `review`.
    """
    old = surface.substitute_version(old_tree, old_ver, new_ver)
    review, diff_text, digest = surface.gate(
        old, new_tree, f"{label}\0{plugin_id}\0{pin}"
    )
    if not review:
        return "clean", [], None
    files = ", ".join(f"{p} ({k})" for p, k in review[:40])
    more = f" (+{len(review) - 40} more)" if len(review) > 40 else ""
    if approve is not None and approve == digest:
        return "approved", [f"owner-approved diff {digest[:12]}: {files}{more}"], None
    notes = [f"changed files need owner review: {files}{more}", diff_text]
    if approve is not None:
        notes.insert(
            0,
            "--approve hash does not match the freshly computed gate diff; refusing",
        )
    base = f"mise run plugins:update --plugin {plugin}"
    cmd = (
        f"{label.lower()} diff: `{base}`; "
        f"{label.lower()} approve: `{base} --approve {digest}`"
    )
    return "review", notes, cmd


def _codex_cache_dir(tp: TrackedPlugin, version: str) -> Path:
    home = os.environ.get("CODEX_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return base / "plugins" / "cache" / tp.marketplace / tp.plugin / version


def _gate_error(code: PluginsUpdateErrorCode, message: str) -> GateOutcome:
    return "error", [], [_err(code, message)], None, {}


def _codex_gate(
    tp: TrackedPlugin,
    sha: str,
    latest: str,
    codex_ver: str,
    *,
    apply: bool,
    approve: str | None,
    runner: Runner,
    fetcher: TarballFetcher,
) -> GateOutcome:
    """Gate the pinned-SHA tarball against the installed cache, install, then re-diff."""
    plugin_id = f"{tp.plugin}@{tp.marketplace}"
    installed = _codex_cache_dir(tp, codex_ver)
    if not installed.is_dir():
        return _gate_error(
            PluginsUpdateErrorCode.ERR_SURFACE_COMPARE_FAILED,
            f"installed Codex cache dir missing: {installed}",
        )
    try:
        old_tree = surface.read_tree(installed, {_CODEX_ARTIFACT}, runtime_pycache=True)
        data = fetcher(tp.repo, sha)
        with tempfile.TemporaryDirectory(prefix="plugins-update-") as tmp:
            new_tree = surface.read_tree(_extract_release(data, Path(tmp)))
    except (LookupFailure, tarfile.TarError, surface.TreeTooLarge, OSError) as exc:
        code = (
            _code(exc, PluginsUpdateErrorCode.ERR_SURFACE_COMPARE_FAILED)
            if isinstance(exc, LookupFailure)
            else PluginsUpdateErrorCode.ERR_SURFACE_COMPARE_FAILED
        )
        return _gate_error(code, f"Codex surface staging failed: {exc}")
    state, notes, review_cmd = _verdict(
        "Codex",
        tp.plugin,
        plugin_id,
        old_tree,
        new_tree,
        codex_ver,
        latest,
        sha,
        approve,
    )
    if state == "review":
        return "review", notes, [], review_cmd, {}
    argv = [
        "mise",
        "run",
        "-q",
        "codex:plugin-update",
        plugin_id,
        "--ref",
        sha,
        "--json",
    ]
    if apply:
        argv.append("--apply")
    if state == "approved":
        argv.append("--trust-hook-diff")  # only after an exact-diff owner approval
    try:
        proc = runner(argv)
    except LookupFailure as exc:
        return _gate_error(
            _code(exc, PluginsUpdateErrorCode.ERR_CODEX_UPDATE_FAILED), str(exc)
        )
    payload: dict[str, object] = {}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break
    raw_errors = payload.get("errors")
    codes = {
        str(e.get("code"))
        for e in (raw_errors if isinstance(raw_errors, list) else [])
        if isinstance(e, dict)
    }
    if "ERR_HOOK_SURFACE_CHANGED" in codes:
        base = f"mise run codex:plugin-update {plugin_id} --ref {sha}"
        return (
            "review",
            ["Codex's own hook-surface gate refused; inspect the diff."],
            [],
            f"codex diff: `{base}`",
            {},
        )
    if proc.returncode != 0 or not payload:
        detail = (proc.stderr.strip() or proc.stdout.strip())[:300]
        return _gate_error(
            PluginsUpdateErrorCode.ERR_CODEX_UPDATE_FAILED,
            f"codex:plugin-update failed: {detail}",
        )
    if not apply:
        return "ready", notes, [], None, {}
    try:
        after_ver = drift.codex_installed_version(tp.plugin, tp.marketplace, runner)
        after_dir = _codex_cache_dir(tp, after_ver or latest)
        after_tree = surface.read_tree(after_dir, {_CODEX_ARTIFACT})
    except (LookupFailure, surface.TreeTooLarge, OSError) as exc:
        return _gate_error(
            PluginsUpdateErrorCode.ERR_POST_INSTALL_MISMATCH,
            f"post-install Codex comparison failed: {exc}",
        )
    mism = surface.strict_diff(new_tree, after_tree)
    if after_ver != latest or mism:
        files = ", ".join(f"{p} ({k})" for p, k in mism[:20])
        return _gate_error(
            PluginsUpdateErrorCode.ERR_POST_INSTALL_MISMATCH,
            f"INSTALLED CODEX TREE DIFFERS FROM THE GATED {sha[:12]} TREE "
            f"(version={after_ver}): {files}. Codex has no documented plugin-disable "
            f"command; inspect it and run `codex plugin remove {plugin_id}` if untrusted.",
        )
    return "updated", notes, [], None, {}


def _claude_source_dir(clone: Path, plugin: str) -> Path:
    """Directory of *plugin* inside the marketplace clone (string `source` only)."""
    cat = clone / ".claude-plugin" / "marketplace.json"
    try:
        entries = json.loads(cat.read_text(encoding="utf-8"))["plugins"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LookupFailure(
            f"cannot read marketplace catalog {cat}: {exc}",
            "ERR_SURFACE_COMPARE_FAILED",
        ) from exc
    for ent in entries:
        if isinstance(ent, dict) and ent.get("name") == plugin:
            src = ent.get("source")
            if not isinstance(src, str) or not src.startswith("./"):
                raise LookupFailure(
                    f"plugin {plugin} uses a non-local marketplace source; cannot gate",
                    "ERR_SURFACE_COMPARE_FAILED",
                )
            path = (clone / src).resolve()
            if not path.is_relative_to(clone.resolve()) or not path.is_dir():
                raise LookupFailure(
                    f"plugin source {src} is outside or missing in the clone",
                    "ERR_SURFACE_COMPARE_FAILED",
                )
            return path
    raise LookupFailure(
        f"plugin {plugin} not found in marketplace catalog",
        "ERR_SURFACE_COMPARE_FAILED",
    )


def _clone_location(marketplace: str, runner: Runner) -> Path:
    proc = runner(["claude", "plugin", "marketplace", "list", "--json"])
    try:
        rows = json.loads(proc.stdout)
        for row in rows:
            if isinstance(row, dict) and row.get("name") == marketplace:
                return Path(str(row["installLocation"]))
    except ValueError, KeyError, TypeError:
        pass
    raise LookupFailure(
        f"cannot locate the {marketplace} marketplace clone",
        "ERR_SURFACE_COMPARE_FAILED",
    )


def _claude_gate_and_update(
    tp: TrackedPlugin,
    latest_sha: str,
    claude_ver: str,
    latest: str,
    install_path: str,
    *,
    apply: bool,
    approve: str | None,
    runner: Runner,
) -> GateOutcome:
    """Refresh the marketplace clone, gate clone-vs-installed, update, verify, else disable."""
    plugin_id = f"{tp.plugin}@{tp.marketplace}"
    installed = Path(install_path)
    extra: dict[str, str] = {}

    def fail(code: PluginsUpdateErrorCode, msg: str) -> GateOutcome:
        return "error", [], [_err(code, msg)], None, extra

    if not installed.is_dir():
        return fail(
            PluginsUpdateErrorCode.ERR_SURFACE_COMPARE_FAILED,
            f"installed Claude plugin dir missing: {install_path}",
        )
    notes: list[str] = []
    try:
        proc = runner(["claude", "plugin", "marketplace", "update", tp.marketplace])
        if proc.returncode != 0:
            return fail(
                PluginsUpdateErrorCode.ERR_CLAUDE_UPDATE_FAILED,
                f"claude plugin marketplace update failed: "
                f"{(proc.stderr.strip() or proc.stdout.strip())[:300]}",
            )
        clone = _clone_location(tp.marketplace, runner)
        source_dir = _claude_source_dir(clone, tp.plugin)
        head = runner(["git", "-C", str(clone), "rev-parse", "HEAD"])
        head_sha = head.stdout.strip() if head.returncode == 0 else ""
        if head_sha:
            extra["clone_head_sha"] = head_sha
            ancestor = runner(
                [
                    "git",
                    "-C",
                    str(clone),
                    "merge-base",
                    "--is-ancestor",
                    latest_sha,
                    "HEAD",
                ]
            )
            if ancestor.returncode != 0:
                notes.append(
                    f"marketplace clone HEAD {head_sha[:12]} is not at or after the "
                    f"latest release commit {latest_sha[:12]} (informational)"
                )
        else:
            notes.append("could not read the marketplace clone HEAD (informational)")
        old_tree = surface.read_tree(installed, {".in_use"}, runtime_pycache=True)
        snapshot = surface.read_tree(source_dir)
    except LookupFailure as exc:
        return fail(
            _code(exc, PluginsUpdateErrorCode.ERR_SURFACE_COMPARE_FAILED), str(exc)
        )
    except (surface.TreeTooLarge, OSError) as exc:
        return fail(
            PluginsUpdateErrorCode.ERR_SURFACE_COMPARE_FAILED,
            f"Claude surface comparison failed: {exc}",
        )
    state, vnotes, review_cmd = _verdict(
        "Claude",
        tp.plugin,
        plugin_id,
        old_tree,
        snapshot,
        claude_ver,
        latest,
        extra.get("clone_head_sha", "unknown"),
        approve,
    )
    if state == "review":
        return "review", notes + vnotes, [], review_cmd, extra
    notes += vnotes
    if not apply:
        return "ready", notes, [], None, extra
    try:
        proc = runner(["claude", "plugin", "update", plugin_id, "--scope", "user"])
        if proc.returncode != 0:
            return fail(
                PluginsUpdateErrorCode.ERR_CLAUDE_UPDATE_FAILED,
                f"claude plugin update failed: "
                f"{(proc.stderr.strip() or proc.stdout.strip())[:300]}",
            )
        after = drift.claude_user_install(tp.plugin, tp.marketplace, runner)
        problem = ""
        mism: list[tuple[str, str]] = []
        if after is None or not Path(after[1]).is_dir():
            problem = "Claude install path missing after update"
        else:
            mism = surface.strict_diff(snapshot, surface.read_tree(Path(after[1])))
            if mism:
                problem = (
                    "installed Claude tree differs from the gated clone snapshot: "
                )
                problem += ", ".join(f"{p} ({k})" for p, k in mism[:20])
    except LookupFailure as exc:
        return fail(
            _code(exc, PluginsUpdateErrorCode.ERR_CLAUDE_UPDATE_FAILED), str(exc)
        )
    except (surface.TreeTooLarge, OSError) as exc:
        problem = f"post-install comparison failed: {exc}"
    if problem:
        try:
            dis = runner(["claude", "plugin", "disable", plugin_id, "--scope", "user"])
            outcome = (
                "plugin disabled"
                if dis.returncode == 0
                else f"disable FAILED (rc={dis.returncode}); disable it manually"
            )
        except LookupFailure as exc:
            outcome = f"disable FAILED ({exc}); disable it manually"
        return fail(
            PluginsUpdateErrorCode.ERR_POST_INSTALL_MISMATCH,
            f"{problem}. Fail-closed: {outcome}.",
        )
    return "updated", notes, [], None, extra


def process_plugin(
    tp: TrackedPlugin,
    *,
    apply: bool,
    approve: str | None = None,
    runner: Runner = drift.run_cmd,
    fetcher: TarballFetcher = fetch_tarball,
) -> PluginUpdateResult:
    """Evaluate one tracked plugin; fail closed on any degraded input."""
    base = {"plugin": tp.plugin, "marketplace": tp.marketplace}
    try:
        tag, sha, latest = drift.latest_release(tp.repo, tp.tag_prefix, runner)
        codex_ver = drift.codex_installed_version(tp.plugin, tp.marketplace, runner)
        claude_inst = drift.claude_user_install(tp.plugin, tp.marketplace, runner)
    except LookupFailure as exc:
        code = (
            PluginsUpdateErrorCode(exc.code)
            if exc.code in PluginsUpdateErrorCode._value2member_map_
            else PluginsUpdateErrorCode.ERR_UNKNOWN
        )
        return PluginUpdateResult(
            **base, status=PluginsUpdateStatus.ERROR, errors=[_err(code, str(exc))]
        )
    claude_ver = claude_inst[0] if claude_inst else None
    try:
        for what, ver in (
            ("Codex installed", codex_ver),
            ("Claude installed", claude_ver),
        ):
            if ver is not None:
                drift.validate_version(ver, what)
    except LookupFailure as exc:
        return PluginUpdateResult(
            **base,
            status=PluginsUpdateStatus.ERROR,
            errors=[_err(PluginsUpdateErrorCode.ERR_INVALID_INPUT, str(exc))],
        )
    info = {
        "latest_tag": tag,
        "latest_sha": sha,
        "latest_version": latest,
        "codex_version": codex_ver,
        "claude_version": claude_ver,
    }
    if codex_ver is None and claude_inst is None:
        return PluginUpdateResult(
            **base,
            **info,
            status=PluginsUpdateStatus.ERROR,
            errors=[
                _err(
                    PluginsUpdateErrorCode.ERR_NOT_INSTALLED,
                    "not installed for Codex or Claude user scope",
                )
            ],
        )

    notes: list[str] = []
    errors: list[PluginsUpdateErrorEntry] = []
    review_cmds: list[str] = []
    outcomes: list[str] = []
    clone_head: str | None = None
    try:
        codex_stale = codex_ver is not None and drift.version_lt(codex_ver, latest)
        claude_stale = claude_ver is not None and drift.version_lt(claude_ver, latest)
    except LookupFailure as exc:
        return PluginUpdateResult(
            **base,
            **info,
            status=PluginsUpdateStatus.ERROR,
            errors=[_err(PluginsUpdateErrorCode.ERR_UNKNOWN, str(exc))],
        )
    if codex_ver is None:
        notes.append("Codex: not installed, skipped")
    if claude_inst is None:
        notes.append("Claude user scope: not installed, skipped")

    if codex_stale and codex_ver:
        out, n, e, rc_cmd, _x = _codex_gate(
            tp,
            sha,
            latest,
            codex_ver,
            apply=apply,
            approve=approve,
            runner=runner,
            fetcher=fetcher,
        )
        outcomes.append(out)
        notes += [f"Codex: {x}" for x in n]
        errors += e
        if rc_cmd:
            review_cmds.append(rc_cmd)
    if claude_stale and claude_inst and claude_ver:
        out, n, e, rc_cmd, extra = _claude_gate_and_update(
            tp,
            sha,
            claude_ver,
            latest,
            claude_inst[1],
            apply=apply,
            approve=approve,
            runner=runner,
        )
        clone_head = extra.get("clone_head_sha")
        outcomes.append(out)
        notes += [f"Claude: {x}" if not x.startswith("Claude") else x for x in n]
        errors += e
        if rc_cmd:
            review_cmds.append(rc_cmd)

    if errors or "error" in outcomes:
        status = PluginsUpdateStatus.ERROR
    elif "review" in outcomes:
        status = PluginsUpdateStatus.NEEDS_REVIEW
    elif not outcomes:
        status = PluginsUpdateStatus.UP_TO_DATE
    elif apply:
        status = PluginsUpdateStatus.UPDATED
    else:
        status = PluginsUpdateStatus.UPDATE_AVAILABLE
    return PluginUpdateResult(
        **base,
        **info,
        status=status,
        review_command="; ".join(review_cmds) or None,
        clone_head_sha=clone_head,
        notes=notes,
        errors=errors,
    )


def run_update(
    tracked: PluginsTracked,
    *,
    apply: bool,
    approve: str | None = None,
    only: str | None = None,
    runner: Runner = drift.run_cmd,
    fetcher: TarballFetcher = fetch_tarball,
) -> PluginsUpdateResponse:
    plugins = [p for p in tracked.plugins if only in (None, p.plugin)]
    results = [
        process_plugin(p, apply=apply, approve=approve, runner=runner, fetcher=fetcher)
        for p in plugins
    ]
    errors: list[PluginsUpdateErrorEntry] = []
    if only is not None and not plugins:
        errors.append(
            _err(
                PluginsUpdateErrorCode.ERR_INVALID_INPUT,
                f"plugin {only!r} is not tracked",
            )
        )
    for r in results:
        errors += r.errors
    statuses = {r.status for r in results}
    returns: list[PluginsUpdateReturnEntry] = []
    if errors or PluginsUpdateStatus.ERROR in statuses:
        rc = RC_ERROR
    elif PluginsUpdateStatus.NEEDS_REVIEW in statuses:
        rc = RC_NEEDS_REVIEW
        returns.append(
            PluginsUpdateReturnEntry(
                code=PluginsUpdateReturnCode.RET_NEEDS_REVIEW,
                message="Plugin tree changed beyond version strings; owner review required.",
            )
        )
    else:
        rc = RC_OK
        if PluginsUpdateStatus.UPDATED in statuses:
            returns.append(
                PluginsUpdateReturnEntry(code=PluginsUpdateReturnCode.RET_UPDATED)
            )
        elif not apply and PluginsUpdateStatus.UPDATE_AVAILABLE in statuses:
            returns.append(
                PluginsUpdateReturnEntry(code=PluginsUpdateReturnCode.RET_DRY_RUN)
            )
        else:
            returns.append(
                PluginsUpdateReturnEntry(code=PluginsUpdateReturnCode.RET_OK)
            )
    ts, ns = now_ns_pair()
    log.info("plugins_update", rc=rc, apply=apply, plugins=[r.plugin for r in results])
    return PluginsUpdateResponse(
        command="plugins:update",
        rc=rc,
        apply=apply,
        results=results,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=errors,
        returns=returns,
    )


def format_summary(resp: PluginsUpdateResponse) -> str:
    lines: list[str] = []
    for r in resp.results:
        lines.append(
            f"{r.status.value:16} {r.plugin}@{r.marketplace}  latest={r.latest_version or '?'}"
            f"  codex={r.codex_version or '-'}  claude(user)={r.claude_version or '-'}"
        )
        for n in r.notes:
            lines.extend(f"    {ln}" for ln in n.splitlines())
        for e in r.errors:
            lines.append(f"    ERROR {e.code.value}: {e.message or ''}")
        if r.clone_head_sha:
            lines.append(f"    claude marketplace clone HEAD: {r.clone_head_sha}")
        if r.review_command:
            lines.append(f"    review: {r.review_command}")
    for e in resp.errors:
        if not any(e in r.errors for r in resp.results):
            lines.append(f"ERROR {e.code.value}: {e.message or ''}")
    lines.append(f"rc={resp.rc} ({'apply' if resp.apply else 'dry-run'})")
    return "\n".join(lines) + "\n"


__all__ = [
    "RC_ERROR",
    "RC_NEEDS_REVIEW",
    "RC_OK",
    "TarballFetcher",
    "format_summary",
    "load_tracked",
    "process_plugin",
    "run_update",
]
