# Shell helpers loaded after mise activation + compinit (10-zsh.zsh) and before starship (95-starship.zsh).
if [[ -r "$HOME/.config/dotfiles-v2/vm-env.sh" || ${MISE_ENV:-} == *vm* ]]; then
	unalias df-pc df-mac df-vm df-pc-exec df-mac-exec df-vm-exec kb-pc kb-mac cpp-pc cpp-mac 2>/dev/null || true
fi
if command -v zoxide >/dev/null 2>&1; then
	eval "$(zoxide init zsh)"
fi
if command -v pitchfork >/dev/null 2>&1; then
	eval "$(pitchfork activate zsh)"
fi
