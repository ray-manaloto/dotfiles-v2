"""Tests for schema-first `codex_trust` inspection, repair, hook-surface diffing, and additive plugin updates."""

from __future__ import annotations

import json
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest

from codex_trust import (
    collect_hook_surface,
    diff_hook_surface,
    inspect_hook_trust,
    plugin_content_hash,
    repair_hook_trust,
    update_plugin,
)
from codex_trust.rpc import CodexAppServerClient, CodexRpcError, parse_semver, semver_ge
from dotfiles_py.generated import (
    CodexHookStatus,
    CodexTrustErrorCode,
    CodexTrustReturnCode,
)


def _write_fake_plugin(
    root: Path,
    *,
    version: str,
    stop_cmd: str = "echo ok",
    hook_note: str = "same",
) -> Path:
    (root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    (root / "hooks").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(
            {"name": "planning-with-files", "version": version, "description": "PWF"}
        ),
        encoding="utf-8",
    )
    (root / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"sh scripts/stop.sh {stop_cmd}",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "scripts" / "stop.sh").write_text(
        f"#!/bin/sh\necho {hook_note}\n", encoding="utf-8"
    )
    (root / "README.md").write_text(f"Docs for {version}\n", encoding="utf-8")
    return root


class _FakeRpcClient:
    def __init__(
        self,
        *,
        version: str = "0.172.0",
        hooks_rows: list[dict[str, Any]] | None = None,
        hooks_rows_fn: Any = None,
        config_path: Path | None = None,
        fail_batch_write: bool = False,
    ) -> None:
        self.version = version
        self._hooks_rows = hooks_rows or []
        self._hooks_rows_fn = hooks_rows_fn
        self.config_path = config_path
        self.fail_batch_write = fail_batch_write
        self.batch_calls: list[list[dict[str, Any]]] = []

    def hooks_list(self, _cwd: Path) -> dict[str, Any]:
        rows = self._hooks_rows_fn() if self._hooks_rows_fn else self._hooks_rows
        return {"data": [{"hooks": rows}]}

    def config_batch_write(
        self,
        edits: list[dict[str, Any]],
        *,
        file_path: Path | None = None,
        reload_user_config: bool = True,
    ) -> dict[str, Any]:
        from codex_trust.rpc import CodexRpcError
        from codex_trust.trust import _upsert_toml_trust_and_orphans

        if self.fail_batch_write:
            raise CodexRpcError("simulated batchWrite unavailable")
        self.batch_calls.append(list(edits))
        target = file_path or self.config_path
        if target is not None:
            upserts: dict[str, tuple[str, str | None]] = {}
            mkt_update: tuple[str, str] | None = None
            for edit in edits:
                kp = str(edit.get("keyPath") or "")
                val = str(edit.get("value") or "")
                if kp.startswith("marketplaces.") and kp.endswith(".ref"):
                    mkt = kp[len("marketplaces.") : -len(".ref")]
                    mkt_update = (mkt, val)
                elif kp.startswith("hooks.state."):
                    rest = kp[len("hooks.state.") :]
                    field = (
                        ".trusted_content_hash"
                        if rest.endswith(".trusted_content_hash")
                        else ".trusted_hash"
                    )
                    qkey = rest[: -len(field)]
                    key = json.loads(qkey) if qkey.startswith('"') else qkey
                    prev_t, prev_c = upserts.get(key, ("", None))
                    if field == ".trusted_hash":
                        upserts[key] = (val, prev_c)
                    else:
                        upserts[key] = (prev_t, val)
            _upsert_toml_trust_and_orphans(
                target,
                upserts=upserts,
                prune_keys=set(),
                marketplace_ref_update=mkt_update,
            )
        return {"ok": True, "filePath": str(target), "reload": reload_user_config}


def test_semver_comparison_rejects_old_and_supports_prerelease() -> None:
    assert parse_semver("0.171.0") == (0, 171, 0)
    assert parse_semver("codex-cli 0.172.0-beta.2") == (0, 172, 0)
    assert semver_ge("0.171.0") is True
    assert semver_ge("0.172.0") is True
    assert semver_ge("0.180.0") is True
    assert semver_ge("0.170.9") is False


