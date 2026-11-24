---
name: find-docs
description: >-
  Use when retrieving up-to-date library, framework, SDK, and CLI documentation
  via the pinned `ctx7` CLI and `llms.txt` instead of Context7 MCP servers or
  stale training memory.
---

# Documentation Lookup (`ctx7` CLI)

Use the `mise`-locked `ctx7` CLI (`0.13.0`) under scoped `fnox` research proxy (or `CONTEXT7_BASE_URL` on the `pc` gateway) for library resolution, versioned docs, and upgrade guides.

## Guardrails

- **NEVER** run `ctx7 setup --antigravity` or unpinned `npx` package installs — upstream `--antigravity` writes `~/.agent/skills/` (singular) and mutates symlinked `~/.gemini/GEMINI.md`. This skill is managed canonically in `dotfiles-v2`.
- Keep `NODE_EXTRA_CA_CERTS` intact when running `ctx7` under `fnox proxy run` (Node 26 native `fetch` fails TLS if `NODE_EXTRA_CA_CERTS` is unset).
- Limit queries to at most 3 attempts per question and never embed secrets or private paths inside queries.

## Workflow

1. Resolve the Context7 library ID (`/org/project`) first unless already known:

```sh
fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
  ctx7 library mise "minimum_release_age per tool configuration"
```

2. Fetch targeted documentation (`--tag` selects an exact version when known) or multi-library search (`ctx7 search`, CLI-only):

```sh
fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
  ctx7 docs /jdx/mise "hooks postinstall dotfile_groups" --tag 2026.11.14

fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
  ctx7 search "per-tool minimum_release_age" --library /jdx/mise --json

fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
  ctx7 get-upgrade-guide /vercel/next.js 15 16
```

3. For guests (`2–4`) using the keyless `pc` Context7 proxy + Tailscale SOCKS/HTTP listener:

```sh
env -u ALL_PROXY -u all_proxy HTTPS_PROXY=http://127.0.0.1:1055 NODE_USE_ENV_PROXY=1 \
  CONTEXT7_BASE_URL="$CONTEXT7_BASE_URL" ctx7 docs /jdx/mise "secrets.fnox"
```
