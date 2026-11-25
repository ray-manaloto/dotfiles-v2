#!/usr/bin/env bash
# Claude Code statusline — reads ONLY the JSON payload on stdin, plus one git call.
# No plugin, no network, no credentials, no cache, no background processes.
#
# Schema: https://code.claude.com/docs/en/statusline ("Full JSON schema").
# Every field below is documented there; most are conditional, and each segment
# is dropped when its data is absent rather than rendered empty.
#
#   [vim] owner/repo/sub ⎇ branch* ↑1↓2 +1dir ⌂wt │ PR#12 ✓ │ Opus 5 · medium · think
#   │ ▓▓░░ 13% │ 5h 3%↻4h · wk 2%↻3d │ style X │ ⏱1h04m (api 15m) │ ↑1.2M ↓45k │ $0.42 +120/-8

set -uo pipefail

payload=$(cat)

# Prefer a REAL git binary over a mise/asdf shim: the shim costs ~65ms per call
# (81ms vs 16ms measured here), which on a per-render script is the whole budget.
GIT=git
for _c in /opt/homebrew/bin/git /usr/bin/git; do
	[[ -x $_c ]] && {
		GIT="$_c"
		break
	}
done

# ONE jq call for everything — it runs on every render.
#
# Delimiter is US (0x1f), NOT tab: tab counts as IFS *whitespace*, so bash
# collapses runs of it and drops empty fields, silently shifting every later
# value one slot left. Most of these fields are conditional and therefore
# frequently empty, so that failure mode would be constant.
# Positional slots (win_size, sess_name, wt_branch) are read but intentionally unused.
# shellcheck disable=SC2034
IFS=$'\037' read -r owner repo cur_dir proj_dir repo_host added_dirs git_wt \
	used_pct remain_pct win_size tok_in tok_out exceeds \
	cost lines_add lines_rm dur_ms api_ms \
	model_name effort thinking fast_mode \
	style cc_version sess_id sess_name \
	rl5 rl5r rl7 rl7r \
	pr_num pr_state wt_name wt_branch vim_mode agent_name <<EOF