def test_hook_surface_diff_ignores_plugin_version_and_catches_hook_change(
    tmp_path: Path,
) -> None:
    old_root = _write_fake_plugin(tmp_path / "old", version="3.29.0")
    new_same = _write_fake_plugin(tmp_path / "new_same", version="3.29.1")
    new_diff = _write_fake_plugin(
        tmp_path / "new_diff", version="3.30.0", hook_note="changed"
    )

    unchanged, changes, diff_text = diff_hook_surface(old_root, new_same)
    assert unchanged is True
    assert changes == []
    assert diff_text == ""

    changed, diff_files, diff_patch = diff_hook_surface(old_root, new_diff)
    assert changed is False
    assert any(f.path == "scripts/stop.sh" for f in diff_files)
    assert "-echo same" in diff_patch
    assert "+echo changed" in diff_patch


def test_inspect_and_repair_hook_trust_fixes_modified_unmigrated_and_orphans(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex"
    cache_root = (
        codex_home
        / "plugins"
        / "cache"
        / "planning-with-files"
        / "planning-with-files"
        / "3.29.0"
    )
    _write_fake_plugin(cache_root, version="3.29.0")
    cfg_path = codex_home / "config.toml"
    key_active = "planning-with-files@planning-with-files:hooks/hooks.json:stop:0:0"
    key_orphan = "last30days@last30days-skill:hooks/hooks.json:session_start:0:0"
    cfg_path.write_text(
        f'[hooks.state."{key_active}"]\n'
        'trusted_hash = "sha256:stale"\n\n'
        f'[hooks.state."{key_orphan}"]\n'
        'trusted_hash = "sha256:dead"\n',
        encoding="utf-8",
    )
    cfg_path.chmod(0o600)

    plugin_content_hash(cache_root)
    key_unmigrated = "planning-with-files@planning-with-files:hooks/hooks.json:post:0:0"
    key_managed = "builtin:hooks.json:session_start:0:0"
    legacy_path = codex_home / ".hook-trust"
    legacy_path.mkdir(parents=True, exist_ok=True)
    from codex_trust.trust import legacy_trust_path

    leg_file = legacy_trust_path(cfg_path, "planning-with-files@planning-with-files")
    leg_file.write_text("sha256:stalelegacy\n", encoding="utf-8")

    client = _FakeRpcClient(
        version="0.172.0",
        config_path=cfg_path,
        fail_batch_write=True,
        hooks_rows=[
            {
                "source": "plugin",
                "key": key_active,
                "pluginId": "planning-with-files@planning-with-files",
                "sourcePath": str(cache_root / "hooks" / "hooks.json"),
                "eventName": "Stop",
                "trustStatus": "modified",
                "currentHash": "sha256:newhook123/sha256:bundlepart",
            },
            {
                "source": "plugin",
                "key": key_unmigrated,
                "pluginId": "planning-with-files@planning-with-files",
                "sourcePath": str(cache_root / "hooks" / "hooks.json"),
                "eventName": "PostToolUse",
                "trustStatus": "trusted",
                "currentHash": "sha256:declonly/sha256:bundlepart2",
            },
            {
                "source": "plugin",
                "key": key_managed,
                "pluginId": "planning-with-files@planning-with-files",
                "sourcePath": str(cache_root / "hooks" / "hooks.json"),
                "eventName": "SessionStart",
                "trustStatus": "managed",
                "currentHash": "sha256:manageddecl",
            },
        ],
    )

    resp_check = inspect_hook_trust(cwd=tmp_path, codex_home=codex_home, client=client)
    assert resp_check.rc == 1
    statuses = {f.key: f.status for f in resp_check.findings}
    assert statuses[key_active] is CodexHookStatus.MODIFIED
    assert statuses[key_unmigrated] is CodexHookStatus.UNMIGRATED
    assert key_managed not in statuses
    assert statuses[key_orphan] is CodexHookStatus.ORPHANED
    assert any(
        e.code is CodexTrustErrorCode.ERR_LEGACY_BASELINE_DRIFT
        for e in resp_check.errors
    )

    resp_repair = repair_hook_trust(cwd=tmp_path, codex_home=codex_home, client=client)
    assert resp_repair.rc == 0
    assert resp_repair.applied is True
    assert key_active in resp_repair.repaired_keys
    assert key_unmigrated in resp_repair.repaired_keys
    assert key_orphan in resp_repair.pruned_orphans
    assert not leg_file.exists()
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600

    repaired_cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    state_tbl = repaired_cfg["hooks"]["state"]
    assert key_orphan not in state_tbl
    assert state_tbl[key_active]["trusted_hash"] == "sha256:newhook123"
    assert state_tbl[key_active]["trusted_content_hash"] == "sha256:bundlepart"
    assert state_tbl[key_unmigrated]["trusted_hash"] == "sha256:declonly"

    # Rejects < 0.171.0 Codex
    old_client = _FakeRpcClient(version="0.170.5", config_path=cfg_path)
    old_resp = inspect_hook_trust(
        cwd=tmp_path, codex_home=codex_home, client=old_client
    )
    assert old_resp.rc == 1
    assert old_resp.errors[0].code is CodexTrustErrorCode.ERR_UNSUPPORTED_CODEX_VERSION


@pytest.mark.parametrize(
    ("asset", "event", "group", "event_name", "status"),
    [
        ("dcg-hooks.json", "PreToolUse", 0, "preToolUse", CodexHookStatus.MODIFIED),
        # heredoc-script-hint (#93) is the second PreToolUse group in the dcg asset.
        ("dcg-hooks.json", "PreToolUse", 1, "preToolUse", CodexHookStatus.UNTRUSTED),
        (
            "learn-hooks.json",
            "SessionStart",
            0,
            "sessionStart",
            CodexHookStatus.UNTRUSTED,
        ),
        ("learn-hooks.json", "Stop", 0, "stop", CodexHookStatus.MODIFIED),
    ],
)
def test_check_reports_user_hook_drift_without_approving_it(
    tmp_path: Path,
    asset: str,
    event: str,
    group: int,
    event_name: str,
    status: CodexHookStatus,
) -> None:
    assets = Path(__file__).resolve().parents[1] / "unix/home/.config/mise/assets/codex"
    declaration = json.loads((assets / asset).read_text(encoding="utf-8"))
    config = tmp_path / "config.toml"
    original = '[hooks.state."user:pre_tool_use:0:0"]\ntrusted_hash = "sha256:old"\n'
    config.write_text(original, encoding="utf-8")
    key = "user:pre_tool_use:0:0"
    row = {
        "source": "user",
        "key": key,
        "sourcePath": str(tmp_path / "hooks.json"),
        "eventName": event_name,
        "command": declaration["hooks"][event][group]["hooks"][0]["command"],
        "enabled": True,
        "trustStatus": status.value,
        "currentHash": "sha256:new",
    }
    client = _FakeRpcClient(
        config_path=config,
        hooks_rows=[row, {**row, "key": "disabled", "enabled": False}],
    )

    result = inspect_hook_trust(cwd=tmp_path, codex_home=tmp_path, client=client)
    assert result.rc == 1
    assert [(f.key, f.status, f.advisory) for f in result.findings] == [
        (key, status, False)
    ]
    assert result.errors[0].code is CodexTrustErrorCode.ERR_UNTRUSTED_OR_MODIFIED

    repair_hook_trust(cwd=tmp_path, codex_home=tmp_path, client=client)
    assert not client.batch_calls
    assert config.read_text(encoding="utf-8") == original

    row["trustStatus"] = "trusted"
    assert inspect_hook_trust(cwd=tmp_path, codex_home=tmp_path, client=client).rc == 0


def test_check_reports_advisories_across_hook_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.toml").touch()
    user = {
        "key": "personal-hook",
        "source": "user",
        "sourcePath": str(tmp_path / "hooks.json"),
        "eventName": "preToolUse",
        "command": "echo personal",
        "enabled": True,
        "trustStatus": "untrusted",
    }
    system = {**user, "key": "system-hook", "source": "system"}
    project = {**user, "key": "project-hook", "source": "project"}
    managed = {
        **system,
        "key": "managed-hook",
        "source": "mdm",
        "trustStatus": "modified",
        "isManaged": True,
    }
    client = _FakeRpcClient()
    monkeypatch.setattr(
        client,
        "hooks_list",
        lambda _cwd: {
            "data": [{"hooks": [user]}, {"hooks": [user, system, project, managed]}]
        },
    )

    result = inspect_hook_trust(cwd=tmp_path, codex_home=tmp_path, client=client)
    assert result.rc == 0
    assert not result.errors
    assert [f.key for f in result.findings] == [
        "personal-hook",
        "system-hook",
        "project-hook",
        "managed-hook",
    ]
    assert all(f.advisory for f in result.findings)


def test_update_plugin_additive_install_and_hook_diff_confirmation_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex"
    plugin_cache = (
        codex_home / "plugins" / "cache" / "planning-with-files" / "planning-with-files"
    )
    old_cache = _write_fake_plugin(plugin_cache / "3.29.0", version="3.29.0")
    old_sha = "1" * 40
    new_sha_same = "2" * 40
    new_sha_diff = "3" * 40

    cfg_path = codex_home / "config.toml"
    cfg_path.write_text(
        "[marketplaces.planning-with-files]\n"
        'source_type = "git"\n'
        'source = "https://github.com/OthmanAdi/planning-with-files.git"\n'
        f'ref = "{old_sha}"\n\n'
        '[plugins."planning-with-files@planning-with-files"]\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    cfg_path.chmod(0o600)

    monkeypatch.setattr(
        "codex_trust.trust._resolve_pin_sha",
        lambda _src, ref: (
            ref
            if len(ref) == 40
            else (new_sha_same if ref == "v3.29.1" else new_sha_diff)
        ),
    )

    def _stage(
        _src: str, sha: str, staging_root: Path, _plugin: str
    ) -> tuple[Path, str]:
        dest = staging_root / "repo"
        if sha == new_sha_same:
            _write_fake_plugin(dest, version="3.29.1")
            return dest, "3.29.1"
        _write_fake_plugin(dest, version="3.30.0", hook_note="changed")
        return dest, "3.30.0"

    hook_key = "planning-with-files@planning-with-files:hooks/hooks.json:stop:0:0"

    def _live_rows() -> list[dict[str, Any]]:
        latest = plugin_cache / (
            "3.30.0" if (plugin_cache / "3.30.0").is_dir() else "3.29.1"
        )
        return [
            {
                "source": "plugin",
                "key": hook_key,
                "pluginId": "planning-with-files@planning-with-files",
                "sourcePath": str(latest / "hooks" / "hooks.json"),
                "eventName": "Stop",
                "trustStatus": "untrusted",
                "currentHash": "sha256:curhook",
            }
        ]

    synced_calls = 0

    def _fake_sync() -> bool:
        nonlocal synced_calls
        synced_calls += 1
        return True

    # 1. Version-only update (3.29.0 -> 3.29.1): dry-run passes with zero diff; --apply installs additively + trusts + syncs PWF
    dry_resp = update_plugin(
        "planning-with-files",
        ref="v3.29.1",
        apply=False,
        cwd=tmp_path,
        codex_home=codex_home,
        client_factory=lambda h: _FakeRpcClient(
            config_path=h / "config.toml", hooks_rows_fn=_live_rows
        ),
        stage_checkout_fn=_stage,
        sync_pwf_fn=_fake_sync,
    )
    assert dry_resp.rc == 0
    assert dry_resp.hook_surface_unchanged is True
    assert dry_resp.returns[0].code is CodexTrustReturnCode.RET_DRY_RUN
    assert not (plugin_cache / "3.29.1").exists()

    app_resp = update_plugin(
        "planning-with-files",
        ref="v3.29.1",
        apply=True,
        cwd=tmp_path,
        codex_home=codex_home,
        client_factory=lambda h: _FakeRpcClient(
            config_path=h / "config.toml", hooks_rows_fn=_live_rows
        ),
        stage_checkout_fn=_stage,
        sync_pwf_fn=_fake_sync,
    )
    assert app_resp.rc == 0
    assert app_resp.applied is True
    assert app_resp.pwf_synced is True
    assert synced_calls == 1
    assert old_cache.is_dir()  # Old version preserved additively for active sessions!
    assert (plugin_cache / "3.29.1").is_dir()
    updated_cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert updated_cfg["marketplaces"]["planning-with-files"]["ref"] == new_sha_same
    assert updated_cfg["hooks"]["state"][hook_key][
        "trusted_content_hash"
    ] == plugin_content_hash(plugin_cache / "3.29.1")

    # 2. Hook diff blocks --apply unless --trust-hook-diff is passed
    blocked_resp = update_plugin(
        "planning-with-files",
        ref="v3.30.0",
        apply=True,
        trust_hook_diff=False,
        cwd=tmp_path,
        codex_home=codex_home,
        client_factory=lambda h: _FakeRpcClient(
            config_path=h / "config.toml", hooks_rows_fn=_live_rows
        ),
        stage_checkout_fn=_stage,
        sync_pwf_fn=_fake_sync,
    )
    assert blocked_resp.rc == 1
    assert blocked_resp.applied is False
    assert blocked_resp.hook_surface_unchanged is False
    assert blocked_resp.errors[0].code is CodexTrustErrorCode.ERR_HOOK_SURFACE_CHANGED
    assert (
        blocked_resp.returns[0].code is CodexTrustReturnCode.RET_CONFIRMATION_REQUIRED
    )
    assert "+echo changed" in (blocked_resp.hook_surface_diff or "")
    assert not (plugin_cache / "3.30.0").exists()

    confirmed_resp = update_plugin(
        "planning-with-files",
        ref="v3.30.0",
        apply=True,
        trust_hook_diff=True,
        cwd=tmp_path,
        codex_home=codex_home,
        client_factory=lambda h: _FakeRpcClient(
            config_path=h / "config.toml", hooks_rows_fn=_live_rows
        ),
        stage_checkout_fn=_stage,
        sync_pwf_fn=_fake_sync,
    )
    assert confirmed_resp.rc == 0
    assert confirmed_resp.applied is True
    assert (plugin_cache / "3.30.0").is_dir()
    assert (plugin_cache / "3.29.1" / ".codex-marketplace-install.json").is_file()
    assert old_cache.is_dir()


def test_codex_trust_security_toml_comments_and_rpc_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Invalid plugin_name / marketplace rejected cleanly
    bad_ident = update_plugin(
        "../escape@mkt",
        ref="v1.0.0",
        cwd=tmp_path,
        codex_home=tmp_path / "codex1",
    )
    assert bad_ident.rc == 1
    assert bad_ident.errors[0].code is CodexTrustErrorCode.ERR_INVALID_INPUT

    # 2. Invalid plugin.json version & symlink plugin_root rejected
    plug_dir = _write_fake_plugin(tmp_path / "plug", version="1.2.3")
    (plug_dir / ".codex").mkdir()
    (plug_dir / ".codex" / "hook.sh").write_text(
        "#!/bin/sh\necho ok\n", encoding="utf-8"
    )
    (plug_dir / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "sh ${CLAUDE_PLUGIN_ROOT:-/fallback}/scripts/stop.sh && sh ./.codex/hook.sh",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    surface = collect_hook_surface(plug_dir)
    assert ".codex/hook.sh" in surface  # Leading dot NOT stripped by lstrip("./")
    assert "scripts/stop.sh" in surface  # :-fallback parsed cleanly

    link_root = tmp_path / "plug_link"
    link_root.symlink_to(plug_dir)
    with pytest.raises(CodexRpcError):
        plugin_content_hash(link_root)
    with pytest.raises(CodexRpcError):
        collect_hook_surface(link_root)

    # 3. Indented TOML + quoted marketplace + trailing comments preserved without duplicates
    from codex_trust.trust import _upsert_toml_trust_and_orphans

    cfg = tmp_path / "quoted_cfg.toml"
    cfg.write_text(
        '  [marketplaces."my.mkt"] # pinned\n'
        '  source = "https://example.com/repo.git"\n'
        '  ref = "oldsha" # old tag\n\n'
        "# comment before next section\n"
        '  [hooks.state."k1"]\n'
        "  enabled = true\n"
        '  trusted_hash = "sha256:old"\n'
        "# trailing comment stays\n"
        "[other]\nx = 1\n",
        encoding="utf-8",
    )
    _upsert_toml_trust_and_orphans(
        cfg,
        upserts={"k1": ("sha256:new", "sha256:bundle")},
        prune_keys=set(),
        marketplace_ref_update=("my.mkt", "a" * 40),
    )
    written = cfg.read_text(encoding="utf-8")
    assert "# old tag" in written
    assert "# comment before next section" in written
    assert "# trailing comment stays" in written
    parsed = tomllib.loads(written)
    assert parsed["marketplaces"]["my.mkt"]["ref"] == "a" * 40
    assert parsed["hooks"]["state"]["k1"]["enabled"] is True
    assert parsed["hooks"]["state"]["k1"]["trusted_hash"] == "sha256:new"

    # 4. RPC catches missing binary OSError & drains >64KiB stderr without deadlocking
    missing_client = CodexAppServerClient(
        codex_home=tmp_path,
        codex_bin=str(tmp_path / "nonexistent-codex"),
        timeout=2.0,
    )
    with pytest.raises(CodexRpcError):
        missing_client.hooks_list(tmp_path)

    fake_srv = tmp_path / "noisy_srv.py"
    fake_srv.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stderr.write('X' * 131072)\n"
        "sys.stderr.flush()\n"
        "init_req = json.loads(sys.stdin.readline())\n"
        "sys.stdout.write(json.dumps({'id': init_req['id'], 'result': {'userAgent': 'codex-cli 0.172.0'}}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdin.readline()  # initialized\n"
        "req = json.loads(sys.stdin.readline())\n"
        "sys.stdout.write(json.dumps({'id': req['id'], 'result': {'data': [{'hooks': []}]}}) + '\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    fake_srv.chmod(0o755)
    # Run with direct python3 script shim named codex-like
    wrapper = tmp_path / "fake_codex"
    wrapper.write_text(
        f"#!/bin/sh\nexec python3 -I -B '{fake_srv}'\n", encoding="utf-8"
    )
    wrapper.chmod(0o755)
    noisy_client = CodexAppServerClient(
        codex_home=None, codex_bin=str(wrapper), timeout=5.0
    )
    assert noisy_client.hooks_list(tmp_path) == {"data": [{"hooks": []}]}

    # 5. CLI --codex-home passed cleanly to _sync_agy_pwf & plugin.json invalid version blocked
    bad_repo = tmp_path / "bad_repo"
    _write_fake_plugin(bad_repo, version="1.0")
    (bad_repo / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "../evil"}), encoding="utf-8"
    )
    (tmp_path / "codex_home2").mkdir()
    (tmp_path / "codex_home2" / "config.toml").write_text(
        '[marketplaces.p]\nsource_type = "git"\nsource = "https://example.com/p.git"\n',
        encoding="utf-8",
    )
    bad_ver_resp = update_plugin(
        "p",
        ref="a" * 40,
        cwd=tmp_path,
        codex_home=tmp_path / "codex_home2",
        stage_checkout_fn=lambda _s, _sha, _d, _p: (bad_repo, "../evil"),
    )
    assert bad_ver_resp.rc == 1
    assert bad_ver_resp.errors[0].code is CodexTrustErrorCode.ERR_INVALID_INPUT
