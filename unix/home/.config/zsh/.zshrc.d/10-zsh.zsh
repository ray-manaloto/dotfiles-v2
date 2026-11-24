typeset -U fpath chpwd_functions precmd_functions
export ZSH_CACHE_DIR="${ZSH_CACHE_DIR:-$XDG_CACHE_HOME/zsh}"
export ZSH_COMPDUMP="$ZSH_CACHE_DIR/.zcompdump-$ZSH_VERSION"
HISTFILE="$XDG_STATE_HOME/zsh/history"
mkdir -p "${HISTFILE:h}" "$ZSH_CACHE_DIR" "$ZSH_CACHE_DIR/completions"
HISTSIZE=50000
SAVEHIST=10000
setopt HIST_IGNORE_DUPS HIST_IGNORE_SPACE SHARE_HISTORY

_df_refresh_zsh_comp() {
	local tool="$1" out="$ZSH_CACHE_DIR/completions/_$1" bin
	shift
	bin="$(command -v "$tool" 2>/dev/null || true)"
	[[ -n $bin ]] || return 0
	if [[ ! -s $out || $bin -nt $out ]]; then
		local tmp
		tmp="$(mktemp "$out.XXXXXX")" || return 0
		if "$@" >"$tmp" 2>/dev/null && [[ -s $tmp ]]; then
			mv -f "$tmp" "$out"
		else
			rm -f "$tmp"
		fi
	fi
}
_df_refresh_zsh_comp mise mise completion zsh
_df_refresh_zsh_comp gh gh completion -s zsh
_df_refresh_zsh_comp hk hk completion zsh
_df_refresh_zsh_comp pitchfork pitchfork completion zsh
_df_refresh_zsh_comp fnox fnox completion zsh
unset -f _df_refresh_zsh_comp

if [[ $OSTYPE == darwin* && ! -r "$HOME/.config/dotfiles-v2/vm-env.sh" ]]; then
	[[ -d "$HOME/.local/share/hermes-cli.noindex/bin" ]] && path=("$HOME/.local/share/hermes-cli.noindex/bin" $path)
	[[ -d /Applications/Orca.app/Contents/Resources/bin ]] && path=(/Applications/Orca.app/Contents/Resources/bin $path)
fi

if [[ $OSTYPE == darwin* && -d /opt/homebrew/share/zsh/site-functions ]]; then
	fpath=(/opt/homebrew/share/zsh/site-functions $fpath)
fi
for _df_comp_dir in "$ZSH_CACHE_DIR/completions" "$HOME/.oh-my-zsh/custom/completions" "$HOME/.docker/completions"; do
	[[ -d $_df_comp_dir ]] && fpath=("$_df_comp_dir" $fpath)
done
unset _df_comp_dir

export LS_COLORS="${LS_COLORS:-di=1;36:ln=35:so=32:pi=33:ex=31:bd=34;46:cd=34;43:su=30;41:sg=30;46:tw=30;42:ow=30;43}"
[[ $OSTYPE == darwin* ]] && export CLICOLOR=1
setopt AUTO_CD
autoload -Uz compinit up-line-or-beginning-search down-line-or-beginning-search
if [[ ${container:-} == docker || $OSTYPE == linux* ]]; then
	compinit -u -d "$ZSH_COMPDUMP"
else
	compinit -d "$ZSH_COMPDUMP"
fi
zstyle ':completion:*' menu select
zstyle ':completion:*' matcher-list 'm:{a-zA-Z}={A-Za-z}'
zle -N up-line-or-beginning-search
zle -N down-line-or-beginning-search
bindkey -e
bindkey '^[[A' up-line-or-beginning-search '^[OA' up-line-or-beginning-search
bindkey '^[[B' down-line-or-beginning-search '^[OB' down-line-or-beginning-search
