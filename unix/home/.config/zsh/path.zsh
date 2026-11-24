# Reapply after macOS /etc/zprofile runs path_helper. No external commands.
typeset -U path
# Remove retired managers, ambient Homebrew build-tool paths, and AGY subshell bin prefix before mise paths.
path=("${(@)path:#$HOME/.cache/oh-my-opencode/bin}")
path=("${(@)path:#$HOME/.gemini/antigravity-cli/bin}")
path=("${(@)path:#/usr/local/Homebrew/*}")
path=("${(@)path:#/usr/local/opt/*}")
_inherited=("${(@)path[1,${path[(i)(/usr/bin|/bin|/usr/sbin|/sbin)]}-1]}")
path=("$HOME/.local/bin" "$XDG_DATA_HOME/mise/shims" /usr/local/share/mise/shims
	/opt/homebrew/bin /opt/homebrew/sbin /usr/bin /bin /usr/sbin /sbin $path
	"$HOME/.docker/bin"
	"$HOME/.opencode/bin"
	"$HOME/Library/Application Support/JetBrains/Toolbox/scripts"
	/Applications/Obsidian.app/Contents/MacOS
	"$HOME/.antigravity-ide/antigravity-ide/bin"
	"$HOME/.gemini/antigravity-cli/bin")
# Only a login shell takes this order: elsewhere what `mise run` or a venv put first stays first,
# and the system folders, which any shell inherits, stay behind the shims.
[[ -o login ]] || path=($_inherited $path)
unset _inherited
export PATH
