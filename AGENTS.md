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
  plus `hk check --all` over unit tests for setup checks. In `hk.pkl`, pair
  `Builtins.mise` + `Builtins.mise_config_dir` for all mise configs and exclude
  them from `tombi-format` while keeping `tombi lint --error-on-warnings`.
- **`mise` `[dotfiles]` & guest sync pitfalls:** Always anchor `dotfiles.root`
  with Tera syntax (`"{{ config_root }}/unix/home"`), pass `--force-dotfiles` on
  sync, and ensure `tart exec` guest scripts trust repo configs (`mise trust
  --all` / `MISE_TRUSTED_CONFIG_PATHS`) and prepend shims to `PATH`.
- **Harnesses & container scripts:** Install AI CLIs via native installers only
  (blocked in global `mise` settings). Omit `--bare` when authenticating Claude
  with `CLAUDE_CODE_OAUTH_TOKEN`, redirect `</dev/null` on interactive/agent
  CLIs inside `bash -s` / `docker exec -i` / `tart exec -i` heredocs, and list
  parent home directories explicitly in `install -d -o dev -g dev` inside
  Dockerfiles.
- **Prototype workflow & privacy:** Direct push to `main` during prototyping,
  guarded locally by `hk check --all` (includes `betterleaks`). Stage files
  explicitly by name; never `git add -A` or `git add .`. Never commit secrets,
  tokens, private hostnames (use `pc` or "remote Linux host"), or local OS
  home directory paths.
