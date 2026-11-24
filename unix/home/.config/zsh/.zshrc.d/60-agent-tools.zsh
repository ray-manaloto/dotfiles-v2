# Codex and OpenCode start under fnox proxy run through the mise [wrappers] in ~/.config/mise/config.toml,
# so no shell function stands in front of them here.
if [[ -r "${HOME}/.config/board/env.sh" ]]; then
	. "${HOME}/.config/board/env.sh"
fi
