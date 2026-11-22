# dotfiles-v2

Clean-room `mise`-managed dotfiles, multi-environment container/VM harnesses,
and C++26 workstation setup.

- **Layout:** Dockerfiles under `docker/`, managed home files under
  `unix/home/`, mise file tasks under `tasks/`, and live cross-environment
  diagnostics under `mise/doctor/`. Never put code or Dockerfiles under `docs/`.
- **Tool scope:** Root `mise.toml` stays tool-free (`no-project-tools` doctor
  check). User/global tools and skills live in `unix/home/.config/mise/config.toml`
  and `unix/home/.config/mise/mise.lock`.
- **Day-1 3 environments:** `pc` (`ssh://pc`, `ubuntu:26.10` `amd64`), `mac`
  (`docker --context desktop-linux`, `ubuntu:26.10` `arm64`), and `vm` (Tart
  `dotfiles-v2-macos` `arm64`). Always run containers with `-e container=docker`
  and never `--privileged`. Never touch `cs-guest`.
- **Parallel worktrees & `planning-with-files`:** Create isolated task
  worktrees with `mise run worktree:new <slug>` (remove with `worktree:rm`).
  Each worktree pins its own `PLAN_ID` inside git-ignored `mise.local.toml` and
  stores git-ignored plan files under `.planning/<PLAN_ID>/` inheriting root
  `.mode` (`autonomous`). `ROADMAP.md` stays the sole-writer tracked milestone
  index on `main`.
- **Agent communication:** Use native `agy <-> agy` commands only between
  Antigravity sessions; never use `hc` for `agy <-> agy`.
- **Schemas, hooks & verification:** Declare public `$schema` / `#:schema`
  headers on config files. Prefer `mise` / `jdx` ecosystem tools, native
  compiled `hk` builtins, `mise fmt` for mise configs, and `mise doctor project`
  plus `hk check --all` over unit tests for setup checks.
- **Prototype workflow & privacy:** Direct push to `main` during prototyping,
  guarded locally by `hk check --all` (includes `betterleaks`). Stage files
  explicitly by name; never `git add -A` or `git add .`. Never commit secrets,
  tokens, private hostnames (use `pc` or "remote Linux host"), or local OS
  home directory paths.
