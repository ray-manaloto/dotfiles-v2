# shellcheck shell=sh
# POSIX sh helper; invoked as `sh tasks/py/host_check.sh <cmd...>` (not executable, so not a mise task).
# Run a live-host doctor check, or report SKIP when the pre-push hook set
# DOTFILES_DOCTOR_SCOPE=repo so pushes depend only on the branch under review.
if [ "${DOTFILES_DOCTOR_SCOPE:-full}" = "repo" ]; then
	echo "SKIP: host check (DOTFILES_DOCTOR_SCOPE=repo)"
	exit 0
fi
exec "$@"
