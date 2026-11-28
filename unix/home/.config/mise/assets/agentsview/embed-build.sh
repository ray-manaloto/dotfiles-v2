#!/usr/bin/env bash
# Runs on pc, shipped over ssh by `mise run hub:embed`: pause, resume or report a long AgentsView
# embedding build that runs as two transient user units (a scratch `agentsview serve` daemon, which
# does the embedding, and `index embeddings build`, which drives it over HTTP).
#
# Pausing keeps the work: the daemon saves each document's vectors to vectors.db as it finishes, and a
# plain (incremental) build fills only what the generation is missing. See docs/agentsview-hub.md.
# Usage: embed-build.sh status|pause|resume
# Env: EMBED_DATA_DIR (required), EMBED_RUNTIME_MAX, EMBED_PORT; EMBED_UNIT_PREFIX and EMBED_AV exist
# so a stub agentsview can rehearse pause/resume on throwaway units without touching the real build.
set -euo pipefail

cmd="${1:-status}"
data_dir="${EMBED_DATA_DIR:?EMBED_DATA_DIR is required}"
runtime_max="${EMBED_RUNTIME_MAX:-144h}"
port="${EMBED_PORT:-8099}"
unit_prefix="${EMBED_UNIT_PREFIX:-av-embed}"
build_unit="${unit_prefix}-build.service"
daemon_unit="${unit_prefix}-daemon.service"
log="${data_dir}/build-progress.log"
state="${data_dir}/embed-pause.json"
snapshot_dir="${data_dir}/paused-snapshot"
av="${EMBED_AV:-${HOME}/.local/bin/agentsview}"
py=/usr/bin/python3
av_env=(AGENTSVIEW_DATA_DIR="${data_dir}" AGENTSVIEW_DISABLE_UPDATE_CHECK=1 AGENTSVIEW_TELEMETRY_ENABLED=0)

die() {
	echo "hub:embed: $*" >&2
	exit 1
}
active() { systemctl --user is-active --quiet "$1"; }
mark() { printf '%s %s\n' "$(date +%s)" "$*" >>"${log}"; }
# A refused resume puts both units back down and keeps the pause state, so a later resume can retry.
back_to_paused() {
	# One unit per call: a transient unit that never started is "not loaded", which fails the whole call.
	local unit
	for unit in "${build_unit}" "${daemon_unit}"; do
		systemctl --user stop "${unit}" 2>/dev/null || true
		systemctl --user reset-failed "${unit}" 2>/dev/null || true
	done
	mark "EMBED-PAUSED resume refused: $*"
	die "$*"
}
generations() { env "${av_env[@]}" "${av}" index embeddings list --json </dev/null; }

[[ -d ${data_dir} ]] || die "data dir not found: ${data_dir}"
[[ -x ${av} ]] || die "agentsview wrapper not found: ${av}"

# Progress of the current run: the lines after the last EMBED-RESUMED marker (or the whole log).
progress() {
	"${py}" -I - "${log}" <<'PY'
import re, sys, time
lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines() if len(sys.argv) > 1 else []
start = max((i for i, l in enumerate(lines) if " EMBED-RESUMED" in l), default=-1) + 1
seg = lines[start:]
pat = re.compile(r"(\d+) progress: (\d+)/(\d+) chunks")
pts = [tuple(map(int, m.groups())) for m in map(pat.match, seg) if m]
paused = next((l for l in reversed(lines) if " EMBED-PAUSED" in l or " EMBED-RESUMED" in l), "")
exits = [l for l in seg if " BUILD-EXIT " in l]
if not pts:
	print("no progress lines in this run yet")
else:
	(t0, n0, _), (t1, n1, tot) = pts[0], pts[-1]
	rate = (n1 - n0) / (t1 - t0) if t1 > t0 else 0.0
	eta = f"{(tot - n1) / rate / 3600:.1f}h" if rate else "n/a"
	print(f"{n1}/{tot} chunks ({100 * n1 / tot:.1f}%) this run, {rate:.2f} chunks/s over "
		f"{(t1 - t0) / 3600:.1f}h, ETA {eta}, last update {(time.time() - t1) / 60:.0f}m ago")
if exits:
	print(f"run ended: {exits[-1].split(' ', 1)[1]}")
if " EMBED-PAUSED" in paused:
	print(f"PAUSED since {time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(int(paused.split()[0])))}")
PY
}

