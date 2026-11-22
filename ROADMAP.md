# dotfiles-v2 Roadmap

- [ ] **Step 0:** Bare repo (`ray-manaloto/dotfiles-v2`), `mise 2026.11.12`,
  `hk-config`, `renovate-config`, clean `AGENTS.md`, `unix/home/` root.
- [ ] **Step 1:** Bare Ubuntu 26.10 container (`docker/ubuntu/Dockerfile`,
  pinned `ca-certificates`, `curl`, `git`, `mise v2026.11.12`, non-privileged,
  `-e container=docker`) + `container:up` / `container:sync` on `pc`.
- [ ] **Step 1.5:** Fresh Mac `agy` session rooted here + `/context` bloat
  audit and cleanup.
- [ ] **Step 2a:** Declarative C++26 core (`mise.ubuntu.toml`: pinned latest
  GCC 16, Clang 23, `lld`/`lldb`/`libc++`, `gdb`, `valgrind`, `cppcheck`,
  `cmake`, `ninja`, `vcpkg`, `gcovr`) + C++26 smoke test.
- [ ] **Step 2b:** Optional C++ extras (`clang-p2996` prebuilt release, IWYU,
  CUDA/NVPTX).
- [ ] **Step 3:** Subscription-backed AI harnesses added and live-verified one by
  one:
  - [ ] 3.1 Claude Code (`agent-secrets` OAuth token mount)
  - [ ] 3.2 OpenCode (free zero-retention model)
  - [ ] 3.3 ZCode (`npm:zcode-app-cli`)
  - [ ] 3.4 Antigravity CLI (`agy`, `Orchestrator (Neon)` default)
  - [ ] 3.5 Codex (`--device-auth`) + `ray-manaloto/skills` + `[dotfile_groups]`
- [ ] **Step 4:** Tighten GitHub ruleset and CI gates after Step 3 passes cold
  build verification.
