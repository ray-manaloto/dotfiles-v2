# Reapply after macOS /etc/zprofile or Linux /etc/zsh/zprofile runs path_helper. No external commands.
typeset -U path
# Remove retired managers, ambient Homebrew build-tool paths, and AGY subshell bin prefix before mise paths.
path=("${(@)path:#$HOME/.cache/oh-my-opencode/bin}")
path=("${(@)path:#$HOME/.gemini/antigravity-cli/bin}")
if [[ $OSTYPE == darwin* ]]; then
	path=("${(@)path:#/usr/local/Homebrew/*}")
	path=("${(@)path:#/usr/local/opt/*}")
fi
_inherited=("${(@)path[1,${path[(i)(/usr/bin|/bin|/usr/sbin|/sbin)]}-1]}")
_mise_shims="${MISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/mise}/shims"
_core_prefix=("$HOME/.local/bin" "$HOME/.opencode/bin" "$_mise_shims")
[[ -d /usr/local/share/mise/shims ]] && _core_prefix+=(/usr/local/share/mise/shims)
_core_prefix+=(/usr/local/bin)
if [[ $OSTYPE == darwin* ]]; then
	_core_prefix+=(/opt/homebrew/bin /opt/homebrew/sbin)
fi
path=($_core_prefix /usr/bin /bin /usr/sbin /sbin $path)
[[ -d "$HOME/.docker/bin" ]] && path+=("$HOME/.docker/bin")
if [[ $OSTYPE == darwin* ]]; then
	[[ -d "$HOME/Library/Application Support/JetBrains/Toolbox/scripts" ]] && path+=("$HOME/Library/Application Support/JetBrains/Toolbox/scripts")
	[[ -d /Applications/Obsidian.app/Contents/MacOS ]] && path+=(/Applications/Obsidian.app/Contents/MacOS)
	if [[ ! -r "$HOME/.config/dotfiles-v2/vm-env.sh" && ${MISE_ENV:-} != *vm* ]]; then
		[[ -d "$HOME/.local/share/hermes-cli.noindex/bin" ]] && path+=("$HOME/.local/share/hermes-cli.noindex/bin")
		[[ -d /Applications/Orca.app/Contents/Resources/bin ]] && path+=(/Applications/Orca.app/Contents/Resources/bin)
	fi
fi
[[ -d "$HOME/.antigravity-ide/antigravity-ide/bin" ]] && path+=("$HOME/.antigravity-ide/antigravity-ide/bin")
[[ -d "$HOME/.gemini/antigravity-cli/bin" ]] && path+=("$HOME/.gemini/antigravity-cli/bin")
# Only a login shell takes this order: elsewhere what `mise run` or a venv put first stays first,
# and the system folders, which any shell inherits, stay behind the shims.
[[ -o login ]] || path=($_inherited $path)
unset _inherited _mise_shims _core_prefix
export PATH
