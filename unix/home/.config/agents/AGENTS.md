# Global Agent Rules

Portable user-level agent conventions managed by `dotfiles-v2` across macOS
hosts, Linux containers, and macOS VMs.

- **Agent communication:** Use native `agy <-> agy` commands between Antigravity
  sessions; never use `hc` for `agy <-> agy`.
- **Containers:** Always pass `-e container=docker`; never pass `--privileged`.
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
  supported config files. Prefer `mise doctor project` and `hk` checks/hooks over
  unit tests for setup verification, and always inspect current docs or source
  before acting.
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
  channel (native `send_message`), and clearly distinguish **dispatched (receipt
  pending)** from **receipt confirmed**.
- **OpenWiki search routing:** Use the `openwiki_search` / `openwiki_read` MCP
  tools for workspace-scoped queries (`--workspace` is not an `openwiki` CLI
  flag).
