# shellcheck shell=sh
for _as_file in \
	/run/agent-secrets/claude-oauth-token \
	"/Volumes/My Shared Files/agent-secrets/claude-oauth-token"; do
	if [ -s "${_as_file}" ]; then
		CLAUDE_CODE_OAUTH_TOKEN="$(cat "${_as_file}")"
		export CLAUDE_CODE_OAUTH_TOKEN
		break
	fi
done
unset _as_file
