# Modern `mise` Dotfiles & Ecosystem Reference Catalog

Canonical machine-readable index: [dotfiles-catalog.toml](./dotfiles-catalog.toml).

> [!NOTE]
> `dotfiles-v2/docs/references/dotfiles-catalog.toml` is the single versioned source of truth. Queue OpenWiki indexing requests for unindexed entries to `knowledge-base-lead-agy` rather than mutating `knowledge-base-v2` directly.

## Core `risu729` & `jdx` Ecosystem

- **[risu729/dotfiles](https://github.com/risu729/dotfiles)** (`kb_indexed=true`): Primary model for `mise bootstrap`, `[dotfile_groups]`, `[shell_alias]`, CI convergence/audit workflows, and Renovate pinning.
- **[risu729/hk-config](https://github.com/risu729/hk-config)** (`kb_indexed=true`), **[risu729/renovate-config](https://github.com/risu729/renovate-config)** (`kb_indexed=true`), **[risu729/skills](https://github.com/risu729/skills)**: Upstream models for `ray-manaloto/{hk-config,renovate-config,skills}`.
- **[jdx.dev/posts/2026-09-07-dotfiles-that-save-themselves](https://jdx.dev/posts/2026-09-07-dotfiles-that-save-themselves/)** (`kb_indexed=true`): Self-saving dotfiles architecture.
- **`jdx` tools** (`kb_indexed=true`): `jdx/mise`, `jdx/hk`, `jdx/fnox`, `jdx/pitchfork`, `jdx/jouet`, `jdx/usage`, `jdx/packslip`, `aubepkg/aube`, plus `jdx/renovate-config`.

## Modern `mise bootstrap`, Containers & CI References

| Repository | Focus | OpenWiki Indexed |
| --- | --- | --- |
| [lukehsiao/dotfiles](https://github.com/lukehsiao/dotfiles) | Omarchy, `mise` `conf.d`, Claude skills/settings | Yes |
| [xdanger/dotfiles](https://github.com/xdanger/dotfiles) | macOS LaunchAgent `mise` PATH, Codex marketplace, `gitconfig` | Yes |
| [omacom/omarchy](https://github.com/omacom/omarchy) | Remote Linux host (`pc`) `/etc/mise/conf.d` baseline | Yes |
| [roalcantara/dots](https://github.com/roalcantara/dots) | Container `mise bootstrap`, `conf.d` splits, `dpkg` pre-packages | No (queue) |
| [chancez/dotfiles](https://github.com/chancez/dotfiles) | Docker cache-warmer layer, CI `dot status --missing`, `aube` locks | No (queue) |
| [TechDufus/dotfiles](https://github.com/TechDufus/dotfiles) | 3x bootstrap convergence CI, shell smoke tests, Codex `config.toml` merge | No |
| [himkt/config](https://github.com/himkt/config) | macOS/Ubuntu CI matrix, release age soak, Renovate `mise` grouping | No |
| [nettlesh/dotfiles](https://github.com/nettlesh/dotfiles) | `min_version` hard/soft, `HK_MISE=1`, `final` hook, `skills` auto-sync | No (queue) |
| [guitsaru/dotfiles](https://github.com/guitsaru/dotfiles) | `pre-dotfiles` linked-worktree guard | No |
| [david-driscoll/dotfiles](https://github.com/david-driscoll/dotfiles) | `[secrets.fnox]`, `history-watch`, `skills` auto-sync | No |
| [iainsimmons/dotfiles](https://github.com/iainsimmons/dotfiles) | Release age soak, `[bootstrap.secrets]`, `copy` mode, per-machine `MISE_ENV` | No |
| [mikeastock/dotfiles](https://github.com/mikeastock/dotfiles) | `-E omarchy`, `pre-dotfiles` cleanup | No |
| [h-wb/dotfiles](https://github.com/h-wb/dotfiles) | `0600`/`0700` permissions, `fnox` templates | No |
| [ahstn/dotfiles](https://github.com/ahstn/dotfiles) | `[bootstrap.secrets]`, `fnox`, Tailscale `mise` tasks | No (queue) |
| [WeixuanZ/dotfiles](https://github.com/WeixuanZ/dotfiles), [Xevion/dotfiles](https://github.com/Xevion/dotfiles), [rios0rios0/dotfiles](https://github.com/rios0rios0/dotfiles), [divaltor/.dotfiles](https://github.com/divaltor/.dotfiles) | Secrets (`Doppler`/`age`/`fnox`) and Tailscale patterns | No |
| [wagov-dtt/devcontainer-base](https://github.com/wagov-dtt/devcontainer-base), [shepherdjerred/monorepo](https://github.com/shepherdjerred/monorepo), [statox/claude-devcontainer](https://github.com/statox/claude-devcontainer), [klab365/klab-pytest-toolkit](https://github.com/klab365/klab-pytest-toolkit), [naa0yama/rotel](https://github.com/naa0yama/rotel) | Container bootstrap, non-root `apt`, AI harness isolation | No |
| [lepinkainen/dotfiles](https://github.com/lepinkainen/dotfiles), [jkp/dotfiles](https://github.com/jkp/dotfiles), [shunk031/dotfiles](https://github.com/shunk031/dotfiles) | Cross-platform `mise` + `hk` dotfiles setups | No |
