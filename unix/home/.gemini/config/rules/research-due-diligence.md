---
trigger: always_on
description: Require multi-source research due diligence before custom implementations and root-cause diagnosis instead of dismissing tool, MCP, or auth errors.
---

# Research Due Diligence & Zero Dismissed Errors

1. **Research before proposing or building**: before proposing custom scripts,
   workarounds, or architectural changes, search local offline docs/repos, git
   and session history (including AgentsView when running), OpenWiki
   (`~/.openwiki`, `~/dev/kb.noindex/`), Context7, Exa, Firecrawl, Last30Days,
   and GitHub issues, PRs, discussions, and source code.
2. **Verify against primary sources & report provenance**: cite primary docs,
   source lines, or verified outputs and end non-trivial recommendations with a
   `Research checked:` summary listing every tool/source actually run and any
   failed or blocked route.
3. **Never dismiss or silently work around tool, MCP, or auth failures**: when an
   MCP server, CLI, proxy, or permission guard fails (`401`, invalid/revoked
   key, blanket deny rule, missing binary), stop and root-cause it first instead
   of glossing over it or falling back without diagnosis:
   - Check secret health with safe metadata checks (`mise run secret:doctor` or
     `fnox check` — never run `fnox get`/`exec`/`export`).
   - Compare running session start time and active `fnox proxy` PID against
     `fnox/config.toml` and `mcp_config.json` mtimes (stale proxies miss newer
     deterministic placeholders and require tab resume).
   - Compare `settings.json` `permissions.deny` against `guards.toml` so
     blanket command denies do not block safe subcommands (`fnox proxy run`,
     `fnox check`).
   - Fix or report the exact root cause and remediation to the user before
     continuing.
