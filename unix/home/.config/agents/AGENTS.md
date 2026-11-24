# Global Agent Rules

Portable user-level agent conventions managed by `dotfiles-v2` across macOS
hosts, Linux containers, and macOS VMs.

- **Agent communication:** For `agy <-> agy`, send full structured payloads via
  native `send_message`, discover live peer tabs dynamically by matching
  `--conversation <uuid>` in `hc ls --json` / `hc get <id> --json` (never
  hardcode stale tab IDs across `agy-hc` restarts), and send a 1-line
  `hc send <tab-id> "Wake doorbell: ..."` (`<= 220` chars) only when
  `status.state` is `"idle"` or `"done"` (omit `--if idle` when `"done"`). Never
  send payloads via `hc send`, and never touch `ANTIGRAVITY_CSRF_TOKEN`.
- **Containers:** Always pass `-e container=docker`; never pass `--privileged`.
- **Pre-ship code review & verification gate:** Before pushing, fast-forward
  merging into `main`/`master`, opening a pull request, or claiming completion on
  any non-documentation change, run Stage 1 `/code-review:code-review` (`+
  code-review-commons`, fanning out across read-only slices when diffs exceed
  ~1,000 lines), fix confirmed `CRITICAL`, `HIGH`, and `MEDIUM` findings, record
  Stage 1 via
  `python3 -I -B ~/.gemini/config/hooks/agy-code-review-guard.py --record --summary "..."`,
  and then run Stage 2 post-review behavioral verification via
  `python3 -I -B ~/.gemini/config/hooks/agy-code-review-guard.py --verify --run "<cmd>" --proof "<observed proof>"`.
  Use `/code-review:pr-code-review` on open GitHub pull requests.
- **Privacy and git hygiene:** Stage changed files explicitly by name (never
  `git add -A` or `git add .`). Never commit secrets, tokens, private
  hostnames (use `pc` or "remote Linux host"), or local `/Users/...` home
  paths. Never enable `set -x` / `bash -x` or print environment variables in
  shells where tokens or credentials are loaded.
- **Tooling scope and ecosystem:** Keep developer tools user-global under
  `~/.config/mise/config.toml` unless a tool is strictly project-specific.
  Prefer `mise`, `hk`, `fnox`, `aube`, and `jdx` ecosystem tools, native compiled
  `hk` builtins (`Rust`, `Go`, `Haskell`, `Pkl`), and `mise fmt` for mise
  configs.
- **Schemas and verification:** Declare public `$schema` / `#:schema` headers on
  supported config files. Prefer `mise doctor project` and `hk` checks/hooks
  over unit tests for upstream tools, git hooks, or config/setup verification
  (reserve unit tests for project code), and always inspect current docs or
  source before acting.
- **Schema-first Python & dual-sink logging:** In schema-first Python repos,
  declare all DTOs, enums, requests, and responses in Draft 2020-12 JSON Schema
  (`schemas/**/*.schema.json`) and generate Pydantic v2 models via
  `datamodel-codegen` instead of hand-writing `BaseModel`/`dataclass`/`Enum`
  types. Include 9-digit zero-padded nanosecond timestamps
  (`YYYYmmddTHHMMSS.NNNNNNNNN±HHMM`) and closed `errors[]`/`returns[]` entries
  on responses, log via dual-sink `structlog` + `orjson`, keep runtime
  dependencies slim (`pydantic`, `orjson`, `structlog`, `psutil`, `filelock`,
  `tenacity`, `cyclopts`), keep `/usr/bin/python3` Antigravity hooks stdlib-only
  (`3.9.6` syntax), and escalate repeated `/learn` issues (`>= 2x`) up the
  5-tier ladder (`hk.pkl -> mise doctor project -> hook -> Skill -> Rule`).
