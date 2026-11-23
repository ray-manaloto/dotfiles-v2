# dotfiles-v2

Clean-room `mise`-managed dotfiles, multi-environment container/VM harnesses,
and C++26 workstation setup.

- **Layout:** Dockerfiles under `docker/`, managed home files and `mise`
  configuration (`unix/home/.config/mise/{config*.toml,assets/,tasks/,mise.lock}`)
  under `unix/home/`, orchestration tasks under `tasks/`, and live
  cross-environment diagnostics under `mise/doctor/`. Never put code or
  Dockerfiles under `docs/`.
- **Tool scope:** Root `mise.toml` stays tool-free (`no-project-tools` doctor
  check). User/global tools and skills live in
  `unix/home/.config/mise/config.toml` and `unix/home/.config/mise/mise.lock`.
- **Day-1 3 environments:** `pc` (`ssh://pc`, `ubuntu:26.10` `amd64`), `mac`
  (`docker --context desktop-linux`, `ubuntu:26.10` `arm64`), and `vm` (Tart
  `dotfiles-v2-macos` `arm64`). Always run containers with `-e container=docker`
  and never `--privileged`. Never touch `cs-guest`.
- **Packslip & Option A `mise bootstrap` architecture:**
  - `docker/ubuntu/Dockerfile` installs `mise 2026.11.12` via pinned
    `ghcr.io/jdx/packslip:1.11.4` (`--pin ps1_nlhmwtfeufglxv5myvwvronk7a`),
    then runs a 2-phase `mise bootstrap` with BuildKit `apt` / `downloads-cas`
    (`download_cache = true`, `10GiB`) caches and optional `acu-trial`
    `ubuntu-cache` (`DOTFILES_APT_CACHE`, auto-probed on `pc:3142` via `/healthz`).
  - Phase 1a (`root`) runs `container-prune-ubuntu` (removes stock `ubuntu`
    1000:1000) then `container,linux` (creates `dev` 1000:1000, installs pinned
    `apt` packages, creates directories and `/etc/profile.d/*` files). Phase 1b
    (`dev`) installs locked `[tools]` (including `tailscale = "1.104.7"` with
    `os = ["linux"]`) and runs `[tasks.bootstrap]` (`bootstrap:claude`,
    `bootstrap:opencode`).
  - Container sync (`container:up` / `container:sync`) and Tart VM
    (`vm:up` / `vm:sync`) run Option A
    `mise bootstrap --from <repo> --from-dir <checkout> --config-dir unix/home/.config/mise`,
    linking `~/.config/mise` directly to `<checkout>/unix/home/.config/mise`.
- **Tailscale SSH & VM boundaries:** Pin Tailscale `>= 1.104.4` (Tailworm
  `TS-2026-014` forwarding fix). Containers run non-root
  `tailscaled --tun=userspace-networking` (`--init`, state dir `0700`) via
  `mise`'s Linux `aqua:tailscale/tailscale` shim and accept SSH only as `dev`.
  Source `/etc/profile.d/{dotfiles-v2-env,agent-secrets}.sh` at line 1 of
  `~/.bashrc` (via prepended `line` dotfile edit above Ubuntu's interactive
  guard) so non-login Tailscale SSH (`SSH_SOURCE_BASHRC`) matches login shells
  without duplicating `PATH` entries. The Tart `vm` pours open-source
  `brew:tailscale` via `mise` and runs non-root `tailscaled --tun=userspace-networking`
  as `[bootstrap.macos.launchd.agents.tailscaled]` (`dev.mise.tailscaled` in
  `gui/$(id -u)` with socket `~/.local/state/tailscale/tailscaled.sock`); it
  cannot run Docker (`kern.hv_support=0`).
- **Parallel worktrees & `planning-with-files`:** Create isolated task
  worktrees with `mise run worktree:new <slug>` (remove with `worktree:rm`).
  Each worktree pins its own `PLAN_ID` inside git-ignored `mise.local.toml` and
  stores git-ignored plan files under `.planning/<PLAN_ID>/` inheriting root
  `.mode` (`autonomous`). `agy-pwf-guard.py` (`PreToolUse`) enforces an active
  `PLAN_ID` before editing repo files in `.mode`-adopted repositories.
  `ROADMAP.md` stays the sole-writer tracked milestone index on `main`.
