# Test inventory (#95)

Issue #95 asks which pytest checks of setup or config should move to
`mise doctor project` or `hk check --all`, so that pytest only tests project code.
This file lists every test module and gives each one a verdict. No test is moved
here; the migrations are follow-up work.

## Verdicts

- **keep**: the module tests project code (task scripts, `tasks/py/*`,
  `unix/home/.config/mise/lib/*`, hooks) with fixtures, fakes or a temporary
  home. Some tests also assert that a task script contains a given string.
  These string checks count as tests of project code, so they stay.
- **migrate**: at least one test asserts committed repo or config state (pins,
  lockfiles, manifests, `mise.toml` doctor wiring, or a lint run over the real
  tree) that no doctor or hk check covers yet. The detail table names the
  target check, which has to be added or extended before the test can go.
- **done**: every repo-state assertion in the module is already covered by the
  named hk step or doctor check, which runs on every `hk check --all` and
  `mise doctor project`. Only deleting the duplicate pytest code is left.

A module marked migrate or done keeps all of its other tests.

## Timings

All timings come from one local run on the Mac host (2026-11-27, `pytest 9.2.0`,
CPython 3.15.2). Other agents were running at the same time, so treat the
numbers as rough.

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
| `tests/test_gates.py` | 115 | 11.12 | migrate | done: `dockerignore-secret-guard`, `git-index-locks`, `mise-version-parity`, `config-hygiene`, `usage-lint`, `pitchfork-config`, `reviewer-plugins-declared`, `claude-ship-env`, `codex-marketplaces`; migrate: `config-hygiene`, `py39-compat`, `mcp-policy`, new `guest-path-order`, new `doctor-config` |
| `tests/test_wrappers_and_tasks.py` | 13 | 6.87 | migrate | new `doctor-config`, `config-hygiene` |
| `tests/test_agy_code_review_guard.py` | 7 | 5.36 | migrate | doctor `agy-code-review-guard` must also run under `/usr/bin/python3` |
| `tests/test_responses_agent.py` | 18 | 4.12 | keep | |
| `tests/test_worktree_issue_pwf.py` | 4 | 3.88 | keep | `test_guard_self_check_and_nanosecond_receipt` overlaps doctor `agy-pwf-guard`, but it also checks the self-check names and timestamp format, which the doctor check does not |
| `tests/test_env_stats_and_ui.py` | 5 | 3.43 | migrate | `mise-lock-integrity`, `config-hygiene`, new `herdr-mods`; `test_agy_statusline_..._sub25ms_latency` failed its strict 25 ms wall-clock median in the serial run on a busy host and passed when rerun alone. It is a timing budget, so expect it to be flaky |
| `tests/test_agy_learn.py` | 23 | 2.62 | migrate | doctor `post-task-learn`, `agy-pwf-guard`, `agy-hc-send-guard`, `agy-interactive-questions` must also run under `/usr/bin/python3` |
| `tests/test_env_smoke.py` | 140 | 2.57 | keep | |
| `tests/test_dcg_hooks.py` | 70 | 2.24 | keep | runs the commands from the shipped hook JSON against fake `dcg`, so it tests behavior |
| `tests/test_api_runner.py` | 35 | 1.50 | migrate | `config-hygiene` (fnox `api-credit` profile) |
| `tests/test_pr_land.py` | 3 | 1.43 | keep | |
| `tests/test_pc_hub.py` | 25 | 1.24 | migrate | done: `pc-hub-config`; migrate: `pc-hub-config`, new hk `stdlib-only` |
| `tests/test_host_staging_prune.py` | 6 | 1.23 | keep | |
| `tests/test_env_inventory.py` | 10 | 1.22 | keep | |
| `tests/test_pc_host.py` | 17 | 1.05 | migrate | new `pin-parity`, `reviewer-plugins` |
| `tests/test_codex_bootstrap.py` | 10 | 0.96 | keep | |
| `tests/test_deps_audit.py` | 3 | 0.62 | migrate | new doctor `deps-audit-offline` |
| `tests/test_dotfiles_py.py` | 4 | 0.60 | done | hk `codegen-check` |
| `unix/home/.config/mise/lib/herdr_roll/tests/test_engine.py` | 20 | 0.29 | keep | also run by doctor `herdr-roll` (see below) |
| `tests/test_remote_ssh_lint.py` | 6 | 0.28 | done | hk `remote-ssh-lint` |
| `tests/test_metadata_gate_lint.py` | 5 | 0.18 | done | hk `metadata-gate-lint` |
| `tests/test_codex_trust.py` | 9 | 0.16 | keep | |
| `tests/test_py39_compat.py` | 5 | 0.13 | migrate | done: hk `py39-compat`; migrate: `py39-compat` glob parity, new `pin-parity` |
| `tests/test_env_quota.py` | 6 | 0.13 | keep | |
| `tests/test_openai_replay.py` | 21 | 0.05 | keep | |
| `unix/home/.config/mise/lib/herdr_roll/tests/test_adapters.py` | 17 | 0.03 | keep | also run by doctor `herdr-roll` (see below) |
| `tests/test_push_leak_scan.py` | 12 | 0.00 | keep | |
| **Total** | **609** | **53.29** | 14 keep, 10 migrate, 3 done | |

