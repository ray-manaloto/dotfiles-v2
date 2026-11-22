# dotfiles-v2 Roadmap

- [x] **Step 0:** Bare repo (`ray-manaloto/dotfiles-v2`), `mise 2026.11.12`,
  `hk-config`, `renovate-config`, clean `AGENTS.md`, `unix/home/` root.
- [x] **Step 1:** Bare Ubuntu 26.10 container (`docker/ubuntu/Dockerfile`,
  pinned `ca-certificates`, `curl`, `git`, `mise v2026.11.12`, non-privileged,
  `-e container=docker`) + `container:up` / `container:sync` on `pc`.
- [x] **Step 1.5:** Mac `agy` context bloat cleanup and checkpoint (redundant
  plugins disabled, duplicate plugin folder removed, 47 redundant skills cleaned
  from `~/.agents/skills/` and `~/.claude/skills/`).
- [ ] **Step 1.6:** Day-1 foundation overhaul across all 3 live environments
  (`pc` Ubuntu 26.10 `amd64`, Mac Docker Desktop Ubuntu 26.10 `arm64`, Mac Tart
  `dotfiles-v2-macos` `arm64`): tool-free root `mise.toml`, global tools in
  `unix/home/.config/mise/`, public config schemas, 100% native compiled `hk.pkl`
  with `mise fmt`, worktree `planning-with-files`, and live `env:doctor`.
- [ ] **Step 2:** Subscriptions-only AI harnesses added and live-verified across
  all 3 environments (`pc`, `mac`, `vm`) before C++ compilers:
  - [ ] 2.1 Claude Code (`agent-secrets` OAuth token mount)
  - [ ] 2.2 OpenCode (free zero-retention model)
  - [ ] 2.3 ZCode (`npm:zcode-app-cli`)
  - [ ] 2.4 Antigravity CLI (`agy`, `Orchestrator (Neon)` default)
  - [ ] 2.5 Codex (`--device-auth`) + `ray-manaloto/skills` + `[dotfile_groups]`
- [ ] **Step 3:** Declarative C++26 core (`mise.ubuntu.toml`: pinned latest
  GCC 16, Clang 23, `lld`/`lldb`/`libc++`, `gdb`, `valgrind`, `cppcheck`,
  `cmake`, `ninja`, `vcpkg`, `gcovr`) + C++26 smoke test, followed by optional
  extras (`clang-p2996`, IWYU, CUDA/NVPTX).
- [ ] **Step 4:** Tighten GitHub ruleset and CI gates after Step 2 and Step 3
  pass cold-build verification.
