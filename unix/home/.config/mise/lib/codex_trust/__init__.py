"""Schema-first Codex plugin hook-trust inspection, repair, and pinned updates."""

from __future__ import annotations

from codex_trust.rpc import (
    MIN_CODEX_VERSION,
    CodexAppServerClient,
    CodexRpcError,
    parse_semver,
    semver_ge,
)
from codex_trust.trust import (
    collect_hook_surface,
    diff_hook_surface,
    inspect_hook_trust,
    legacy_trust_path,
    plugin_content_hash,
    repair_hook_trust,
    update_plugin,
)

__all__ = [
    "MIN_CODEX_VERSION",
    "CodexAppServerClient",
    "CodexRpcError",
    "collect_hook_surface",
    "diff_hook_surface",
    "inspect_hook_trust",
    "legacy_trust_path",
    "parse_semver",
    "plugin_content_hash",
    "repair_hook_trust",
    "semver_ge",
    "update_plugin",
]
