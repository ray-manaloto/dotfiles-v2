# Test inventory (#95)

Issue #95 asks which pytest checks of setup or config should move to
`mise doctor project` or `hk check --all`, so that pytest only tests project code.
This file lists every test module and gives each one a verdict. No test is moved
here; the migrations are follow-up work. The ticket wants the inventory in the
issue itself, so paste the tables below into #95.

## Verdicts

- **keep**: the module tests project code (task scripts, `tasks/py/*`,
  `unix/home/.config/mise/lib/*`, hooks) with fixtures, fakes or a temporary
  home. Some tests also assert that a task script contains a given string.
  These string checks count as tests of project code, so they stay.
- **migrate**: at least one test asserts committed repo or config state (pins,
  lockfiles, manifests, `mise.toml` doctor wiring, or a lint run over the real
  tree) that no doctor or hk check covers yet, or that a check covers only
  outside push time (see done). The detail table names the target check, which
  has to be added, extended or wired into pre-push before the test can go.
- **done**: every repo-state assertion in the module is already covered by the
  named check, and that check runs over the whole tree at push time and in CI.
  Only deleting the duplicate pytest code is left. A check runs at push time
  when it is a step in `hooks["pre-push"]` in `hk.pkl` or a `[doctor.checks]`
  entry, because the `gh-doctor-pre-push` git hook (the `~/.gitconfig/gh-doctor`
  block in `unix/home/.config/mise/config.toml`) runs `mise doctor project`. An hk step that runs only in `pre-commit` (on staged
  files) and `hk check --all` does not count. For such a step, the pytest copy is
  the only whole-tree run at push time. Deleting the copy would let a
  `--no-verify` commit reach the remote unchecked.

A module marked migrate or done keeps all of its other tests.

## Timings

All timings, except those marked † in the detail table, come from one local run on the Mac host (2026-11-27, `pytest 9.2.0`,
CPython 3.15.2). Other agents were running at the same time, so treat the
numbers as rough. The run used base commit
`baf65de721afaa64c427ece195c8ce3f61699f28`. All `file:line` references below
point at that tree and will go stale when the test files change.

| Run | Wall time |
| --- | --- |
| `UV_OFFLINE=1 mise run py:test` (`-n auto`, `PYTEST_XDIST_AUTO_NUM_WORKERS=4`) | 31.6 s, 609 passed |
| `pytest` step inside `hk check --all` (`-n auto`), two runs | 17.9 s and 27.3 s, 609 passed |
| serial `pytest -p no:xdist --junitxml` (source of the per-module times below) | 54.8 s, 608 passed, 1 failed (see the note on `test_env_stats_and_ui`) |
| `mise doctor project` (whole command) | 6.0 s |

## Module inventory

Modules are sorted by serial time. "Tests" is the number of collected items,
including parametrized cases.

