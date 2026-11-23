# shellcheck shell=sh
_df_prepend_path() {
	_df_new="$1"
	_df_old=":${PATH}:"
	while :; do
		case "${_df_old}" in
		*":${_df_new}:"*) _df_old="${_df_old%%":${_df_new}:"*}:${_df_old#*":${_df_new}:"}" ;;
		*) break ;;
		esac
	done
	_df_old="${_df_old#:}"
	_df_old="${_df_old%:}"
	PATH="${_df_new}${_df_old:+:${_df_old}}"
}
for _df_p in /opt/homebrew/sbin /opt/homebrew/bin /usr/local/bin "${HOME}/.local/share/mise/shims" "${HOME}/.opencode/bin" "${HOME}/.local/bin"; do
	_df_prepend_path "${_df_p}"
done
unset _df_p _df_new _df_old
unset -f _df_prepend_path
export PATH
if [ -r "${HOME}/.config/agent-secrets/env.sh" ]; then
	# shellcheck source=/dev/null
	. "${HOME}/.config/agent-secrets/env.sh"
fi
tailscale() {
	/opt/homebrew/bin/tailscale --socket="${HOME}/.local/state/tailscale/tailscaled.sock" "$@"
}
