# shellcheck shell=sh
if [ -s /run/agent-secrets/claude-oauth-token ]; then
	CLAUDE_CODE_OAUTH_TOKEN="$(cat /run/agent-secrets/claude-oauth-token)"
	export CLAUDE_CODE_OAUTH_TOKEN
fi
