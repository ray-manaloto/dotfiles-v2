# Global Agent Rules

Portable user-level agent conventions managed by `dotfiles-v2` across macOS
hosts, Linux containers, and macOS VMs.

- **Agent communication:** Use native `agy <-> agy` commands between Antigravity
  sessions; never use `hc` for `agy <-> agy`.
- **Containers:** Always pass `-e container=docker`; never pass `--privileged`.
- **Privacy and git hygiene:** Stage changed files explicitly by name (never
  `git add -A` or `git add .`). Never commit secrets, tokens, private
  hostnames (use `pc` or "remote Linux host"), or local `/Users/...` home
  paths.
- **Tooling scope and ecosystem:** Keep developer tools user-global under
  `~/.config/mise/config.toml` unless a tool is strictly project-specific.
  Prefer `mise`, `hk`, `fnox`, `aube`, and `jdx` ecosystem tools, native compiled
  `hk` builtins (`Rust`, `Go`, `Haskell`, `Pkl`), and `mise fmt` for mise
  configs.
- **Schemas and verification:** Declare public `$schema` / `#:schema` headers on
  supported config files. Prefer `mise doctor project` and `hk` checks/hooks over
  unit tests for setup verification, and always inspect current docs or source
  before acting.