| Module | Tests | Serial s | Verdict | Target check(s) / notes |
| --- | ---: | ---: | --- | --- |
| `tests/test_gates.py` | 115 | 11.12 | migrate | done: `dockerignore-secret-guard`, `git-index-locks`, `mise-version-parity`, `config-hygiene`, `usage-lint`, `pitchfork-config`, `mise-binary-integrity`, `reviewer-plugins-declared`, `claude-ship-env`, `codex-marketplaces`; migrate: `config-hygiene`, `py39-compat`, `mcp-policy`, `reviewer-plugins` repo scope, new `guest-path-order`, new `doctor-config` |
| `tests/test_wrappers_and_tasks.py` | 13 | 6.87 | migrate | new `doctor-config`, `config-hygiene` |
| `tests/test_agy_code_review_guard.py` | 7 | 5.36 | migrate | doctor `agy-code-review-guard` must also run under `/usr/bin/python3` |
| `tests/test_responses_agent.py` | 18 | 4.12 | keep | |
| `tests/test_worktree_issue_pwf.py` | 4 | 3.88 | keep | `test_guard_self_check_and_nanosecond_receipt` overlaps doctor `agy-pwf-guard`, but it also checks the self-check names and timestamp format, which the doctor check does not |
| `tests/test_env_stats_and_ui.py` | 5 | 3.43 | migrate | `mise-lock-integrity`, `config-hygiene`, new `herdr-mods`; `test_agy_statusline_..._sub25ms_latency` failed its strict 25 ms wall-clock median in the serial run on a busy host and passed when rerun alone. It is a timing budget, so expect it to be flaky |
| `tests/test_agy_learn.py` | 23 | 2.62 | migrate | done: doctor `post-task-learn` (`hooks.json` registration); migrate: doctor `post-task-learn`, `agy-pwf-guard`, `agy-hc-send-guard`, `agy-interactive-questions` must also run under `/usr/bin/python3` |
| `tests/test_env_smoke.py` | 140 | 2.57 | keep | |
| `tests/test_dcg_hooks.py` | 70 | 2.24 | keep | runs the commands from the shipped hook JSON against fake `dcg`, so it tests behavior |
| `tests/test_api_runner.py` | 35 | 1.50 | migrate | `config-hygiene` (fnox `api-credit` profile) |
| `tests/test_pr_land.py` | 3 | 1.43 | keep | |
| `tests/test_pc_hub.py` | 25 | 1.24 | migrate | done: `pc-hub-config`; migrate: `pc-hub-config`, new `stdlib-only` |
| `tests/test_host_staging_prune.py` | 6 | 1.23 | keep | |
| `tests/test_env_inventory.py` | 10 | 1.22 | keep | |
| `tests/test_pc_host.py` | 17 | 1.05 | migrate | `config-hygiene` (`config.pc.toml` content), new `pin-parity`, `reviewer-plugins` |
| `tests/test_codex_bootstrap.py` | 10 | 0.96 | keep | |
| `tests/test_deps_audit.py` | 3 | 0.62 | migrate | new doctor `deps-audit-offline` |
| `tests/test_dotfiles_py.py` | 4 | 0.60 | done | hk `codegen-check` |
| `unix/home/.config/mise/lib/herdr_roll/tests/test_engine.py` | 20 | 0.29 | keep | also run by doctor `herdr-roll` (see below) |
| `tests/test_remote_ssh_lint.py` | 6 | 0.28 | migrate | add hk `remote-ssh-lint` to `hooks["pre-push"]` (it runs only in pre-commit and `hk check --all` today) |
| `tests/test_metadata_gate_lint.py` | 5 | 0.18 | migrate | add hk `metadata-gate-lint` to `hooks["pre-push"]` (same gap) |
| `tests/test_codex_trust.py` | 9 | 0.16 | keep | |
| `tests/test_py39_compat.py` | 5 | 0.13 | migrate | add hk `py39-compat` to `hooks["pre-push"]`, `py39-compat` glob parity, new `pin-parity` |
| `tests/test_env_quota.py` | 6 | 0.13 | keep | |
| `tests/test_openai_replay.py` | 21 | 0.05 | keep | |
| `unix/home/.config/mise/lib/herdr_roll/tests/test_adapters.py` | 17 | 0.03 | keep | also run by doctor `herdr-roll` (see below) |
| `tests/test_push_leak_scan.py` | 12 | 0.00 | keep | |
| **Total** | **609** | **53.29** | 14 keep, 12 migrate, 1 done | |

`tests/tasks_py_loader.py` is a helper and has no tests.

## Per-test detail (migrate and done)

The times are for the whole test. Where a test mixes cases, only the named
assertions move, so the saving is smaller than the time shown. Times marked †
come from a separate serial rerun of the single test, not from the run above.

