# shellcheck shell=sh
export container=docker
export CLAUDE_CONFIG_DIR=/home/dev/.claude
export MISE_DATA_DIR=/opt/mise
export MISE_CACHE_DIR=/opt/mise/cache
export MISE_YES=1
export MISE_TRUSTED_CONFIG_PATHS=/workspace:/home/dev/.config/mise
case ":${PATH}:" in
*":/opt/mise/shims:"*) ;;
*) export PATH="/home/dev/.local/bin:/home/dev/.opencode/bin:/opt/mise/shims:${PATH}" ;;
esac
