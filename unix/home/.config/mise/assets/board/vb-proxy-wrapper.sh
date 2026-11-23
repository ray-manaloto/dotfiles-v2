#!/usr/bin/env bash
set -euo pipefail

cmd="$(basename "$0")"
data_dir="${MISE_DATA_DIR:-${HOME}/.local/share/mise}"
bin=""
if command -v mise >/dev/null 2>&1; then
	bin="$(mise which "${cmd}" 2>/dev/null || true)"
fi
if [[ -z ${bin} || ! -x ${bin} ]]; then
	direct="${data_dir}/installs/npm-vercel-board/latest/node_modules/.bin/${cmd}"
	if [[ -x ${direct} ]]; then
		bin="${direct}"
	else
		bin="${data_dir}/shims/${cmd}"
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
