# shellcheck shell=sh
export container=docker
export CLAUDE_CONFIG_DIR=/home/dev/.claude
export ZCODE_CREDENTIAL_SECRET_FILE=/home/dev/.zcode/v2/credential-secret
export MISE_DATA_DIR=/opt/mise
export MISE_CACHE_DIR=/opt/mise/cache
export MISE_ENV=container
export MISE_YES=1
export MISE_TRUSTED_CONFIG_PATHS=/workspace:/home/dev/.config/mise
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
for _df_p in /home/dev/.local/bin /home/dev/.opencode/bin /opt/mise/shims; do
	_df_prepend_path "${_df_p}"
done
unset _df_p _df_new _df_old
unset -f _df_prepend_path
export PATH
if [ -r "${HOME}/.config/board/env.sh" ]; then
	# shellcheck source=/dev/null
	. "${HOME}/.config/board/env.sh"
fi
