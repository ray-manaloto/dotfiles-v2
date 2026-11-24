# shellcheck shell=sh
# Shared helpers for secret:* mise tasks.

clear_fnox_daemon_cache() {
	fnox_cfg="${FNOX_CONFIG_FILE:-${HOME}/.config/fnox/config.toml}"
	command -v fnox >/dev/null 2>&1 || return 0
	[ -f "${fnox_cfg}" ] || return 0

	clear_out=""
	if ! clear_out="$(fnox --non-interactive --config "${fnox_cfg}" daemon clear </dev/null 2>&1 >/dev/null)"; then
		flat="$(printf '%s\n' "${clear_out}" | sed 's/^[[:space:]]*│[[:space:]]*//' | tr '\n' ' ')"
		case "${flat}" in
		*"Failed to connect to fnox daemon at "*"No such file or directory"* | *"Failed to connect to fnox daemon at "*"Connection refused"*)
			return 0
			;;
		*)
			printf '%s\n' "${clear_out}" >&2
			return 1
			;;
		esac
	fi
}
