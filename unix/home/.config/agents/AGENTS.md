# Global Agent Rules

Portable user-level agent conventions managed by `dotfiles-v2` across macOS
hosts, Linux containers, and macOS VMs.

- **Agent communication:** For `agy <-> agy`, send full structured payloads via
  native `send_message`, discover live peer tabs dynamically by matching
  `--conversation <uuid>` in `hc ls --json` / `hc get <id> --json` (never
  hardcode stale tab IDs across `agy-hc` restarts), and send a 1-line
  `hc send <tab-id> "Wake doorbell: ..."` (`<= 220` chars) only when
  `status.state` is `"idle"` or `"done"` (omit `--if idle` when `"done"`). Never
  send payloads via `hc send`, and never touch `ANTIGRAVITY_CSRF_TOKEN`.
- **Containers:** Always pass `-e container=docker`; never pass `--privileged`.
- **Pre-ship code review gate:** Before pushing, fast-forward merging into
  `main`/`master`, or opening a pull request on any non-documentation change,
  run `/code-review:code-review` (`+ code-review-commons`, fanning out across
  read-only slices when diffs exceed ~1,000 lines), fix confirmed `CRITICAL`,
  `HIGH`, and `MEDIUM` findings, and record the `HEAD` receipt via
  `python3 -I -B ~/.gemini/config/hooks/agy-code-review-guard.py --record --summary "..."`.
  Use `/code-review:pr-code-review` on open GitHub pull requests.
- **Privacy and git hygiene:** Stage changed files explicitly by name (never
  `git add -A` or `git add .`). Never commit secrets, tokens, private
  hostnames (use `pc` or "remote Linux host"), or local `/Users/...` home
  paths. Never enable `set -x` / `bash -x` or print environment variables in
  shells where tokens or credentials are loaded.
- **Tooling scope and ecosystem:** Keep developer tools user-global under
  `~/.config/mise/config.toml` unless a tool is strictly project-specific.
  Prefer `mise`, `hk`, `fnox`, `aube`, and `jdx` ecosystem tools, native compiled
  `hk` builtins (`Rust`, `Go`, `Haskell`, `Pkl`), and `mise fmt` for mise
  configs.
- **Schemas and verification:** Declare public `$schema` / `#:schema` headers on
  supported config files. Prefer `mise doctor project` and `hk` checks/hooks
  over unit tests for upstream tools, git hooks, or config/setup verification
  (reserve unit tests for project code), and always inspect current docs or
  source before acting.
- **Research gate & error resolution:** Run multi-source research (local
  offline repos/docs, session/git history, OpenWiki, Context7, Exa, Firecrawl,
  Last30Days, and GitHub issues/PRs/discussions/code) before proposing custom
  builds, and report every route run plus any failed route (`Research checked:`).
  Root-cause and fix (or report exact remediation for) every tool, MCP, proxy,
  or permission failure first rather than ignoring or bypassing it.
- **Non-interactive runs and watchdogs:** Pass explicit non-interactive flags
  (`MISE_YES=1`, `--yes` / `-y`) to long-running commands, and pair any
  backgrounded build, sync, or upgrade task with a bounded `schedule` watchdog.
- **Reporting peer messages & receipt confirmation:** Always request explicit
  receipt confirmation when sending a message to another agent. When reporting
  peer messages to the owner, name the target as `tab-name (id)`, state the
  channel (`native send_message`, plus `hc send` idle doorbell if sent), and
  clearly distinguish **dispatched (receipt pending)** from **receipt
  confirmed**.
- **OpenWiki search routing:** Use the `openwiki_search` / `openwiki_read` MCP
  tools for workspace-scoped queries (`--workspace` is not an `openwiki` CLI
  flag).

## Prefer native hk builtins and compiled tools

Before writing a custom Python or shell check, search `hk builtins` for a native
`Builtins.*` step and prefer compiled native tools (`Rust`, `C++`, `C`, `Zig`,
`Go` — such as `betterleaks`, `rg`, `ruff`, `ty`, `tombi`, `jq`, `mise`,
`agnix`, `jactionlint`, `zizmor`, `typos`, `hk util`) when available in the
toolchain.
