# A dumb terminal can't draw starship's prompt, and starship says so on stderr at every prompt (1.27.0).
# Ordered after 90-mise.zsh so mise/zoxide/pitchfork hooks run before prompt rendering.
if [[ $TERM != dumb ]] && command -v starship >/dev/null 2>&1; then
	eval "$(starship init zsh)"
fi