| Test | Serial s | Assertion | Verdict | Target |
| --- | ---: | --- | --- | --- |
| `test_gates::test_tier1_tier2_offline_gates_catch_regressions` (lines 404-410 only) | 0.65 | the real repo passes six offline checks | done | doctor `dockerignore-secret-guard`, `git-index-locks`, `mise-version-parity`, `config-hygiene`, `usage-lint`, `pitchfork-config`. All but `git-index-locks` are also hk steps. `git-index-locks` is doctor only (`mise.toml` `[doctor.checks.git-index-locks]`) and has no hk step |
| `test_gates::test_live_host_gates_ci_skip_and_mise_audit_fixable` (lines 611-616 only) | 0.04 † | `check_mise_binary_integrity(repo_root) == []` with an empty `MISE_DATA_DIR`, so only the committed dcg config, pack, hooks and `harden-bins` are checked | done | doctor `mise-binary-integrity` (its committed-file checks run in every scope) |
| `test_gates::test_worktree_rm_common_dir_and_dcg_hardening` (lines 953-954, committed-file part only) | 1.59 † | the same call after `host:harden-bins` runs on a fixture installs dir | done | doctor `mise-binary-integrity` for the committed files. The fixture-installs part tests `host:harden-bins` behavior and stays |
| `test_gates::test_real_global_env_keeps_ship_available` | 0.01 | `_check_flag_fetch_env` is clean on the real config | done | hk (pre-push) and doctor `config-hygiene` (calls `_check_flag_fetch_env`) |
| `test_gates::test_real_managed_claude_settings_keep_ship` | 0.00 | `check_claude_ship_env` is clean in repo scope | done | doctor `claude-ship-env` (pre-push runs it with `DOTFILES_DOCTOR_SCOPE=repo`) |
| `test_gates::test_reviewer_plugins_repo_scope_passes_on_real_tree` | 0.01 | reviewer-plugins check passes on the real manifest, lib and tasks | done | hk `reviewer-plugins-declared` (also in pre-push) |
| `test_gates::test_reviewer_sync_never_removes_mcp_servers` (lines 2578-2584 only) | 0.01 † | the real `reviewer-plugins.json` has no `claude.retired_mcps` | migrate | `reviewer-plugins` repo scope (hk `reviewer-plugins-declared`): reject a `retired_mcps` key. No check reads that key today |
| `test_gates::test_codex_marketplace_policy_is_consistent_and_rejects_claude` (first assert only) | 0.00 | the committed marketplace policy is consistent | done | doctor `codex-marketplaces` (this policy check runs before the live-host skip) |
| `test_gates::test_real_managed_claude_settings_keep_connectors_off` | 0.00 | `permissions.json` has `disableClaudeAiConnectors`, the apify deny and the `server-pdf` deny | migrate | the connector flag and the apify deny are already covered by `claude-ship-env` and `mcp-policy`. Add `serverCommand` denies (`@modelcontextprotocol/server-pdf`) to `mcp-policy` |
| `test_gates::test_host_hygiene_shim_classification_and_ignored_tracked_configs` (lines 1450-1453 only) | 0.01 | `miserc.toml` keeps the opencode offline path | migrate | `config-hygiene` |
| `test_gates::test_guest_shim_first_path_herdr_roll_dep_and_workspace_roll_ownership` (lines 1512-1549 only) | 0.01 | shim-first PATH order in Dockerfile, container env, VM env and `env:doctor`; the `herdr-roll` doctor check depends on `py-deps-fresh` | migrate | new `guest-path-order` for PATH order; new `doctor-config` for the `depends` wiring |
| `test_gates::test_reviewer_plugins_lib_parses_on_python39` | 0.00 | `unix/home/.config/mise/lib/reviewer_plugins.py` parses as Python 3.9 | migrate | add the file to the hk `py39-compat` glob and to `pyproject.toml` `per-file-target-version`, and add `py39-compat` to `hooks["pre-push"]` |
| `test_wrappers_and_tasks::test_host_doctor_checks_skip_in_repo_scope_and_pre_push_sets_it` | 0.01 | `mise.toml` host-only doctor checks are wrapped by `host_check.sh`; the gh-doctor pre-push block sets `DOTFILES_DOCTOR_SCOPE=repo` | migrate | new `doctor-config` (lint of `[doctor.checks]` wiring) |
| `test_wrappers_and_tasks::test_sync_gemini_and_6harness_parity_retire_legacy_mirrors` (lines 949-980 only) | 0.29 | the `[dotfiles]` gitconfig hook blocks parse with `git config` and end in the expected commands | migrate | `config-hygiene` |
| `test_agy_code_review_guard::test_agy_code_review_guard_macos_python3_9_compatible` | 0.20 | the hook `--self-check` passes under `/usr/bin/python3` (3.9) | migrate | doctor `agy-code-review-guard`: run under `/usr/bin/python3` when it exists (hk `py39-compat` checks only syntax) |
| `test_agy_learn::test_hooks_python39_compatibility_and_cross_session_worktree_rm` (first loop only) | 0.89 | four hooks pass `--self-check` under `/usr/bin/python3` | migrate | doctor `post-task-learn`, `agy-pwf-guard`, `agy-hc-send-guard`, `agy-interactive-questions`: same change as above |
| `test_agy_learn::test_hooks_python39_compatibility_and_cross_session_worktree_rm` (lines 930-932 only) | 0.89 | the committed `hooks.json` registers `post-task-learn` on `SessionStart` | done | doctor `post-task-learn` (`--check-hooks-json` requires non-empty `SessionStart` and `Stop` handlers, see `_check_hooks_json` in `post-task-learn.py`) |
| `test_env_stats_and_ui::test_bottom_locked_and_env_top_targets` (lines 41-58 only) | 0.53 | `bottom` pin, banned monitoring tools, `mise.lock` mode and platforms | migrate | lock mode and platforms are already covered by `mise-lock-integrity`. Add a banned-tools list to `config-hygiene`. Drop the version pin assert, since the lock already pins it |
| `test_env_stats_and_ui::test_herdr_env_stats_mod_and_sync_tasks` (lines 248-253 and 263-272 only) | 1.67 | `mod.json` fields and `herdr-app check` on the committed mod | migrate | new `herdr-mods` (see finding 4 for where it runs) |
| `test_api_runner::test_task_wrapper_routes_through_scoped_fnox_profile` (fnox half) | 0.00 | `fnox/api-credit.toml` has strict egress to `api.anthropic.com` | migrate | `config-hygiene` (its hk glob already includes `unix/home/.config/fnox/*.toml`) |
| `test_pc_hub::test_validate_pc_hub_repo_passes_on_real_repo` | 0.00 | the real hub stack validates | done | doctor `pc-hub-config` (`hub:doctor --offline`) |
| `test_pc_hub::test_pc_hub_cli_validate_and_build_status` (lines 297-301 only) | 0.02 † | `validate --repo REPO_ROOT` is ok (calls the same `validate_pc_hub_repo`) | done | doctor `pc-hub-config` |
| `test_pc_hub::test_hub_tasks_dry_run_and_container_refusal` (`hub:doctor` part only) | 0.48 | `hub:doctor --offline --json` reports ok on the real repo | done | doctor `pc-hub-config` |
| `test_pc_hub::test_agentsview_vector_recipe_is_complete` | 0.00 | `agentsview/semantic-search.toml` content | migrate | `pc-hub-config` (extend `validate_pc_hub_repo`) |
| `test_pc_hub::test_remote_py_is_stdlib_only` | 0.01 | `unix/home/.config/mise/lib/pc_hub/remote.py` imports only the stdlib | migrate | new `stdlib-only` (also covers `unix/home/.gemini/config/hooks/*.py`, which AGENTS.md requires to be stdlib-only and nothing checks today) |
| `test_pc_host::test_pc_overlay_and_zsh` (lines 77-92 only) | 0.44 † | committed `config.pc.toml`: `#:schema` first line, `settings.upgrade`, no `[env]`, and the `[dotfiles]` fnox entries | migrate | `config-hygiene` (its glob already covers `config*.toml`) |
| `test_pc_host::test_omarchy_channel_pin_matches_up_and_doctor` | 0.00 | `pc:up` and `pc:doctor` use the same Omarchy channel | migrate | new `pin-parity` |
| `test_pc_host::test_pc_reviewer_plugins_match_manifest` | 0.00 | the plugin list inlined in pc stack-doctor matches `reviewer-plugins.json` | migrate | `reviewer-plugins` repo scope (hk `reviewer-plugins-declared`) |
| `test_deps_audit::test_deps_audit_cli_help_and_repo_offline_json` (rc and items part) | 0.58 | `deps:audit --offline` exits 0 on the real repo | migrate | new `deps-audit-offline` (`mise run deps:audit --offline`) |
| `test_dotfiles_py::test_datamodel_codegen_check_passes` | 0.60 | `datamodel-codegen --check` | done | hk `codegen-check` (also in pre-push) |
| `test_remote_ssh_lint::test_repo_tasks_are_clean` | 0.06 | lint over `tasks/hub` and `tasks/pc` | migrate | add hk `remote-ssh-lint` to `hooks["pre-push"]`. Today it runs only in pre-commit (staged files) and `hk check --all`, so this test is the only whole-tree run at push time |
| `test_metadata_gate_lint::test_repo_workflows_and_review_code_are_clean` | 0.03 | lint over `.github/openai-review/*.py` and workflows | migrate | add hk `metadata-gate-lint` to `hooks["pre-push"]` (same gap). The test also lints `.github/openai-review/test_*.py`, which the hk step excludes |
| `test_py39_compat::test_real_targets_pass_and_globs_match_ruff_override` | 0.07 | the real targets pass, and the hk glob equals ruff `per-file-target-version` | migrate | add hk `py39-compat` to `hooks["pre-push"]` for the real-targets part (same gap). Teach `tasks/py/py39_compat.py` to compare its globs with `pyproject.toml` |
| `test_py39_compat::test_pc_ollama_pin_matches_hub_up` | 0.00 | `config.pc.toml`, `mise.pc.lock` and `hub:up` agree on the ollama version | migrate | new `pin-parity` |