$(printf '%s' "$payload" | jq -j '
	[ (.workspace.repo.owner // "")
	, (.workspace.repo.name // "")
	, (.workspace.current_dir // .cwd // "")
	, (.workspace.project_dir // "")
	, (.workspace.repo.host // "")
	, ((.workspace.added_dirs // []) | length)
	, (.workspace.git_worktree // "")
	, (.context_window.used_percentage // "")
	, (.context_window.remaining_percentage // "")
	, (.context_window.context_window_size // "")
	, (.context_window.total_input_tokens // 0)
	, (.context_window.total_output_tokens // 0)
	, (if .exceeds_200k_tokens then "1" else "" end)
	, (.cost.total_cost_usd // 0)
	, (.cost.total_lines_added // 0)
	, (.cost.total_lines_removed // 0)
	, (.cost.total_duration_ms // 0)
	, (.cost.total_api_duration_ms // 0)
	, (.model.display_name // "")
	, (.effort.level // "")
	, (if .thinking.enabled then "1" else "" end)
	, (if .fast_mode then "1" else "" end)
	, (.output_style.name // "")
	, (.version // "")
	, (.session_id // "")
	, (.session_name // "")
	, (.rate_limits.five_hour.used_percentage // "")
	, (.rate_limits.five_hour.resets_at // "")
	, (.rate_limits.seven_day.used_percentage // "")
	, (.rate_limits.seven_day.resets_at // "")
	, (.pr.number // "")
	, (.pr.review_state // "")
	, (.worktree.name // "")
	, (.worktree.branch // "")
	, (.vim.mode // "")
	, (.agent.name // "")
	] | map(tostring) | join("\u001f")' 2>/dev/null)
EOF

# --- colors (256-colour, readable on light and dark backgrounds) ---
C_REPO=$'\033[38;5;39m'    # blue
C_BRANCH=$'\033[38;5;179m' # amber
C_DIRTY=$'\033[38;5;203m'  # red
C_MODEL=$'\033[38;5;140m'  # violet
C_DIM=$'\033[38;5;245m'    # grey
C_OK=$'\033[38;5;71m'      # green
C_WARN=$'\033[38;5;179m'   # amber
C_HOT=$'\033[38;5;203m'    # red
C_R=$'\033[0m'

# Plain string accumulation, not an array: bash 3.2 (what macOS ships) errors on
# ${arr[@]} for an empty array under `set -u`, which is exactly the empty-input case.
out=""
add() {
	[[ -z $1 ]] && return
	[[ -n $out ]] && out="${out}${C_DIM} │ ${C_R}"
	out="${out}$1"
}

# Formatters use only builtins (printf is a bash builtin) so they cost no fork.
hum() { # 1234567 -> 1.2M ; 45000 -> 45k
	local n=${1:-0}
	[[ $n =~ ^[0-9]+$ ]] || {
		printf '0'
		return
	}
	if ((n >= 1000000)); then
		printf '%d.%dM' $((n / 1000000)) $(((n % 1000000) / 100000))
	elif ((n >= 1000)); then
		printf '%dk' $((n / 1000))
	else printf '%d' "$n"; fi
}
dur() { # milliseconds -> 1h04m / 12m / 45s
	local s=$((${1:-0} / 1000))
	if ((s >= 3600)); then
		printf '%dh%02dm' $((s / 3600)) $(((s % 3600) / 60))
	elif ((s >= 60)); then
		printf '%dm' $((s / 60))
	else printf '%ds' "$s"; fi
}
pct_color() {
	local p=${1:-0}
	if ((p >= 80)); then
		printf '%s' "$C_HOT"
	elif ((p >= 50)); then
		printf '%s' "$C_WARN"
	else printf '%s' "$C_OK"; fi
}

# --- 1. vim mode, when enabled — it changes what typing does, so it leads ---
[[ -n ${vim_mode:-} ]] && add "${C_BRANCH}${vim_mode}${C_R}"

# --- 2. location: repo, sub-path, branch, divergence, extra dirs, worktree ---
loc=""
if [[ -n ${owner:-} && -n ${repo:-} ]]; then
	# Host prefix only for non-GitHub remotes — otherwise noise on every line.
	[[ -n ${repo_host:-} && $repo_host != "github.com" ]] && loc="${C_DIM}${repo_host}:${C_R}"
	loc="${loc}${C_DIM}${owner}/${C_R}${C_REPO}${repo}${C_R}"
elif [[ -n ${cur_dir:-} ]]; then
	loc="${C_REPO}${cur_dir##*/}${C_R}" # ${##*/} not basename: saves a fork
fi

# Sub-path within the project, so "which package am I in" is visible in a monorepo.
if [[ -n ${proj_dir:-} && -n ${cur_dir:-} && $cur_dir != "$proj_dir" && $cur_dir == "$proj_dir"/* ]]; then
	loc="${loc}${C_DIM}/${cur_dir#"$proj_dir"/}${C_R}"
fi

# Branch, dirty marker and upstream divergence — ONE git call.
# porcelain=v2 --branch yields the branch, divergence, and (via any non-'#' line)
# whether the tree is dirty. Untracked files count: this repo has 29 untracked vs
# 1 tracked change, so -uno would wrongly call it clean.
if [[ -n ${cur_dir:-} && -d $cur_dir ]]; then
	if st=$("$GIT" -C "$cur_dir" status --porcelain=v2 --branch 2>/dev/null); then
		branch=""
		ahead=""
		behind=""
		dirty=""
		while IFS= read -r line; do
			case "$line" in
			'# branch.head '*) branch=${line#'# branch.head '} ;;
			'# branch.oid '*) oid=${line#'# branch.oid '} ;;
			'# branch.ab '*)
				ab=${line#'# branch.ab '}
				a=${ab%% *}
				b=${ab##* }
				[[ $a != "+0" ]] && ahead=${a#+}
				[[ $b != "-0" ]] && behind=${b#-}
				;;
			'#'*) ;;
			*) dirty=1 ;;
			esac
		done <<<"$st"
		# Detached HEAD reports "(detached)" — show the short sha instead.
		[[ $branch == "(detached)" && -n ${oid:-} ]] && branch="${oid:0:7}"
		if [[ -n $branch ]]; then
			loc="${loc} ${C_DIM}⎇${C_R} ${C_BRANCH}${branch}${C_R}"
			[[ -n $dirty ]] && loc="${loc}${C_DIRTY}*${C_R}"
			# One leading space covering the pair, so behind-only renders "main ↓1".
			if [[ -n $ahead || -n $behind ]]; then
				loc="${loc} "
				[[ -n $ahead ]] && loc="${loc}${C_DIM}↑${ahead}${C_R}"
				[[ -n $behind ]] && loc="${loc}${C_DIM}↓${behind}${C_R}"
			fi
		fi
	fi
fi

# --add-dir directories are invisible in Claude Code's own UI, and they change
# what the session can reach — so surface the count.
[[ ${added_dirs:-0} =~ ^[0-9]+$ ]] && ((added_dirs > 0)) &&
	loc="${loc} ${C_DIM}+${added_dirs}dir${C_R}"

# Worktree: `worktree.name` during --worktree sessions, `workspace.git_worktree`
# when merely sitting inside a linked worktree. Either means "not the main tree".
_wt="${wt_name:-}"
[[ -z $_wt ]] && _wt="${git_wt:-}"
[[ -n $_wt ]] && loc="${loc} ${C_DIM}⌂${_wt}${C_R}"
add "$loc"

# --- 3. session name: NOT rendered. Claude Code already prints it natively in
# the divider above the prompt (right-aligned), so showing it here duplicated it
# on screen. Still extracted above in case it is ever wanted. ---

# --- 4. open PR for this branch, with review state ---
if [[ -n ${pr_num:-} ]]; then
	case "${pr_state:-}" in
	approved)
		_pc="$C_OK"
		_ps="✓"
		;;
	changes_requested)
		_pc="$C_HOT"
		_ps="✗"
		;;
	pending)
		_pc="$C_WARN"
		_ps="•"
		;;
	draft)
		_pc="$C_DIM"
		_ps="◌"
		;;
	*)
		_pc="$C_DIM"
		_ps=""
		;;
	esac
	add "${_pc}PR#${pr_num}${_ps:+ $_ps}${C_R}"
fi

# --- 5. model, effort, thinking, fast mode ---
# Claude Code prints these in its session HEADER, but the header scrolls out of
# view after the first few exchanges, so they are not persistent there.
# Permission mode and agent COUNT stay pinned in the native footer, so those are
# deliberately still omitted.
if [[ -n ${model_name:-} ]]; then
	m="${C_MODEL}${model_name%% (*}${C_R}" # "Opus 5 (1M context)" -> "Opus 5"
	[[ -n ${effort:-} ]] && m="${m} ${C_DIM}· ${effort}${C_R}"
	[[ -n ${thinking:-} ]] && m="${m} ${C_DIM}· think${C_R}"
	[[ -n ${fast_mode:-} ]] && m="${m} ${C_DIM}· fast${C_R}"
	add "$m"
fi

# --- 6. context usage bar ---
# used_percentage is authoritative; remaining_percentage is the documented
# complement and the only sane fallback. current_usage is NOT usable here — the
# schema defines it as an OBJECT of token counts, not a scalar.
pct=""
if [[ -n ${used_pct:-} ]]; then
	pct=${used_pct%.*}
elif [[ -n ${remain_pct:-} && ${remain_pct%.*} =~ ^[0-9]+$ ]]; then
	pct=$((100 - ${remain_pct%.*}))
fi

if [[ $pct =~ ^[0-9]+$ ]]; then
	((pct > 100)) && pct=100
	((pct < 0)) && pct=0
	filled=$(((pct + 12) / 13))
	((filled > 8)) && filled=8
	bar=""
	for ((i = 0; i < 8; i++)); do ((i < filled)) && bar+="▓" || bar+="░"; done
	c=$(pct_color "$pct")
	_ctx="${c}${bar}${C_R} ${c}${pct}%${C_R}"
	[[ -n ${exceeds:-} ]] && _ctx="${_ctx} ${C_HOT}⚠200k${C_R}"
	add "$_ctx"
fi

# --- 7. subscription rate limits (payload-provided; absent until the first API
# response of the session, and only for Claude.ai subscribers) ---
_now=""      # populated lazily; only the reset countdown needs the clock
reset_in() { # unix epoch -> "4h" / "3d", empty if unknown or already past
	local e=${1:-}
	[[ $e =~ ^[0-9]+$ ]] || return
	[[ -z $_now ]] && _now=$(date +%s)
	local d=$((e - _now))
	((d <= 0)) && return
	if ((d >= 172800)); then
		printf '%dd' $((d / 86400))
	elif ((d >= 3600)); then
		printf '%dh' $((d / 3600))
	else printf '%dm' $((d / 60)); fi
}

limits_seg=""
for _tuple in "5h|${rl5}|${rl5r}" "wk|${rl7}|${rl7r}"; do
	IFS='|' read -r _label _val _reset <<<"$_tuple"
	[[ -z $_val ]] && continue
	_int=${_val%.*}
	[[ $_int =~ ^[0-9]+$ ]] || continue
	((_int > 100)) && _int=100
	[[ -n $limits_seg ]] && limits_seg="${limits_seg}${C_DIM} · ${C_R}"
	limits_seg="${limits_seg}${C_DIM}${_label}${C_R} $(pct_color "$_int")${_int}%${C_R}"
	# Reset countdown only once a window is filling — keeps the line quiet at 3%.
	if ((_int >= 50)); then
		_r=$(reset_in "$_reset")
		[[ -n $_r ]] && limits_seg="${limits_seg}${C_DIM}↻${_r}${C_R}"
	fi
done
add "$limits_seg"

# --- 8. non-default output style — it silently changes how Claude responds ---
[[ -n ${style:-} && $style != "default" ]] && add "${C_DIM}style${C_R} ${C_BRANCH}${style}${C_R}"

# --- 9. named agent (--agent / agent setting) ---
[[ -n ${agent_name:-} ]] && add "${C_DIM}agent${C_R} ${C_MODEL}${agent_name}${C_R}"

# --- 10. session wall time, and how much was spent waiting on the API ---
if [[ ${dur_ms:-0} =~ ^[0-9]+$ ]] && ((dur_ms >= 60000)); then
	_d="${C_DIM}⏱$(dur "$dur_ms")${C_R}"
	[[ ${api_ms:-0} =~ ^[0-9]+$ ]] && ((api_ms > 0)) &&
		_d="${_d}${C_DIM} (api $(dur "$api_ms"))${C_R}"
	add "$_d"
fi

# --- 11. cumulative session tokens (distinct from the context bar, which is
# only what is resident right now) ---
if [[ ${tok_in:-0} =~ ^[0-9]+$ && ${tok_out:-0} =~ ^[0-9]+$ ]] &&
	((tok_in + tok_out > 0)); then
	add "${C_DIM}↑$(hum "$tok_in")${C_R} ${C_DIM}↓$(hum "$tok_out")${C_R}"
fi

# --- 12. CLI version and session id ---
# transcript_path is deliberately NOT shown: its filename IS session_id, so it
# was the same identifier twice and the widest thing on the line.
[[ -n ${cc_version:-} ]] && add "${C_DIM}v${cc_version}${C_R}"
[[ -n ${sess_id:-} ]] && add "${C_DIM}sid ${sess_id:0:8}${C_R}"

# --- 13. cost and lines changed (both hidden while still zero) ---
tail_seg=""
if [[ -n ${cost:-} ]] && awk -v c="$cost" 'BEGIN{exit !(c+0 >= 0.01)}' 2>/dev/null; then
	tail_seg="${C_OK}\$$(printf '%.2f' "$cost" 2>/dev/null)${C_R}"
fi
if [[ ${lines_add:-0} != "0" || ${lines_rm:-0} != "0" ]]; then
	[[ -n $tail_seg ]] && tail_seg="${tail_seg} "
	tail_seg="${tail_seg}${C_OK}+${lines_add}${C_R}${C_DIM}/${C_R}${C_HOT}-${lines_rm}${C_R}"
fi
add "$tail_seg"

printf '%s\n' "$out"
