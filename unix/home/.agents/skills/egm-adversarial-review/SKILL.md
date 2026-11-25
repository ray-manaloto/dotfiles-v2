---
name: egm-adversarial-review
description: Run cost-optimized, fresh-context Elephant-Goldfish Model (EGM) adversarial reviews across Claude Code, OpenAI Codex, and Antigravity with mandatory harness_rev fingerprinting and multi-source research gates.
---

# EGM Adversarial Review (Cross-Harness Tuned Protocol)

Execute isolated, read-only adversarial reviews based on `google/elephant-goldfish-model` (pinned at `46933754e63c`; upstream release tracker Issue `#29`, PWF `plan_id: egm-upstream-release-tracking`) with strict model/effort cost controls and reproducible `harness_rev` stamping.

## 1. Stamp Every Report With `harness_rev`

Prepend this metadata header to every EGM review report so Ring 0–3 re-reviews remain bounded and selective:

- `harness_rev`: short git SHA of `unix/home/{.agents,.claude,.codex,.gemini,.config/mise/assets}` + active CLI version (`claude --version`, `codex --version`, or `agy --version`) + `egm_rev` (`46933754e63c` until Issue `#29` switches to an attested `v0.1.2+` tag).
- **Selective re-review triggers (do NOT blanket re-run):** Re-run an LLM adversarial review only when **(a)** the model family or effort routing changes, **(b)** a sandbox, isolation, or tool-access boundary changes, or **(c)** core `AGENTS.md` / EGM rubric standards change materially. Routine tweaks are verified by deterministic `hk`, `mise doctor project`, and behavioral checks.

## 2. Tiered Model / Effort Routing (Spare Flagship Quotas)

Never fan out all Goldfish reviewers on `fable` or unrestricted `gpt-6.2-astra`. Always pin explicit model + effort per reviewer role and keep synthesis in the lead session:

### Claude Code (`2.1.332+`, lead on `opus` / `medium`, `--advisor opus`)

Pass `--advisor fable` only on final review-gate sessions when flagship verification is explicitly required. All 8 critic personas use plugin-pinned `tools: [Read, Grep, Glob]`:

| Agent Type | Tier | Explicit `model` / `effort` |
|---|---|---|
| `egm:egm-design`, `egm:egm-inventive-catalyst` | Critic | `opus` / `medium` |
| `egm:egm-codebase-details`, `egm:egm-implementation-readiness`, `egm:egm-zoom-out` | Goldfish | `sonnet` / `medium` |
| `egm:egm-comprehension`, `egm:egm-structural` | Goldfish | `haiku` / `medium` |
| `egm:egm-style` | Goldfish | `haiku` / `low` |
| `egm:egm-synthesizer` | Lead only | Do not spawn in read-only audits; lead synthesizes |

### OpenAI Codex (`0.172.0+`, parent launched with `--profile review`)

Launch read-only review sessions with `codex --profile review` (`sandbox_mode = "read-only"`, `approval_policy = "never"`) because Codex role projection ignores `sandbox_mode` and `mcp_servers` inside `~/.codex/agents/*.toml`. Spawn tracked `egm-*` roles using **V2 `fork_turns: "none"`** (V2 rejects V1 `fork_context: false`):

| Tracked Role (`~/.codex/agents/`) | Model / Effort |
|---|---|
| `egm-design`, `egm-inventive-catalyst` | `gpt-6.2-astra` / `low` |
| `egm-codebase-details`, `egm-implementation-readiness`, `egm-zoom-out` | `gpt-6.2-sol` / `medium` |
| `egm-comprehension`, `egm-structural`, `egm-style` | `gpt-6-luna` / `high` |

### Antigravity CLI (`1.5.17+`)

Invoke read-only subagents (`egm-design`, `egm-inventive-catalyst`, `egm-codebase-details`, `egm-implementation-readiness`, `egm-zoom-out`, `egm-comprehension`, `egm-structural`, `egm-style`) with `Model: "pro"` for architecture/TRIZ/readiness, `"flash"` for structural/comprehension, and `"flash_lite"` for style passes. Keep `~/.gemini/config/plugins/egm/skills/egm-review/SKILL.md` under AGY's 24 KB skill cutoff (`agy-skill-cutoff.py`).

## 3. Research & Verification Stack

Before ranking findings:

1. Cross-check primary files/source with exact `path:line` and tag every claim `[V]` (verified directly), `[S]` (single-reviewer claim), or `[U]` (runtime check still needed).
2. Run the repo's research routes as needed: `ctx7` CLI, `openwiki_search` (always pass `root` + explicit `workspace`), `exa-search` / `firecrawl` / `webclaw`, `agentsview session search --server ... --server-token-file ...`, `gh api`/`gh search`, and `last30days` (with `--plan` / `--research-only`).
3. Report every route checked and any failed/blocked route in `Research checked:`.
4. Verify git working tree cleanliness (`git status --short`) before completing read-only audits.
