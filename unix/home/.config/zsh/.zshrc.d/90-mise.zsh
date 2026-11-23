# Exactly one interactive activation, after prompt/completion setup.
_mise_bin="$(command -v mise || true)"
if [[ -z $_mise_bin && -x "$HOME/.local/bin/mise" ]]; then
	_mise_bin="$HOME/.local/bin/mise"
fi
if [[ -x $_mise_bin ]]; then
	# The env cache can replay a stale resolution of a `{ default }` or get_env template, ignoring a
	# caller's override (jdx/mise#15751); rerun with MISE_ENV_CACHE=0 or `mise x --fresh-env`.
	export MISE_ENV_CACHE="${MISE_ENV_CACHE:-1}"
	eval "$("$_mise_bin" activate zsh)"
fi
unset _mise_bin
