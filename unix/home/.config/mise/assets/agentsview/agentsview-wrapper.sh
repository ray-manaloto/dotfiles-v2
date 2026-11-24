#!/usr/bin/env bash
set -euo pipefail

data_dir="${MISE_DATA_DIR:-${HOME}/.local/share/mise}"
bin=""
if command -v mise >/dev/null 2>&1; then
	bin="$(mise which --tool github:kenn-io/agentsview agentsview 2>/dev/null || true)"
	if [[ -z ${bin} || ${bin} == */command-wrappers/* || ${bin} == */.local/bin/* || ! -x ${bin} ]]; then
		candidate="$(mise which agentsview 2>/dev/null || true)"
		if [[ -n ${candidate} && ${candidate} != */command-wrappers/* && ${candidate} != */.local/bin/* && -x ${candidate} ]]; then
			bin="${candidate}"
		else
			bin=""
		fi
	fi
fi
if [[ -z ${bin} || ! -x ${bin} ]]; then
	latest_match="$(
		for match in "${data_dir}/installs/github-kenn-io-agentsview"/*/agentsview; do
			[[ -x ${match} ]] && printf '%s\n' "${match}"
		done | sort -V | tail -n 1 || true
	)"
	if [[ -n ${latest_match} && -x ${latest_match} ]]; then
		bin="${latest_match}"
	else
		bin="${data_dir}/shims/agentsview"
	fi
fi
if [[ -z ${bin} || ! -x ${bin} ]]; then
	echo "agentsview-wrapper: executable 'agentsview' not found (install github:kenn-io/agentsview via mise)" >&2
	exit 127
fi

orig_server="${AGENTSVIEW_SERVER-}"
orig_token_file="${AGENTSVIEW_SERVER_TOKEN_FILE-}"
orig_skills_server="${AGENTSVIEW_SKILLS_SERVER-}"
orig_skills_token="${AGENTSVIEW_SKILLS_SERVER_TOKEN_FILE-}"
orig_no_daemon="${AGENTSVIEW_NO_DAEMON-}"
if [[ -r "${HOME}/.config/agentsview/env.sh" ]]; then
	# shellcheck source=/dev/null
	. "${HOME}/.config/agentsview/env.sh"
fi
if [[ -n ${orig_server} ]]; then
	export AGENTSVIEW_SERVER="${orig_server}"
fi
if [[ -n ${orig_token_file} ]]; then
	export AGENTSVIEW_SERVER_TOKEN_FILE="${orig_token_file}"
fi
if [[ -n ${orig_skills_server} ]]; then
	export AGENTSVIEW_SKILLS_SERVER="${orig_skills_server}"
fi
if [[ -n ${orig_skills_token} ]]; then
	export AGENTSVIEW_SKILLS_SERVER_TOKEN_FILE="${orig_skills_token}"
fi
if [[ -n ${orig_no_daemon} ]]; then
	export AGENTSVIEW_NO_DAEMON="${orig_no_daemon}"
fi

is_ts_target=0
if [[ ${AGENTSVIEW_SERVER:-} == https://*.ts.net/* ]]; then
	is_ts_target=1
fi
for arg in "$@"; do
	case "${arg}" in
	--server=https://*.ts.net/* | https://*.ts.net/*)
		is_ts_target=1
		;;
	esac
done

if [[ ${is_ts_target} -eq 1 ]]; then
	if (exec 3<>/dev/tcp/127.0.0.1/1055) 2>/dev/null; then
		export HTTPS_PROXY="http://127.0.0.1:1055"
		export HTTP_PROXY="http://127.0.0.1:1055"
		export https_proxy="http://127.0.0.1:1055"
		export http_proxy="http://127.0.0.1:1055"
		unset ALL_PROXY all_proxy
	else
		# Native-Tailscale hosts (macOS host) must bypass fnox localhost CONNECT proxy, which rejects *.ts.net with 403.
		unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy ALL_PROXY all_proxy
	fi
fi

# Route supported client read/skill commands to the offloaded pc Hub when configured.
subcmd="${1:-}"
subcmd2="${2:-}"
inject_server=0
case "${subcmd}" in
session | recall | why | friction | mcp)
	inject_server=1
	;;
skills)
	if [[ ${subcmd2} == "install" ]]; then
		inject_server=1
	fi
	;;
esac

if [[ ${inject_server} -eq 1 && -n ${AGENTSVIEW_SERVER:-} ]]; then
	has_explicit_target=0
	has_help=0
	for arg in "$@"; do
		case "${arg}" in
		--server | --server=* | --pg | --pg=*)
			has_explicit_target=1
			;;
		-h | --help | -v | --version)
			has_help=1
			;;
		esac
	done
	if [[ ${has_explicit_target} -eq 0 && ${has_help} -eq 0 ]]; then
		shift
		extra=(--server "${AGENTSVIEW_SERVER}")
		if [[ -n ${AGENTSVIEW_SERVER_TOKEN_FILE:-} && -r ${AGENTSVIEW_SERVER_TOKEN_FILE} ]]; then
			extra+=(--server-token-file "${AGENTSVIEW_SERVER_TOKEN_FILE}")
		fi
		exec "${bin}" "${subcmd}" "${extra[@]}" "$@"
	fi
fi

exec "${bin}" "$@"