- **Agent communication:** Use native `agy <-> agy` commands only between
  Antigravity sessions; never use `hc` for `agy <-> agy`. Queue all OpenWiki /
  `knowledge-base-v2` addition requests to `knowledge-base-lead-agy` via native
  `agy` messaging; never mutate `knowledge-base-v2` directly from `dotfiles-v2`.
- **Schemas, hooks & verification:** Declare public `$schema` / `#:schema`
  headers on config files. Prefer `mise` / `jdx` ecosystem tools, native
  compiled `hk` builtins, `mise fmt` for mise configs, and `mise doctor project`
  plus `hk check --all` over unit tests for setup checks. Always invoke
  `#USAGE` file tasks inside `[doctor.checks]` via `mise ... run <task>` rather
  than direct script paths so `mise` parses flags. In `hk.pkl`, pair
  `Builtins.mise` + `Builtins.mise_config_dir` for all mise configs and exclude
  them from `tombi-format` while keeping `tombi lint --error-on-warnings`.
- **`mise` `[dotfiles]` & guest sync pitfalls:** Always anchor `dotfiles.root`
  in `unix/home/.config/mise/config.toml` via symlink-resolving Tera syntax
  (`"{{ config_source | canonicalize | dirname }}/../.."`), pass
  `--force-dotfiles` on sync, use `line` (never `block`) for
  `position = "prepend"`, remove any non-symlink `~/.config/mise` before
  Option A `--config-dir` linking, and ensure `tart exec` guest scripts trust
  repo configs (`MISE_TRUSTED_CONFIG_PATHS`). Use `conf.d/<topic>/mise.toml`
  folder fragments (not single-file `conf.d/*.toml`) for additive
  `[task_config].includes`. Use only pinned standalone `mise` installed via
  `packslip` (`/usr/local/bin/mise` on VM/containers, `~/.local/bin/mise` on
  host) instead of Homebrew `mise`; uninstall any base-image Homebrew `mise`
  formula (`brew uninstall --force mise`) so `/opt/homebrew/bin/mise` stays
  absent.
- **Harnesses & container scripts:** Install AI CLIs (`claude`, `codex`, `agy`,
  `opencode`) via native installers only (`[tasks.bootstrap]`, blocked with
  `mise` in `disable_tools` & `auto_install_disable_tools`). Omit `--bare` when
  authenticating Claude with `CLAUDE_CODE_OAUTH_TOKEN`, redirect `</dev/null`
  on interactive/agent CLIs inside `bash -s` / `docker exec -i` / `tart exec -i`
  heredocs, and avoid creating the container user home directory or `/opt/mise`
  subtrees as `root` before `dev (1000:1000)` exists. Run headless OpenCode checks with
  `opencode --standalone run --archived`, keep `"update": "disable"` per
  `https://opencode.ai/config.json`, and default free runs to zero-retention
  `opencode/deepseek-v4.1-flash-free`.
- **Research & interactive decisions:** Search prior history (Claude/Codex/AGY
  transcripts, Git history, AgentsView when running), OpenWiki (`~/.openwiki`
  and `~/dev/kb.noindex/`), Context7, Exa, Firecrawl, Last30Days, and GitHub
  before proposing architecture; verify claims against primary docs/source,
  report every tool run and failed route, root-cause and fix every
  tool/MCP/auth error first rather than dismissing it, and ask decisions
  interactively via `ask_question` (recommended option first with concise
  pros/cons).
- **Prototype workflow & privacy:** Direct push to `main` during prototyping,
  guarded locally by `hk check --all` (includes `betterleaks`). Stage files
  explicitly by name; never `git add -A` or `git add .`. Never commit secrets,
  tokens, private hostnames (use `pc` or "remote Linux host"), or local OS
  home directory paths.
