# dotfiles-v2 Roadmap

- [x] **Step 0:** Bare repo (`ray-manaloto/dotfiles-v2`), `mise 2026.11.12`,
  `hk-config`, `renovate-config`, clean `AGENTS.md`, `unix/home/` root.
- [x] **Step 1:** Bare Ubuntu 26.10 container (`docker/ubuntu/Dockerfile`,
  pinned `ca-certificates`, `curl`, `git`, `mise v2026.11.12`, non-privileged,
  `-e container=docker`) + `container:up` / `container:sync` on `pc`.
- [x] **Step 1.5:** Mac `agy` context bloat cleanup and checkpoint (redundant
  plugins disabled, duplicate plugin folder removed, 47 redundant skills cleaned
  from `~/.agents/skills/` and `~/.claude/skills/`).
- [x] **Step 1.6:** Day-1 foundation overhaul across all 3 live environments
  (`pc` Ubuntu 26.10 `amd64`, Mac Docker Desktop Ubuntu 26.10 `arm64`, Mac Tart
  `dotfiles-v2-macos` `arm64`): tool-free root `mise.toml`, global tools in
  `unix/home/.config/mise/`, public config schemas, 100% native compiled `hk.pkl`
  with `mise fmt`, worktree `planning-with-files`, and live `env:doctor`.
- [x] **Step 1.7:** Direct Tailscale SSH (`tag:dev-env`) across all 3 live
  environments (`dotfiles-v2-pc`, `dotfiles-v2-mac`, `dotfiles-v2-vm`),
  non-root userspace `tailscaled` in Ubuntu containers, Homebrew `tailscaled`
  in Tart VM, `mise run env:ssh`, and live Tailscale SSH checks in `env:doctor`.
- [ ] **Step 2:** Subscriptions-only AI harnesses added and live-verified across
  all 3 environments (`pc`, `mac`, `vm`) before C++ compilers:
  - [x] 2.1 Claude Code (`agent-secrets` OAuth token mount)
  - [x] 2.2 OpenCode (free zero-retention model)
  - [x] 2.3 ZCode (`npm:zcode-app-cli`)
  - [ ] 2.4 Antigravity CLI (`agy`, `Orchestrator (Neon)` default)
  - [ ] 2.5 Codex (`--device-auth`) + `ray-manaloto/skills` + `[dotfile_groups]`
- [ ] **Step 3:** Declarative C++26 core (`mise.ubuntu.toml`: pinned latest
  GCC 16, Clang 23, `lld`/`lldb`/`libc++`, `gdb`, `valgrind`, `cppcheck`,
  `cmake`, `ninja`, `vcpkg`, `gcovr`) + C++26 smoke test, followed by optional
  extras (`clang-p2996`, IWYU, CUDA/NVPTX).
- [ ] **Step 4:** Tighten GitHub ruleset and CI gates after Step 2 and Step 3
  pass cold-build verification.
- [ ] **Follow-up (Host Homebrew -> `mise` audit):** Migrate clean prebuilt
  Mac host leaves (`mas` -> `aqua:mas-cli/mas`, `ruby` -> `core:ruby`, `viu`
  -> `github:atanunq/viu`, `dockerfilegraph` ->
  `github:patrickhoefler/dockerfilegraph`) to global `mise` config while keeping
  native C++/system libraries and Casks in Homebrew.
- [ ] **Follow-up (`packslip v1.11.5`):** Switch `tasks/vm/up` from
  `packslip verify --pin` back to `packslip install github.com/jdx/mise` once
  `jdx/packslip` `v1.11.5` (PR `#337`, fixing `#346`) releases.
- [x] **Follow-up (Local `vercel/board` multi-harness messaging + `jdx` skills):**
  Package `npm:@vercel/board`, `pitchfork`, `aube`, `communique`, and `usage`
  globally via `mise`, sync `jdx` + `vercel-board` skills across all harnesses
  via `bootstrap:skills`, and run a `pitchfork`-supervised local/tailnet board
  shared across host, `pc`, `mac`, and `vm`.
- [ ] **Follow-up (`knowledge-base-v2` container readiness):** Evaluate running
  `knowledge-base-v2` inside `dotfiles-v2` containers (`jq` in container toolset,
  `:ro` `~/.config/agents/private-patterns.txt` mount, persistent `kb.noindex` /
  `pins.json` mounts, and `fnox` proxy forwarding).
- [x] **Follow-up (Clean-room `herdr_roll` & post-review verification gate):**
  Shipped clean-room 6-harness rolling-restart engine (`herdr_roll`, `e73ce7b`,
  `5b5513a`), `repotools trust` worktree wiring, and ordered 2-stage
  post-review verification gate (`2eb3410`).
- [ ] **Follow-up (Herdr rolling restarts & issue-mapped PWF workflow):**
  - [ ] [#4](https://github.com/ray-manaloto/dotfiles-v2/issues/4) Cut over
    global `roll:*` `mise` tasks and Herdr skills from legacy `dotfiles` to
    `dotfiles-v2` (`PLAN_ID` / worktree slug: `herdr-roll-global-cutover`).
  - [ ] [#5](https://github.com/ray-manaloto/dotfiles-v2/issues/5) Fix
    `OpenCodeAdapter` session-ID resolution for `opencode --standalone` tabs
    without `-s` (`PLAN_ID` / worktree slug: `herdr-roll-opencode-sid`).
  - [ ] [#7](https://github.com/ray-manaloto/dotfiles-v2/issues/7) Live
    `--apply` verification across `claude`, `hermes`, `zcode`, and full
    reboot/wave flows (`PLAN_ID` / worktree slug: `herdr-roll-live-verify`;
    blocked by `#4`, `#5`).
  - [ ] [#8](https://github.com/ray-manaloto/dotfiles-v2/issues/8) Add
    nanosecond-timestamped verification evidence, process `lstart`/hook-load
    proof to `roll:verify`, and dialog triage docs (`PLAN_ID` / worktree slug:
    `herdr-roll-verify-proof`; blocked by `#4`).
  - [ ] [#6](https://github.com/ray-manaloto/dotfiles-v2/issues/6) Enforce
    GitHub issue -> PWF `PLAN_ID` -> git worktree mapping and nanosecond
    verification evidence (`PLAN_ID` / worktree slug: `worktree-issue-pwf-map`).