`tests/tasks_py_loader.py` is a helper and has no tests.

## Per-test detail (migrate and done)

The times are for the whole test. Where a test mixes cases, only the named
assertions move, so the saving is smaller than the time shown.

| Test | Serial s | Assertion | Verdict | Target |
| --- | ---: | --- | --- | --- |
| `test_gates::test_tier1_tier2_offline_gates_catch_regressions` (lines 404-410 only) | 0.65 | the real repo passes six offline checks | done | hk and doctor `dockerignore-secret-guard`, `git-index-locks`, `mise-version-parity`, `config-hygiene`, `usage-lint`, `pitchfork-config` |
| `test_gates::test_real_global_env_keeps_ship_available` | 0.01 | `_check_flag_fetch_env` is clean on the real config | done | hk and doctor `config-hygiene` (calls `_check_flag_fetch_env`) |
| `test_gates::test_real_managed_claude_settings_keep_ship` | 0.00 | `check_claude_ship_env` is clean in repo scope | done | doctor `claude-ship-env` (pre-push runs it with `DOTFILES_DOCTOR_SCOPE=repo`) |
| `test_gates::test_reviewer_plugins_repo_scope_passes_on_real_tree` | 0.01 | reviewer-plugins check passes on the real manifest, lib and tasks | done | hk `reviewer-plugins-declared` |
| `test_gates::test_codex_marketplace_policy_is_consistent_and_rejects_claude` (first assert only) | 0.00 | the committed marketplace policy is consistent | done | doctor `codex-marketplaces` (this policy check runs before the live-host skip) |
| `test_gates::test_real_managed_claude_settings_keep_connectors_off` | 0.00 | `permissions.json` has `disableClaudeAiConnectors`, the apify deny and the `server-pdf` deny | migrate | the connector flag and the apify deny are already covered by `claude-ship-env` and `mcp-policy`. Add `serverCommand` denies (`@modelcontextprotocol/server-pdf`) to `mcp-policy` |
| `test_gates::test_host_hygiene_shim_classification_and_ignored_tracked_configs` (lines 1450-1453 only) | 0.01 | `miserc.toml` keeps the opencode offline path | migrate | `config-hygiene` |
| `test_gates::test_guest_shim_first_path_herdr_roll_dep_and_workspace_roll_ownership` (lines 1512-1548 only) | 0.01 | shim-first PATH order in Dockerfile, container env, VM env and `env:doctor`; the `herdr-roll` doctor check depends on `py-deps-fresh` | migrate | new doctor and hk `guest-path-order` for PATH order; new `doctor-config` for the `depends` wiring |
| `test_gates::test_reviewer_plugins_lib_parses_on_python39` | 0.00 | `lib/reviewer_plugins.py` parses as Python 3.9 | migrate | add the file to the hk `py39-compat` glob and to `pyproject.toml` `per-file-target-version` |
| `test_wrappers_and_tasks::test_host_doctor_checks_skip_in_repo_scope_and_pre_push_sets_it` | 0.01 | `mise.toml` host-only doctor checks are wrapped by `host_check.sh`; the gh-doctor pre-push block sets `DOTFILES_DOCTOR_SCOPE=repo` | migrate | new doctor and hk `doctor-config` (lint of `[doctor.checks]` wiring) |
| `test_wrappers_and_tasks::test_sync_gemini_and_6harness_parity_retire_legacy_mirrors` (lines 949-980 only) | 0.29 | the `[dotfiles]` gitconfig hook blocks parse with `git config` and end in the expected commands | migrate | `config-hygiene` |
| `test_agy_code_review_guard::test_agy_code_review_guard_macos_python3_9_compatible` | 0.20 | the hook `--self-check` passes under `/usr/bin/python3` (3.9) | migrate | doctor `agy-code-review-guard`: run under `/usr/bin/python3` when it exists (hk `py39-compat` checks only syntax) |
| `test_agy_learn::test_hooks_python39_compatibility_and_cross_session_worktree_rm` (first loop only) | 0.89 | four hooks pass `--self-check` under `/usr/bin/python3` | migrate | doctor `post-task-learn`, `agy-pwf-guard`, `agy-hc-send-guard`, `agy-interactive-questions`: same change as above |
| `test_env_stats_and_ui::test_bottom_locked_and_env_top_targets` (lines 41-58 only) | 0.53 | `bottom` pin, banned monitoring tools, `mise.lock` mode and platforms | migrate | lock mode and platforms are already covered by `mise-lock-integrity`. Add a banned-tools list to `config-hygiene`. Drop the version pin assert, since the lock already pins it |
| `test_env_stats_and_ui::test_herdr_env_stats_mod_and_sync_tasks` (lines 248-253 and 263-272 only) | 1.67 | `mod.json` fields and `herdr-app check` on the committed mod | migrate | new doctor `herdr-mods` (`os = "macos"`) |
| `test_api_runner::test_task_wrapper_routes_through_scoped_fnox_profile` (fnox half) | 0.00 | `fnox/api-credit.toml` has strict egress to `api.anthropic.com` | migrate | `config-hygiene` (its hk glob already includes `unix/home/.config/fnox/*.toml`) |
| `test_pc_hub::test_validate_pc_hub_repo_passes_on_real_repo` | 0.00 | the real hub stack validates | done | doctor `pc-hub-config` (`hub:doctor --offline`) |
| `test_pc_hub::test_hub_tasks_dry_run_and_container_refusal` (`hub:doctor` part only) | 0.48 | `hub:doctor --offline --json` reports ok on the real repo | done | doctor `pc-hub-config` |
| `test_pc_hub::test_agentsview_vector_recipe_is_complete` | 0.00 | `agentsview/semantic-search.toml` content | migrate | `pc-hub-config` (extend `validate_pc_hub_repo`) |
| `test_pc_hub::test_remote_py_is_stdlib_only` | 0.01 | `lib/pc_hub/remote.py` imports only the stdlib | migrate | new hk `stdlib-only` (also covers `unix/home/.gemini/config/hooks/*.py`, which AGENTS.md requires to be stdlib-only and nothing checks today) |
| `test_pc_host::test_omarchy_channel_pin_matches_up_and_doctor` | 0.00 | `pc:up` and `pc:doctor` use the same Omarchy channel | migrate | new doctor and hk `pin-parity` |
| `test_pc_host::test_pc_reviewer_plugins_match_manifest` | 0.00 | the plugin list inlined in pc stack-doctor matches `reviewer-plugins.json` | migrate | `reviewer-plugins` repo scope (hk `reviewer-plugins-declared`) |
| `test_deps_audit::test_deps_audit_cli_help_and_repo_offline_json` (rc and items part) | 0.58 | `deps:audit --offline` exits 0 on the real repo | migrate | new doctor `deps-audit-offline` (`mise run deps:audit --offline`) |
| `test_dotfiles_py::test_datamodel_codegen_check_passes` | 0.60 | `datamodel-codegen --check` | done | hk `codegen-check` (also in pre-push) |
| `test_remote_ssh_lint::test_repo_tasks_are_clean` | 0.06 | lint over `tasks/hub` and `tasks/pc` | done | hk `remote-ssh-lint` |
| `test_metadata_gate_lint::test_repo_workflows_and_review_code_are_clean` | 0.03 | lint over `.github/openai-review` and workflows | done | hk `metadata-gate-lint` |
| `test_py39_compat::test_real_targets_pass_and_globs_match_ruff_override` | 0.07 | the real targets pass, and the hk glob equals ruff `per-file-target-version` | migrate | the real-targets part is already covered by hk `py39-compat`. Teach `tasks/py/py39_compat.py` to compare its globs with `pyproject.toml` |
| `test_py39_compat::test_pc_ollama_pin_matches_hub_up` | 0.00 | `config.pc.toml`, `mise.pc.lock` and `hub:up` agree on the ollama version | migrate | new doctor and hk `pin-parity` |