- **Research gate & error resolution:** Run multi-source research (local
  offline repos/docs, session/git history, OpenWiki, Context7, Exa, Firecrawl,
  Last30Days, and GitHub issues/PRs/discussions/code) before proposing custom
  builds, and report every route run plus any failed route (`Research checked:`).
  Root-cause and fix (or report exact remediation for) every tool, MCP, proxy,
  or permission failure first rather than ignoring or bypassing it.
- **Non-interactive runs and watchdogs:** Pass explicit non-interactive flags
  (`MISE_YES=1`, `--yes` / `-y`) to long-running commands, and pair any
  backgrounded build, sync, or upgrade task with a bounded `schedule` watchdog.
- **Reporting peer messages & receipt confirmation:** Always request explicit
  receipt confirmation when sending a message to another agent. When reporting
  peer messages to the owner, name the target as `tab-name (id)`, state the
  channel (`native send_message`, plus `hc send` idle doorbell if sent), and
  clearly distinguish **dispatched (receipt pending)** from **receipt
  confirmed**.
- **OpenWiki search routing:** Use the `openwiki_search` / `openwiki_read` MCP
  tools for workspace-scoped queries (`--workspace` is not an `openwiki` CLI
  flag).

## Prefer native hk builtins and compiled tools

Before writing a custom Python or shell check, search `hk builtins` for a native
`Builtins.*` step and prefer compiled native tools (`Rust`, `C++`, `C`, `Zig`,
`Go` — such as `betterleaks`, `rg`, `ruff`, `ty`, `tombi`, `jq`, `mise`,
`agnix`, `jactionlint`, `zizmor`, `typos`, `hk util`) when available in the
toolchain.

## Watched agents open as herdr-app tabs

Open any agent the owner may watch as a tab in the current `herdr-app` space
instead of a background popup:
`herdr-app do tab.new --space <id> --name <name> --folder <dir> --argv '["codex-hc", "<task>"]'`.
Always start Codex as `codex-hc` (so `fnox` proxy, `--disable daemon_auto_start`,
and `HC_*` environment variables apply). Pass an initial prompt when opening a
Claude tab (`--task` or final `--argv` element), verify delivery with
`hc read <t>`, close temporary tabs when finished, and always refer to terminals
as `tab-name (id)`.

## Environment credentials and Doppler

Use `fnox --non-interactive proxy run -- <cmd>` when a command requires managed
provider authentication (`fnox exec`, `fnox get`, and `fnox export` are disabled
for agents, and avoid `fnox set` on a Doppler provider). Manage Doppler entries
only through the `secret` mise tasks (`pbpaste | mise run secret set` or the
corresponding `secret` subcommands `set --replace`, `delete` with owner
approval, `list`, and `doctor`).

## Claude plugin changes

Install, update, enable, or disable Claude Code plugins via `claude plugin` CLI
only instead of editing `~/.claude/plugins/**`. Uninstall only with
`claude plugin uninstall --keep-data` unless the owner explicitly approves data
deletion, and verify with `claude plugin list --json`.

## Scratch, Spotlight & remote Linux host (`pc`) upgrades

- **Mac Spotlight:** `~/dev` is excluded from Spotlight (`~/dev/tmp/<task>` for
  scratch); outside `~/dev`, place bulky clones, caches, and build trees inside
  directories ending in `.noindex`.
- **Remote Linux host (`pc`) upgrades:** Upgrade `pc` only with owner approval
  via `OMARCHY_PATH=/usr/share/omarchy omarchy update -y` instead of
  `pacman -Syu`.

## Large reviews fan out

Split diffs exceeding ~1,000 changed lines across up to 6 parallel read-only
subagents covering the full `(file, rule)` checklist, verify every reported
finding directly at `path:line`, and merge deduplicated confirmed findings.

## Context7 docs lookup

When looking up library, framework, SDK, API, or CLI syntax/configuration, call
Context7 `resolve-library-id` first and run focused `query-docs` calls per
concept rather than relying on stale memory or generic web search.
