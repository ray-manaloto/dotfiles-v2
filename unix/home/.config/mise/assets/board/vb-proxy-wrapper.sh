#!/usr/bin/env bash
set -euo pipefail

cmd="$(basename "$0")"
data_dir="${MISE_DATA_DIR:-${HOME}/.local/share/mise}"
bin=""
if command -v mise >/dev/null 2>&1; then
	bin="$(mise which --tool npm:@vercel/board "${cmd}" 2>/dev/null || true)"
	if [[ -z ${bin} || ${bin} == */command-wrappers/* || ! -x ${bin} ]]; then
		candidate="$(mise which "${cmd}" 2>/dev/null || true)"
		if [[ -n ${candidate} && ${candidate} != */command-wrappers/* && -x ${candidate} ]]; then
			bin="${candidate}"
		else
			bin=""
		fi
	fi
fi
if [[ -z ${bin} || ! -x ${bin} ]]; then
	direct="${data_dir}/installs/npm-vercel-board/latest/node_modules/.bin/${cmd}"
	if [[ -x ${direct} ]]; then
		bin="${direct}"
	else
		for match in "${data_dir}/installs/npm-vercel-board"/*/node_modules/.bin/"${cmd}"; do
			if [[ -x ${match} ]]; then
				bin="${match}"
				break
			fi
		done
		if [[ -z ${bin} || ! -x ${bin} ]]; then
			bin="${data_dir}/shims/${cmd}"
		fi
	fi
fi

orig_board_url="${BOARD_URL-}"
orig_board_token="${BOARD_TOKEN-}"
if [[ -r "${HOME}/.config/board/env.sh" ]]; then
	# shellcheck source=/dev/null
	. "${HOME}/.config/board/env.sh"
fi
if [[ -n ${orig_board_url} ]]; then
	export BOARD_URL="${orig_board_url}"
fi
if [[ -n ${orig_board_token} ]]; then
	export BOARD_TOKEN="${orig_board_token}"
fi

if [[ ${BOARD_URL:-} == https://*.ts.net/* ]] && (exec 3<>/dev/tcp/127.0.0.1/1055) 2>/dev/null; then
	export NODE_OPTIONS="${NODE_OPTIONS:+${NODE_OPTIONS} }--use-env-proxy"
	export HTTPS_PROXY="http://127.0.0.1:1055"
	export HTTP_PROXY="http://127.0.0.1:1055"
	export https_proxy="http://127.0.0.1:1055"
	export http_proxy="http://127.0.0.1:1055"
	unset ALL_PROXY all_proxy
fi

exec "${bin}" "$@"
