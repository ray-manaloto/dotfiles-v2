#!/usr/bin/env bash
set -euo pipefail

cmd="$(basename "$0")"
data_dir="${MISE_DATA_DIR:-${HOME}/.local/share/mise}"
direct="${data_dir}/installs/npm-vercel-board/latest/node_modules/.bin/${cmd}"
bin="${data_dir}/shims/${cmd}"
if [[ -x ${direct} ]]; then
	bin="${direct}"
fi

if [[ -r "${HOME}/.config/board/env.sh" ]]; then
	# shellcheck source=/dev/null
	. "${HOME}/.config/board/env.sh"
fi

if [[ ${BOARD_URL:-} == https://*.ts.net/* ]] && (exec 3<>/dev/tcp/127.0.0.1/1055) 2>/dev/null; then
	exec 3>&-
	export NODE_OPTIONS="${NODE_OPTIONS:+${NODE_OPTIONS} }--use-env-proxy"
	export HTTPS_PROXY="http://127.0.0.1:1055"
	export HTTP_PROXY="http://127.0.0.1:1055"
fi

exec "${bin}" "$@"
