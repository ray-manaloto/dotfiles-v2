---
name: toolchain-update-review
description: >-
  Use when running a cross-OS toolchain, OCI image, Python, hk preset, native
  harness, or OpenWiki dependency update review using `mise run deps:audit`,
  changelog triage, lockfile regeneration, and verification gates.
---

# Toolchain & Dependency Update Review

Standardized, context-free procedure to audit and land tool, OCI, Python, hook, and harness updates across macOS host, Tart VM, and Linux containers.

## Step 1: Run Automated Cross-OS Audit

Always start with the schema-first `deps:audit` task (it scans every global and OS-specific `config*.toml`, including `os = ["linux"]` tools skipped by `mise outdated` on macOS):

```sh
mise run deps:audit --offline --json   # Fast local parity/digest/lock/config scan
mise run deps:audit --json             # Online upstream latest-version & compare-URL scan
```

Check every reported category:

1. **`mise` core binary + OCI + `packslip` pin:** root `mise.toml` `min_version`, global `config.toml` `min_version`, Dockerfile digest-pinned `ghcr.io/jdx/mise`, `tasks/vm/up` `MISE_VER` & `packslip` pin, `tasks/env/doctor`, `AGENTS.md`, and `mise-bootstrap-ops` skill.
2. **All global & OS-specific `config*.toml` tools:** macOS, Linux, container, VM, `apt:*`, `brew:*`, `github:*`, `npm:*`, `aqua:*`, `packslip:*`. Verify registry short names (zero backend opt-outs) and `prerelease = true` on hyphenated npm versions.
3. **`uv` / `pyproject.toml` & `uv.lock`:** inspect `[project].dependencies` and `[dependency-groups]` for security/compatibility bumps (`uv lock --upgrade-package ...` when upgrading).
4. **OCI base digests:** `ubuntu:26.10` and `ghcr.io/jdx/mise` in Dockerfile (`docker buildx imagetools inspect` before bumping).
5. **`hk.pkl`:** preset URL (`ray-manaloto/hk-config`) and `Builtins.pkl` `jdx/hk` tag match `[tools].hk`.
6. **Native harnesses:** `claude`, `codex`, `agy`, `opencode`, `hermes`, `zcode` (`npm:zcode-app-cli` validated via `zcode doctor --json` in isolated temp `HOME`).
7. **`kb:outdated`:** check OpenWiki checkout pins in `knowledge-base-v2` (respect any documented write holds such as `tailscale`).

## Step 2: Review Changelogs & Smoke-Test Before Applying

- For every proposed bump, inspect upstream release notes (`gh release view` / `compare_url`) for breaking CLI flags, hook schema changes, seccomp constraints, or checksum mismatches (for example, confirm `pkgs.tailscale.com/stable` publishes an archive before bumping `tailscale`).
- Test pre-release/risky CLI upgrades (such as `npm:zcode-app-cli`) inside an isolated `mktemp -d` `HOME` before updating repo pins.
- Never overwrite live `~/.local/share/mise/installs` manually (`dcg` `custom.mise_protect` + `0555` `host:harden-bins` enforce binary immutability).

## Step 3: Lock, Format & Verify End-to-End

1. Update pins and regenerate lockfiles inside the managed mise config dir with clean environment & `0644` permissions:

   ```sh
   dotfiles_home="$PWD/unix/home"
   MISE_CONFIG_DIR="${dotfiles_home}/.config/mise"
   MISE_OFFLINE=0 MISE_CONFIG_DIR="$MISE_CONFIG_DIR" \
     MISE_GLOBAL_CONFIG_FILE="$MISE_CONFIG_DIR/config.toml" \
     mise -C "$MISE_CONFIG_DIR" lock --global
   chmod 0644 "$MISE_CONFIG_DIR/mise.lock"
   find "$MISE_CONFIG_DIR/locks" -type d -exec chmod 0755 {} + -o -type f -exec chmod 0644 {} +
   ```

2. Run full verification gates:

   ```sh
   mise run py:codegen --check
   mise run py:test
   mise run host:sync-gemini --check
   hk check --all
   mise doctor project
   ```

3. Complete Stage 1 code review (`--record`), Stage 2 behavioral `--verify`, and bounded `/learn` review before committing or shipping.
