---
name: mise-bootstrap-ops
description: >-
  Use when authoring or debugging Dockerfiles, Tart macOS VM provisioning, or
  mise bootstrap configurations (accounts, dotfiles, lockfiles, packslip
  verification, and macOS LaunchAgents) in dotfiles-v2.
---

# `mise` Bootstrap Operations & Verified Checklists

## 1. Linux Container Account & Phase 1a Ordering

- Run `container-prune-ubuntu` before `container` creates `dev (1000:1000)`
  (`mise` applies `Present` accounts before `Absent` accounts).
- Omit `[bootstrap.groups.<user>]` on Ubuntu when removing a user whose
  primary group matches its username (`USERGROUPS_ENAB yes` in `/etc/login.defs`
  deletes the eponymous group during `userdel --remove`, causing `mise`'s
  pre-planned `groupdel` to exit `6`).
- Export `HOME=/tmp/phase1-home`, `XDG_*`, and `MISE_*` **before** the first
  `mise` invocation (even `mise --version`) in `root` Phase 1a so the container
  user's `$HOME` is never pre-created owned by `root:root` before
  `useradd --create-home`.

## 2. Option A (`--from --from-dir --config-dir`) & Lockfile Safety

- Anchor `dotfiles.root = "{{ config_source | canonicalize | dirname }}/../.."`.
- Use `line` (never `block`) for `position = "prepend"` dotfile edits (`mise`
  rejects `position = "prepend"` on `block`).
- Pass `MISE_LOCKED=1` in guest bootstraps and pre-lock package `provenance`
  tables in global `mise.lock` (`config.toml`'s sibling lockfile) so guest
  `bootstrap` runs never dirty symlinked mirror checkouts (`/workspace` /
  `~/dev/dotfiles-v2`).
- Run `chmod 0644 /tmp/phase1b/mise.lock` after `COPY` in Docker (`mise lock`
  writes `0600` mode).
- Remove any non-symlink `~/.config/mise` before Option A `--config-dir`
  linking, and source profile env scripts at line 1 of `~/.bashrc` above
  Ubuntu's interactive guard for non-login Tailscale SSH parity.

## 3. Binary Verification (`packslip v1.11.5` & Digest-Pinned Docker)

- With `jdx/packslip` `v1.11.5` (PR `#347`, fixing `#346`'s 8 MiB `/releases`
  response cap), copy digest-pinned `ghcr.io/jdx/mise` (`2026.11.13`) in Docker
  and run `packslip install github.com/jdx/mise --version ... --pin <pin> --system --bin-dir /usr/local/bin --force`
  on macOS.

## 4. Non-Root macOS LaunchAgents

- Prefer `"brew:<formula>"` + `[bootstrap.macos.launchd.agents.<name>]` over
  `brew services` or `sudo`.
- LaunchAgents run outside login shells without `mise` `PATH`; use absolute
  paths, `gui/$(id -u)` domain checks, and `~/.local/state/tailscale/tailscaled.sock`.

## 5. Multi-Environment Checks, Worktree Sync & Headless Harnesses

- Use stdlib `python3` instead of `jq` inside minimal Ubuntu container checks
  (`pc`, `mac` omit `jq`).
- Place explanatory comments on a separate line above bare `# hadolint
  ignore=<RULE>` lines (`hadolint` parses every token after `=` as a rule ID).
- After running `mise run host:sync-gemini` from a task worktree, re-run
  `mise -C <main-repo> run host:sync-gemini` after merging to `main` so
  pre-push `host-gemini-sync` sees symlinks pointing at `main`.
- Keep container secret files (such as `ZCODE_CREDENTIAL_SECRET_FILE`) out of
  Docker image layers via `MISE_CONFIG_DIR != "/tmp/phase1b"` runtime guards
  on persistent named volumes (`~/.zcode`).
- For headless `zcode-app-cli`, invoke `zcode login --oauth --no-browser`
  without a positional `zai` arg (`classifyZaiOAuthInvocation`) and pass
  `--prompt=...` rather than `-p --model ...`.
- Skip unit tests for upstream CLI behavior, git hooks, or doctor shell
  snippets; verify toolchain/config invariants via `mise doctor project` and
  `hk check` (reserve unit tests for project code).

## 6. Global Skills Sync, Lockfiles & Tailscale PacketFilter

- Keep `~/.claude/skills` a real directory (remove any symlink first) and
  assert `claude_dir.resolve() != agents_dir.resolve()` inside `bootstrap:skills`
  before `mise skills sync --global --prune` so `../../.agents/skills/<name>`
  links never self-loop.
- Run `MISE_CONFIG_DIR="$PWD" mise lock --global && chmod 0644 mise.lock` inside
  the managed `.config/mise/` directory so `linux-x64`, `linux-arm64`, and `macos-arm64`
  remain locked (`mise skills sync --global` cannot be combined with `--dir`).
- Explicit relative `source` paths in `[dotfiles]` resolve from `config.toml`'s
  directory (`.config/mise/`), while omitted `source` resolves under
  `dotfiles.root`.
- Anchor root ignores (`/.agents/`, `/.claude/`) in `.gitignore` so managed
  `.agents/` skill directories stay trackable.
- When `tailscale ping` succeeds (`1ms`) while `curl --proxy http://127.0.0.1:1055`
  or `tailscale nc` times out, inspect `tailscale debug netmap | jq '.PacketFilter'`
  on both peers for `tag:dev-env` ACL rules.
