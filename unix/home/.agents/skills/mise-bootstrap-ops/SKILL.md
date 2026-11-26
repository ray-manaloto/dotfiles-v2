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
  response cap), copy digest-pinned `ghcr.io/jdx/mise` (`2026.11.14`) in Docker
  and run `packslip install github.com/jdx/mise --version ... --pin <pin> --system --bin-dir /usr/local/bin --force`
  on macOS.
- `[hooks] postinstall` runs `bootstrap:skills` and `host:harden-bins` (`chmod a-w`
  on compiled Mach-O/ELF binaries inside `$MISE_DATA_DIR/installs` while leaving
  parent directories `0755` for `mise` upgrades/pruning), paired with
  `dcg` (`~/.config/dcg/{config.toml,packs/mise-protect.yaml}`) across harnesses.
- Root `mise.toml` declares `[secrets.fnox] profile = "infra"` so infra tasks
  (`container:up`, `vm:up` with `#MISE secrets=["TAILSCALE_API_KEY"]`) resolve
  secrets natively; headless Linux/VM use `config.headless.toml` without Keychain.
- Keep claude.ai connectors off unless needed and prefer CLI/SDK for research tools (CLI-first skills: `find-docs`, `exa-search`,
  `firecrawl`, `apify-ultimate-scraper`, `toolchain-update-review`, plus the
  official `agentsview-finding-history` installed by `bootstrap:skills` via
  `agentsview skills install --harness agents --force`),
  preserve `NODE_EXTRA_CA_CERTS` under `fnox proxy run` for Node 26 CLIs, and on
  Env 2-4 guests use `env -u ALL_PROXY -u all_proxy HTTPS_PROXY=http://127.0.0.1:1055 NODE_USE_ENV_PROXY=1`.

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

## 7. Phase 10-11 Lessons: `packslip --system`, Login Parity & Shim-Mode Doctor

- **`packslip --system` directory ownership (`src/install_fs.rs:382`):** On macOS,
  `packslip install ... --system --bin-dir /usr/local/bin` rejects non-root or
  group/world-writable prefix directories. Normalize `/usr/local` and
  `/usr/local/bin` to `root:wheel` (`0755`) via
  `sudo install -d -o root -g wheel -m 0755 /usr/local/bin && sudo chown root:wheel /usr/local /usr/local/bin && sudo chmod 0755 /usr/local /usr/local/bin`
  before invoking `packslip install ... --system` in `tasks/vm/up`.
- **Ubuntu container login vs non-login parity:** `config.container.toml`
  appends `. /etc/profile.d/dotfiles-v2-env.sh && . /etc/profile.d/agent-secrets.sh`
  to `~/.profile` (`position = "append"`) alongside prepending at line 1 of
  `~/.bashrc` above Ubuntu's interactive guard (and `/etc/bash.bashrc`), so login
  and non-login shells resolve identical `MISE_*`, `PATH`, and secrets.
- **Tart VM `MISE_ENV=vm`, `~/.zprofile`, and `brew:tailscale` LaunchAgent:**
  `assets/vm/vm-env.sh` exports `MISE_ENV=vm` and prepends Homebrew,
  `/usr/local/bin`, `~/.local/bin`, `~/.opencode/bin`, and
  `~/.local/share/mise/shims` in deterministic order. `config.vm.toml` appends
  `vm-env.sh` to `~/.zprofile` and `~/.bash_profile` (`position = "append"` so
  it stays ahead of `/etc/zprofile`'s `path_helper`), prepends it to `~/.zshrc`
  and `~/.bashrc`, `zsh/.zprofile` (symlinked to `~/.config/zsh/.zprofile`) sources
  `vm-env.sh` when readable, and `brew:tailscale` runs non-root under `dev.mise.tailscaled`
  LaunchAgent (`~/.local/state/tailscale/tailscaled.sock`).
- **Shim-mode `mise doctor project` (`mise-doctor-clean`) PATH normalization:**
  When `mise doctor` runs inside a shim-mode `mise` task (`MISE_SHELL` and
  `__MISE_DIFF` unset while `shims` is on `PATH`), `_normalize_path_for_mise_doctor`
  in `tasks/py/doctor_checks.py` strips `*/git-core` (`GIT_EXEC_PATH`) plus
  `mise`-injected `installs/`, `command-wrappers/bin`, and repo `.venv/bin`
  prefixes so `mise -C ~ doctor --json` sees real shell PATH ordering without
  masking genuine misconfigurations (`~/.local/bin` or `/opt/homebrew/bin` ahead
  of `shims`).
- **Parallel multi-target builds & BuildKit SBOM/provenance:**
  `tasks/container/up --target all` and `tasks/env/doctor --target all` run
  `mac`, `pc` (and `vm` for doctor) concurrently with per-target log files and
  fail-closed status aggregation; `container:up` passes
  `docker buildx build --load --sbom=true --provenance=mode=max --metadata-file`
  to persist BuildKit provenance and SBOM attestation metadata atomically under
  `$XDG_STATE_HOME/dotfiles-v2/inventory/buildx-<target>.metadata.json`.
