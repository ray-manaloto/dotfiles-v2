---
name: exa-search
description: >-
  Use when querying Exa semantic/deep web search (`POST /search`) or clean page
  extraction (`POST /contents`) directly via `curl` through scoped `fnox`
  research proxy or `pc` gateway with zero MCP schema overhead.
---

# Exa Search & Contents (`api.exa.ai`)

Use `curl` against `https://api.exa.ai/search` and `https://api.exa.ai/contents` instead of `exa` MCP tools. Direct REST exposes `POST /answer`, `type: "deep"`, `includeDomains: ["github.com"]`, soft `"category"` hints, `snapshotAsOf`, and `outputSchema` without per-turn MCP schema bloat.

## Authentication & Transport

- **macOS host (`fnox` Arch B strict research profile):**
  `fnox` automatically injects `EXA_API_KEY` into `x-api-key` when `fnox-placeholder-exa-mcp` is sent to `api.exa.ai`. Never print or run `fnox get`/`exec`/`export`.

- **Guests / VM (`2–4`) over `pc` gateway / Tailscale `:1055`:**
  Use `EXA_BASE_URL` pointing to the `pc` Search/Exa gateway when available, or route via `env -u ALL_PROXY -u all_proxy HTTPS_PROXY=http://127.0.0.1:1055`.

## Search (`POST /search`)

```sh
exa_auth_hdr="x-api-key: fnox-placeholder-exa-mcp"
fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
  curl -fsS -X POST "${EXA_BASE_URL:-https://api.exa.ai}/search" \
  -H "Content-Type: application/json" \
  -H "${exa_auth_hdr}" \
  -d '{
    "query": "mise lockfile minimum_release_age per tool",
    "type": "auto",
    "numResults": 5,
    "contents": {
      "highlights": true
    }
  }'
```

Useful filters:

- `"includeDomains"` / `"excludeDomains"` (use `["github.com"]` to strictly scope to GitHub), `"startPublishedDate"` (`YYYY-MM-DD`).
- `"category"`: soft classification hint (`"github"`, `"news"`, `"publication"`, `"company"`, `"people"`, `"financial report"`; pair with `"includeDomains"` when strict domain filtering is required).
- `"type"`: `"auto"`, `"lite"`, `"deep-lite"`, or `"deep"`.
- `"contents"`: `{"highlights": true}` for concise grounded snippets, or `{"text": {"maxCharacters": 2000}, "maxAgeHours": 24}` for full markdown sections.

## Contents Extraction (`POST /contents`)

```sh
exa_auth_hdr="x-api-key: fnox-placeholder-exa-mcp"
fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
  curl -fsS -X POST "${EXA_BASE_URL:-https://api.exa.ai}/contents" \
  -H "Content-Type: application/json" \
  -H "${exa_auth_hdr}" \
  -d '{
    "urls": ["https://mise.jdx.dev/configuration.html"],
    "text": { "maxCharacters": 4000 }
  }'
```
