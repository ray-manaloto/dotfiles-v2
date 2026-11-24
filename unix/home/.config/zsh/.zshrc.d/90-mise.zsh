# Exactly one interactive activation, after completion setup and before starship prompt.
_mise_bin="$(command -v mise || true)"
if [[ -z $_mise_bin && -x "$HOME/.local/bin/mise" ]]; then
	_mise_bin="$HOME/.local/bin/mise"
elif [[ -z $_mise_bin && -x /usr/local/bin/mise ]]; then
	_mise_bin="/usr/local/bin/mise"
fi
if [[ -n $_mise_bin && -x $_mise_bin ]]; then
	eval "$("$_mise_bin" activate zsh)"
fi
unset _mise_bin
if [[ -r "$HOME/.config/dotfiles-v2/vm-env.sh" || ${MISE_ENV:-} == *vm* ]]; then
	unalias df-pc df-mac df-vm df-pc-exec df-mac-exec df-vm-exec kb-pc kb-mac cpp-pc cpp-mac 2>/dev/null || true
fi
if command -v zoxide >/dev/null 2>&1; then
	eval "$(zoxide init zsh)"
fi
if command -v pitchfork >/dev/null 2>&1; then
	eval "$(pitchfork activate zsh)"
fi