status() {
	echo "units: build=$(systemctl --user is-active "${build_unit}" || true) daemon=$(systemctl --user is-active "${daemon_unit}" || true)"
	[[ -f ${log} ]] && progress
	[[ -f ${state} ]] && echo "pause state: ${state}"
	if command -v nvidia-smi >/dev/null 2>&1; then
		echo "gpu: $(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader)"
	fi
}

pause() {
	if [[ -f ${state} ]] && ! active "${build_unit}" && ! active "${daemon_unit}"; then
		echo "hub:embed: already paused (${state})"
		return 0
	fi
	active "${build_unit}" || die "${build_unit} is not running; nothing to pause"
	active "${daemon_unit}" || die "${daemon_unit} is not running, but the build is; refusing to guess"
	# The unit names are global, so make sure the running build is the one for this data dir.
	local unit
	for unit in "${build_unit}" "${daemon_unit}"; do
		systemctl --user show -p Environment --value "${unit}" | tr ' ' '\n' |
			grep -qxF "AGENTSVIEW_DATA_DIR=${data_dir}" ||
			die "${unit} is running against another data dir, not ${data_dir}"
	done
	local version gens
	version="$("${av}" --version)"
	gens="$(generations)" || die "could not list embedding generations"
	"${py}" -I - "${state}" "${version}" "${gens}" "${log}" <<'PY'
import json, re, sys, time
path, version, gens, log = sys.argv[1:5]
last = None
for line in open(log, encoding="utf-8", errors="replace"):
	m = re.match(r"(\d+) progress: (\d+)/(\d+) chunks", line)
	if m:
		last = tuple(map(int, m.groups()))
state = {"paused_at": int(time.time()), "version": version, "generations": json.loads(gens)["generations"],
			"last_progress": {"ts": last[0], "done": last[1], "total": last[2]} if last else None}
with open(path, "w", encoding="utf-8") as f:
	json.dump(state, f, indent=2)
PY
	echo "hub:embed: recorded ${state}"
	# Stop the driver first, then the daemon that does the embedding; the daemon finishes its
	# current save and releases vectors.write.lock on a clean stop.
	systemctl --user stop "${build_unit}"
	systemctl --user stop "${daemon_unit}"
	local u
	for u in "${build_unit}" "${daemon_unit}"; do systemctl --user reset-failed "${u}" 2>/dev/null || true; done
	if fuser -s "${data_dir}/vectors.db" 2>/dev/null; then
		die "a process still holds ${data_dir}/vectors.db after stopping both units"
	fi
	# A reflink copy on btrfs is instant and shares blocks; elsewhere it falls back to a full copy.
	mkdir -p "${snapshot_dir}"
	local f
	for f in vectors.db vectors.db-wal vectors.db-shm; do
		[[ -f ${data_dir}/${f} ]] && cp --reflink=auto -f "${data_dir}/${f}" "${snapshot_dir}/${f}"
	done
	mark "EMBED-PAUSED $("${py}" -I -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["last_progress"]))' <"${state}")"
	echo "hub:embed: paused; vectors.db snapshot in ${snapshot_dir}"
	status
}

