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
  - [ ] 2.4 Antigravity CLI (`agy`, `Orchestrator (Neon)` default; `agy-rc-guard`
    and quota-gated `env:smoke` landed; container/VM bootstrap tracked in `#24`)
  - [ ] 2.5 Codex (`--device-auth`) + `ray-manaloto/skills` + `[dotfile_groups]`
    (`dotfile_groups`, 6-harness `bootstrap:skills`, and `env:smoke` landed;
    remaining container/VM `codex` bootstrap parity tracked in `#24`)
- [ ] **Step 3:** Declarative C++26 core (`mise.ubuntu.toml`: pinned latest
  GCC 16, Clang 23, `lld`/`lldb`/`libc++`, `gdb`, `valgrind`, `cppcheck`,
  `cmake`, `ninja`, `vcpkg`, `gcovr`) + C++26 smoke test, followed by optional
  extras (`clang-p2996`, IWYU, CUDA/NVPTX). Seed it from the sunset
  `cpp-standard` (`mise.ubuntu.toml` and the 83-item checklist in
  `docs/standard/ubuntu.md`) and land everything here; `cpp-standard` takes no
  new work.
- [ ] **Follow-up (`cpp-standard` sunset, 2026-11-27):** stop its scheduled
  workflows, save uncommitted worktree changes, move `repotools` and the `pc`
  runner launcher's source of truth here, re-home the `clang-p2996` build and
  the FIX encoder, then archive it and the legacy `dotfiles`.
- [ ] **Step 4:** Tighten GitHub ruleset and CI gates after Step 2 and Step 3
  pass cold-build verification.
- [ ] **Follow-up (Host Homebrew -> `mise` audit):** Migrate clean prebuilt
  Mac host leaves (`mas` -> `aqua:mas-cli/mas`, `ruby` -> `core:ruby`, `viu`
  -> `github:atanunq/viu`, `dockerfilegraph` ->
  `github:patrickhoefler/dockerfilegraph`) to global `mise` config while keeping
  native C++/system libraries and Casks in Homebrew.
- [x] **Follow-up (`packslip v1.11.5`):** Switched `tasks/vm/up` back to
  `packslip install --pin ps1_nlhmwtfeufglxv5myvwvronk7a` once `jdx/packslip`
  `v1.11.5` (PR `#347`, fixing `#346`) released (`#13`).
- [x] **Follow-up (Phase 0 local `vercel/board` + `jdx` skills prototype):**
  Packaged `npm:@vercel/board` (`0.54.1`), `pitchfork`, `aube`, `communique`,
  and `usage` in repo config (`147cba6`), synced `jdx` + `vercel-board` skills,
  and verified `pitchfork`-supervised local board on MacBook/containers/VM
  (global Option A host cutover + `0.55.0` tracked in `#9`; always-on `pc`
  orchestration in `#11`).
- [ ] **Follow-up (`knowledge-base-v2` container readiness):** Evaluate running
  `knowledge-base-v2` inside `dotfiles-v2` containers (`jq` in container toolset,
  `:ro` `~/.config/agents/private-patterns.txt` mount, persistent `kb.noindex` /
  `pins.json` mounts, and `fnox` proxy forwarding).
- [x] **Follow-up (Clean-room `herdr_roll` & post-review verification gate):**
  Shipped clean-room 6-harness rolling-restart engine (`herdr_roll`, `e73ce7b`,
  `5b5513a`), `repotools trust` worktree wiring, and ordered 2-stage
  post-review verification gate (`2eb3410`).
