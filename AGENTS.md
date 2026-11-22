# dotfiles-v2

Clean-room `mise`-managed dotfiles, Ubuntu 26.10 C++26 container, and AI harness
setup.

- **Layout:** Dockerfiles under `docker/`, managed home files under
  `unix/home/`, mise file tasks under `tasks/`. Never put code or Dockerfiles
  under `docs/`.
- **Prototype workflow:** Direct push to `main` while Steps 0–3 are in
  progress, guarded locally by `hk check --all` (includes `betterleaks`). Stage
  files by name; never `git add -A` or `git add .`.
- **Secrets and privacy:** Never commit secrets, tokens, private hostnames
  (use `pc` or "remote Linux host"), or local home paths.
- **Containers:** Always run with `-e container=docker`; never pass
  `--privileged`. Sync running container via `mise run container:sync`.