resume() {
	[[ -f ${state} ]] || die "no pause state at ${state}; pause with hub:embed first"
	active "${build_unit}" && die "${build_unit} is already running"
	active "${daemon_unit}" && die "${daemon_unit} is already running"
	local version
	version="$("${av}" --version)"
	"${py}" -I - "${state}" "${version}" <<'PY' || exit 1
import json, sys
s = json.load(open(sys.argv[1], encoding="utf-8"))
if s["version"] != sys.argv[2]:
	sys.exit(f"hub:embed: agentsview changed since the pause ({s['version']!r} -> {sys.argv[2]!r}); "
				"a new release can reset vectors.db. Read its release notes, then resume by hand.")
PY
	local u
	for u in "${build_unit}" "${daemon_unit}"; do systemctl --user reset-failed "${u}" 2>/dev/null || true; done
	local setenv=()
	local kv
	for kv in "${av_env[@]}"; do setenv+=("--setenv=${kv}"); done
	systemd-run --user --quiet --unit="${daemon_unit}" -p RuntimeMaxSec="${runtime_max}" -p Nice=5 \
		"${setenv[@]}" "${av}" serve --host 127.0.0.1 --port "${port}" --no-browser --no-update-check
	local gens=""
	for _ in $(seq 1 60); do
		gens="$(generations 2>/dev/null)" && break
		gens=""
		sleep 2
	done
	[[ -n ${gens} ]] || back_to_paused "daemon did not answer within 120s; check: journalctl --user -u ${daemon_unit}"
	# The generation that was building must still be there with the same fingerprint.
	"${py}" -I - "${state}" "${gens}" <<'PY' || {
import json, sys
before = {(g["id"], g["fingerprint"]) for g in json.load(open(sys.argv[1], encoding="utf-8"))["generations"]
			if g["state"] in ("building", "active")}
now = json.loads(sys.argv[2])["generations"]
if not before <= {(g["id"], g["fingerprint"]) for g in now}:
	sys.exit(f"hub:embed: generations changed since the pause: before={sorted(before)} now={now}")
for g in now:
	print(f"generation {g['id']} {g['state']}: embedded={g['embedded']} missing={g['missing']}")
PY
		back_to_paused "the building generation changed since the pause"
	}
	mark "EMBED-RESUMED"
	# No --yes: an incremental build never asks, so a build that wants to confirm a full rebuild
	# fails on /dev/null instead of re-embedding from zero.
	local inner
	# shellcheck disable=SC2016  # expanded by the unit's bash, not here
	printf -v inner '%q index embeddings build </dev/null 2>&1 | while IFS= read -r l; do printf %q "$(date +%%s)" "$l"; done >>%q; echo "$(date +%%s) BUILD-EXIT ${PIPESTATUS[0]}" >>%q' \
		"${av}" '%s %s\n' "${log}" "${log}"
	systemd-run --user --quiet --unit="${build_unit}" -p RuntimeMaxSec="${runtime_max}" \
		-p WorkingDirectory="${HOME}" "${setenv[@]}" /bin/bash -c "${inner}"
	echo "hub:embed: build restarted; waiting for its first progress line"
	# Acceptance: the new run's total must be about what was left, not the whole archive.
	local verdict
	for _ in $(seq 1 90); do
		verdict="$(
			"${py}" -I - "${state}" "${log}" <<'PY'
import json, re, sys
s = json.load(open(sys.argv[1], encoding="utf-8"))
lines = open(sys.argv[2], encoding="utf-8", errors="replace").read().splitlines()
seg = lines[max(i for i, l in enumerate(lines) if " EMBED-RESUMED" in l) + 1:]
for l in seg:
	if " BUILD-EXIT " in l:
		print(f"exit {l.split(' BUILD-EXIT ', 1)[1]}"); break
	m = re.match(r"\d+ progress: \d+/(\d+) chunks", l)
	if m:
		total, lp = int(m.group(1)), s["last_progress"]
		left = lp["total"] - lp["done"] if lp else None
		# New sessions synced during the pause add a little; a reset brings back the work already
		# done. Call it a restart when more than half of the done chunks reappear.
		ok = left is None or total <= left + lp["done"] // 2
		print(f"{'ok' if ok else 'restart'} {total} {left}"); break
PY
		)"
		[[ -n ${verdict} ]] && break
		sleep 10
	done
	case "${verdict}" in
	ok\ *)
		read -r _ total left <<<"${verdict}"
		mv -f "${state}" "${data_dir}/embed-pause.last.json"
		echo "hub:embed: resumed: this run has ${total} chunks to embed (${left} were left at the pause)"
		;;
	restart\ *)
		read -r _ total left <<<"${verdict}"
		back_to_paused "resumed build wants ${total} chunks but only ${left} were left; stopped it. The snapshot is in ${snapshot_dir}"
		;;
	exit\ *) back_to_paused "the build exited before reporting progress (${verdict}); see ${log}" ;;
	*) back_to_paused "no progress line within 15 minutes; check ${log} and journalctl --user -u ${daemon_unit}" ;;
	esac
	status
}

case "${cmd}" in
status) status ;;
pause) pause ;;
resume) resume ;;
*) die "unknown command: ${cmd} (status|pause|resume)" ;;
esac