- [x] **Follow-up (Herdr rolling restarts & issue-mapped PWF workflow):**
  - [x] [#4](https://github.com/ray-manaloto/dotfiles-v2/issues/4) Cut over
    global `roll:*` `mise` tasks and Herdr skills from legacy `dotfiles` to
    `dotfiles-v2` (`PLAN_ID` / worktree slug: `herdr-roll-global-cutover`;
    resolved via `#9` on `main` at `0c24382`).
  - [x] [#5](https://github.com/ray-manaloto/dotfiles-v2/issues/5) Fix
    `OpenCodeAdapter` session-ID resolution for `opencode --standalone` tabs
    without `-s` (`PLAN_ID` / worktree slug: `herdr-roll-opencode-sid`;
    shipped on `main` at `3be8af`).
  - [x] [#7](https://github.com/ray-manaloto/dotfiles-v2/issues/7) Live
    `--apply` verification across `claude`, `hermes`, `zcode`, and full
    reboot/wave flows (`PLAN_ID` / worktree slug: `herdr-roll-live-verify`;
    completed in `62f57c6` / `aaa5c4d`).
  - [x] [#8](https://github.com/ray-manaloto/dotfiles-v2/issues/8) Add
    nanosecond-timestamped verification evidence, process `lstart`/hook-load
    proof to `roll:verify`, and dialog triage docs (`PLAN_ID` / worktree slug:
    `herdr-roll-verify-proof`; shipped on `main` at `3be8af`).
  - [x] [#6](https://github.com/ray-manaloto/dotfiles-v2/issues/6) Enforce
    GitHub issue -> PWF `PLAN_ID` -> git worktree mapping and nanosecond
    verification evidence (`PLAN_ID` / worktree slug: `worktree-issue-pwf-map`;
    shipped on `main` at `0c24382`).
- [ ] **Follow-up (Open GitHub PR & credit-backed coding-agent runners):**
  - [ ] [PR #1](https://github.com/ray-manaloto/dotfiles-v2/pull/1) Add manual
    OpenAI PR review (`automation/manual-openai-pr-review`).
  - [ ] [#2](https://github.com/ray-manaloto/dotfiles-v2/issues/2) Add an OpenAI
    Responses coding-agent profile using linked API credits.
  - [ ] [#3](https://github.com/ray-manaloto/dotfiles-v2/issues/3) Add a
    self-run Claude coding agent using linked API credits.
- [ ] **Follow-up (Option A cutover, `@vercel/board@0.55.0`, schema-first
  Python & self-healing `/learn`):**
  - [x] [#9](https://github.com/ray-manaloto/dotfiles-v2/issues/9) Execute
    zero-debt Option A Mac host cutover (6 active repos) and upgrade
    `@vercel/board` to `0.55.0` (`PLAN_ID` / worktree slug:
    `option-a-cutover-and-board-055`; also resolves `#4`; shipped on `main` at
    `0c24382`).
  - [x] [#10](https://github.com/ray-manaloto/dotfiles-v2/issues/10) Adopt
    aggregated `pyproject.toml` + `[deps.uv]`, schema-first
    `datamodel-codegen`, dual-sink `structlog`, and strict `hk`/doctor
    prevention gates (`PLAN_ID` / worktree slug: `py-schema-deps-and-gates`;
    shipped on `main` at `0c24382`).
  - [x] [#12](https://github.com/ray-manaloto/dotfiles-v2/issues/12) Build
    self-optimizing/self-healing `agy-learn` ledger, fix 7 Antigravity hook
    bugs, and apply Proposal 8 (`PLAN_ID` / worktree slug:
    `self-healing-agy-learn`; shipped on `main` at `3be8af`).
  - [ ] [#11](https://github.com/ray-manaloto/dotfiles-v2/issues/11) Host
    `@vercel/board` always-on on remote Linux host (`pc`) and adopt
    `templates/swarm` + `factory` orchestration (`PLAN_ID` / worktree slug:
    `vercel-board-pc-orchestration`; `@vercel/board@0.56.1` now live behind
    `pc` Caddy/Tailscale and synced across `host`/`pc`/`mac`/`vm` in
    `806d74c`–`5d6b945`; remaining: `templates/swarm`/`factory` channel
    conventions `ops/kb-queue` & `work/*`, tailnet ACL cleanup, and
    `--no-tailscale` Mac fallback check).
- [ ] **Follow-up (Deep Audit, Full `mise`/`jdx` & Dependency Upgrades, Hardened Tier 1–3 Health Gates):**
  - [x] [#13](https://github.com/ray-manaloto/dotfiles-v2/issues/13) Upgrade
    `mise` (`2026.11.13`), `packslip` (`1.11.5`), `hk` (`2.14.0`), all outdated
    global tools & `uv` deps, add `webclaw` (`0.7.4`) & `@vercel/board`
    (`0.56.1`), and canonicalize `mise registry` names (`PLAN_ID` / worktree
    slug: `jdx-and-deps-upgrades`).
  - [x] [#14](https://github.com/ray-manaloto/dotfiles-v2/issues/14) Fix
    `opencode`/`vb`/`fnox` wrappers, `.dockerignore` secret leak,
    `config.local.toml -> fnox`, `[dotfiles]` `main_checkout`, `bootstrap:skills`,
    and Mac host cleanup (`PLAN_ID` / worktree slug: `cli-wrappers-and-dotfiles`).
  - [x] [#15](https://github.com/ray-manaloto/dotfiles-v2/issues/15) Enforce
    Tier 1–3 zero-warning/zero-error health gates and fix `post-task-learn`
    double-trigger (`PLAN_ID` / worktree slug: `strict-gates-and-learn-hook`).
  - [ ] [#16](https://github.com/ray-manaloto/dotfiles-v2/issues/16) Migrate
    active sibling repos (`cpp-core`, `renovate-config`) off project-level
    `[tools]` and `hk 2.12.0` (separate follow-up tracking issue).
- [x] **Follow-up (Phase 12: `agy-learn` enforcement, `roll:self` / `agy`
  `1.5.16` / staging prune, BuildKit SBOM, native `env:inventory`, and
  zero-opt-out `mise registry`):**
  - [x] [#17](https://github.com/ray-manaloto/dotfiles-v2/issues/17) Enforce
    canonical `plan_key`, anti-rubber-stamp `no_new_proposals`/`PROPOSED`/
    `APPLIED` gates, legacy `hook_drift` warnings, and `Errors Encountered`
    row-count cross-check in `agy-pwf-guard.py` (`PLAN_ID` / worktree slug:
    `agy-learn-anti-rubber-stamp`; shipped on `main` at `f531e5e`).
  - [x] [#18](https://github.com/ray-manaloto/dotfiles-v2/issues/18) Refresh live
    status before `skipped-busy`, raise `roll:self` `--busy-wait` default to
    `90s`, support `agy 1.5.16` `-r`/`--resume`, and share stdlib fail-closed
    `mise_staging_prune` across `host:prune` and `host-hygiene` (`PLAN_ID` /
    worktree slug: `roll-self-agy-resume-and-staging-prune`; shipped on `main`
    at `2d0405c`).
  - [x] [#19](https://github.com/ray-manaloto/dotfiles-v2/issues/19) Parallelize
    `container:up --target all` and `env:doctor --target all`, emit BuildKit
    `--sbom=true --provenance=mode=max` metadata, document Phase 10-11 ops in
    `mise-bootstrap-ops/SKILL.md`, and ship schema-first `env:inventory` +
    sanitized `inventory/latest.{host,pc,mac,vm}.json` (`PLAN_ID` / worktree
    slug: `parallel-containers-and-native-inventory`; shipped on `main` at
    `1d413d2` and `2b4e4a5`).
  - [x] [#20](https://github.com/ray-manaloto/dotfiles-v2/issues/20) Remove
    `DOCUMENTED_BACKEND_OPTOUTS` and `#` bypass, enforce canonical
    `mise registry` short names across all `config*.toml`, switch `oxfmt` to
    `0.80.0` (`npm:oxfmt`/`aube`), add `syft` `1.57.2`, and support canonical
    `npm:` entries in `check_mise_lock_integrity` (`PLAN_ID` / worktree slug:
    `mise-registry-canonical`; shipped on `main` at `23794f3`).
- [x] **Follow-up (Phase 13 Wave 1A & 1B: core `mise`, 6-harness skills,
  Arch B `fnox`, CLI-first research skills, hardened hooks/CI):**
  - [x] [#21](https://github.com/ray-manaloto/dotfiles-v2/issues/21) Wave 1A:
    core `mise 2026.11.14`, strict `tombi` schema, `[dotfile_groups]`,
    universal `zsh`, 6-harness `bootstrap:skills`, `[shell_alias]`, `Herdr.app`
    cask, `upgrade.auto_prune`, `dotfiles-catalog`, `yt-dlp`, `1h` soak, Arch B
    scoped `fnox`, `dcg`+`0555` hardening, CLI-first research skills, and `deps:audit`
    (shipped on `main` at `2daba55`, `1e2a3ca`, `278efcc`, `5c5212e`).
  - [x] [#22](https://github.com/ray-manaloto/dotfiles-v2/issues/22) Wave 1B:
    `/learn` & hook shell-unwrapping hardening, Claude/Codex `/learn` hooks,
    `codex:hook-trust` / `codex:plugin-update`, smart wrappers + `agy-rc-guard`,
    `herdr_roll` closed-space fix, `env:enter`, and
    `.github/workflows/{ci,health}.yml` (shipped on `main` at `fb0e5c6`,
    `fb9065b`, `b1387b9`, `dac18ce`).
- [ ] **Follow-up (Phase 13 Wave 2A/2B & Track 4: `pc` Hub, Stats UI, `pc`
  Option A, `env:quota`, multi-project containers):**
  - [ ] [#23](https://github.com/ray-manaloto/dotfiles-v2/issues/23) Wave 2A:
    **Shipped:** always-on `pc` Hub (`Caddy`, `Beszel 0.21.2` + Permanent
    Universal Token + `3/3` host agents + 20 containers, `@vercel/board@0.56.1`
    synced across `host`/`pc`/`mac`/`vm`, `AgentsView Hub` w/ `11G` Mac archive
    & `NO_LOCAL_DAEMON`, `SearXNG`+`Valkey`, `qmd 2.9.0` w/ `43,029` indexed KB
    files via `knowledge-base-v2` `PR #160`/`#161`, CLI-only static
    `codebase-memory-mcp 0.12.1`; `806d74c`–`5d6b945`). **Remaining:**
    `config.pc.toml` + `tasks/pc/{up,sync,doctor}` Option A cutover on `pc`
    (`fnox-age.key`) and `ops/kb-queue` / `work/*` board channels.
  - [ ] [#24](https://github.com/ray-manaloto/dotfiles-v2/issues/24) Wave 2B:
    **Shipped:** dynamic quota-gated `env:smoke --skill-check`, Track 4 UI/TUI
    (`bottom`, `env:top`, `env:stats`, native GPUI `env-stats` Herdr mod,
    `herdr:realtime-stats`, `<25ms` `agy-statusline`, dead Codex `[otel]`
    cleanup; `278efcc`, `6e88e27`–`0fbb33b`). **Remaining:** standalone
    `tasks/env/quota` (`env:doctor --smoke`), container/VM `agy`/`codex`
    bootstrap parity, and persistent Docker volumes + multi-project `pc`
    containers (`kb-v2-pc`, `cpp-core-pc`).
- [ ] **Follow-up (GitHub Actions AI PR reviewers & upstream trackers):**
  - [ ] [#25](https://github.com/ray-manaloto/dotfiles-v2/issues/25) Claude
    GitHub Actions PR code review setup (`PR #1` & sibling repos).
  - [ ] [#26](https://github.com/ray-manaloto/dotfiles-v2/issues/26) Codex
    GitHub Actions PR code review setup (`PR #1` & sibling repos).
  - [ ] [#27](https://github.com/ray-manaloto/dotfiles-v2/issues/27) Upstream
    tracker: Codex `app-server-daemon` env isolation, ephemeral `fnox` CA
    lifecycle & plugin hook trust.
  - [ ] [#28](https://github.com/ray-manaloto/dotfiles-v2/issues/28) Upstream
    tracker: Antigravity CLI (`agy`) Remote Control macOS bugs.
