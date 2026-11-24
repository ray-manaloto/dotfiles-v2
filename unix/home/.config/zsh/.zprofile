# /etc/zprofile has already run path_helper; restore our declared order.
if [[ -r "${ZDOTDIR:-$HOME/.config/zsh}/path.zsh" ]]; then
	source "${ZDOTDIR:-$HOME/.config/zsh}/path.zsh"
fi
if [[ -r "$HOME/.config/dotfiles-v2/vm-env.sh" ]]; then
	source "$HOME/.config/dotfiles-v2/vm-env.sh"
fi