## Findings

1. **Moving these checks will not make the suite much faster.** In the serial
   run, the migrate and done tests that can be deleted outright take about 1 s
   of 53 s. Where only some assertions move, the saving is smaller still. Most
   of the suite's time goes to behavior tests that start subprocesses. The
   three slowest are `test_wrappers_and_tasks::test_agy_rc_guard_check_and_never_runs_serve_help`
   (4.4 s), `test_agy_code_review_guard::test_full_code_review_and_post_review_verification_gate`
   (4.0 s) and `test_worktree_issue_pwf::...::test_worktree_new_and_pwf_status_end_to_end`
   (3.3 s). Together they take 22 % of the serial time. Splitting them and
   tuning `PYTEST_XDIST_AUTO_NUM_WORKERS` helps a little. The bigger saving is
   to stop running the whole suite more than once (finding 2).

2. **Duplicate runs.** This is the main speed lever.
   - **The whole suite runs two or three times per gate.** `hk check --all`
     already runs pytest: the `check` hook uses `checkSteps`, which is
     `fastSteps` plus the `pytest` step (`hk.pkl`, `checkSteps` and
     `hooks["check"]`). The `.github/workflows/ci.yml` job "Lint, Gates, and
     Tests" then runs `hk check --all` and runs `mise run py:test` again.
     `tasks/pr/ship` does the same: `hk check --all` and then
     `mise run -q py:test`. `git push` then runs the pre-push `pytest` step a
     third time when the pushed files match its glob. The timings above show the
     cost: about 18-27 s for the copy inside `hk check --all` and 31.6 s for
     `py:test`. To save that time, drop the separate `py:test` call from CI and
     `pr:ship`, since `hk check --all` already covers it. The other option is to
     run `hk check --all --skip-step pytest` before `py:test`. Either change saves
     one full suite run, much more than the roughly 1 s that migration saves.
   - **`herdr-roll` runs unit tests.** The doctor check `herdr-roll` runs
     `unittest discover` over `herdr_roll/tests`, and pytest already collects
     those tests through `testpaths`. A doctor check should check setup, not run
     unit tests, so `herdr-roll` could keep only its executable-task and
     `SKILL.md` checks.

