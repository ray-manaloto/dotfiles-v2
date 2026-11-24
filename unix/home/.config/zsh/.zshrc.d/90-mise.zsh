# Exactly one interactive activation, after prompt/completion setup.
_mise_bin="$(command -v mise || true)"
if [[ -z $_mise_bin && -x "$HOME/.local/bin/mise" ]]; then
	_mise_bin="$HOME/.local/bin/mise"
fi
if [[ -x $_mise_bin ]]; then
	eval "$("$_mise_bin" activate zsh)"
fi
unset _mise_bin