## Findings

1. **Moving these checks will not make the suite much faster.** In the serial
   run, the migrate and done tests that can be deleted outright take about 1 s
   of 53 s. Where only some assertions move, the saving is smaller still. Most
   of the suite's time goes to behavior tests that start subprocesses. The
   three slowest are `test_wrappers_and_tasks::test_agy_rc_guard_check_and_never_runs_serve_help`
   (4.4 s), `test_agy_code_review_guard::test_full_code_review_and_post_review_verification_gate`
   (4.0 s) and `test_worktree_issue_pwf::...::test_worktree_new_and_pwf_status_end_to_end`
   (3.3 s). Together they take 22 % of the serial time. To speed up the suite,
   split those tests and tune `PYTEST_XDIST_AUTO_NUM_WORKERS`.
2. **Duplicate runs.** The doctor check `herdr-roll` runs
   `unittest discover` over `herdr_roll/tests`, and pytest already collects
   those tests through `testpaths`. A doctor check should check setup, not run
   unit tests, so `herdr-roll` could keep only its executable-task and
   `SKILL.md` checks.
3. **Out of scope.** `.github/openai-review/test_*.py` is run by the hk step
   `openai-review-tests` (unittest), not by pytest, so it is not listed here.
4. **Suggested new checks**, each owning several assertions: `pin-parity`
   (ollama, Omarchy channel, other cross-file pins), `doctor-config`
   (`[doctor.checks]` wiring), `guest-path-order`, `stdlib-only`, `herdr-mods`
   and `deps-audit-offline`. Following AGENTS.md, add each one as a
   `tasks/py/doctor_checks.py` subcommand and call it from both
   `[doctor.checks]` and an hk step.