3. **Out of scope.** `.github/openai-review/test_*.py` is run by the hk step
   `openai-review-tests` (unittest), not by pytest, so it is not listed here.

4. **Suggested new checks.** Each one owns several assertions. Each must run
   over the whole tree at push time (see the done definition): either as a
   `[doctor.checks]` entry, or as an hk step that is also listed in
   `hooks["pre-push"]`.

   | Check | Owns | Surface |
   | --- | --- | --- |
   | `pin-parity` | ollama, Omarchy channel, other cross-file pins | `tasks/py/doctor_checks.py` subcommand, called from `[doctor.checks]` and from an hk step whose glob covers the pinned files |
   | `doctor-config` | `[doctor.checks]` wiring (`host_check.sh` wrapping, `depends`) and the gh-doctor pre-push block | `doctor_checks.py` subcommand, called from `[doctor.checks]` and from an hk step on `mise.toml` and `unix/home/.config/mise/config.toml` |
   | `guest-path-order` | shim-first PATH in Dockerfile, container env, VM env and `env:doctor` | `doctor_checks.py` subcommand, called from `[doctor.checks]` and from an hk step on those four files |
   | `stdlib-only` | `unix/home/.config/mise/lib/pc_hub/remote.py` and `unix/home/.gemini/config/hooks/*.py` | hk step only (a file lint like `py39-compat`), and it must also be listed in `hooks["pre-push"]` |
   | `herdr-mods` | `mod.json` fields and `herdr-app check` on the committed mod | `doctor_checks.py` subcommand, `[doctor.checks]` only. The `mod.json` part runs everywhere. The `herdr-app check` part skips unless the host is macOS and `herdr-app` is on PATH, as `check_herdr_app_bundle` does. `[doctor.checks]` entries in `mise.toml` use only `description`, `run`, `hint`, `depends` and `timeout`, so the check skips inside the code, not through an OS key |
   | `deps-audit-offline` | `deps:audit --offline` exits 0 on the real repo | `[doctor.checks]` only, `run = "mise run deps:audit --offline"`. AGENTS.md requires `#USAGE` file tasks in `[doctor.checks]` to be called through `mise ... run` |

   AGENTS.md says only to prefer `mise doctor project` and `hk check --all` over
   unit tests for setup checks. It does not say which of the two surfaces a check
   belongs to. The surfaces above are suggestions.
