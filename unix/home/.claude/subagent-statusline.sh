#!/usr/bin/env bash
# Claude Code subagent status line — one custom row per agent in the agent panel.
# Reads ONLY the JSON on stdin. No network, no credentials, no state.
#
# Schema: https://code.claude.com/docs/en/statusline ("Subagent status lines").
# stdin:  { columns, cwd, session_id, prompt_id, transcript_path, tasks: [...] }
# stdout: one JSON line per row to override — {"id": "<task id>", "content": "<body>"}
#         Omitting a task keeps Claude Code's default row; "" hides the row.
#
# Default row is `name · description · token count`. This replaces it with:
#
#   ▸ Find README files · Probing candidate paths · dotfiles · Opus · ▓░░░ 5% · 1m12s
#     └ static task       └ LIVE activity          └ repo     └ model  └ ctx    └ age
#
# `label` is the field that actually answers "what is it doing NOW" — it changes
# as the agent works, while `description` stays fixed. `cwd` is what tells apart
# agents running in different repos, which the default row never shows.
#
# All formatting happens inside the single jq pass: one subprocess per tick.

set -uo pipefail

jq -c '
	# ---- helpers -------------------------------------------------------------
	def esc: "\u001b";
	def dim:   esc + "[38;5;245m";
	def blue:  esc + "[38;5;39m";
	def viol:  esc + "[38;5;140m";
	def green: esc + "[38;5;71m";
	def amber: esc + "[38;5;179m";
	def red:   esc + "[38;5;203m";
	def rst:   esc + "[0m";

	def elapsed:                                   # seconds -> 1h04m / 12m / 45s
			(. | floor) as $s
		| if   $s >= 3600 then "\($s/3600|floor)h\((($s%3600)/60)|floor)m"
			elif $s >= 60   then "\(($s/60)|floor)m"
			else "\($s)s" end;

	def shortmodel:                                # claude-opus-5[1m] -> Opus
			ascii_downcase
		| if   test("opus")   then "Opus"
			elif test("sonnet") then "Sonnet"
			elif test("haiku")  then "Haiku"
			elif test("fable")  then "Fable"
			else "?" end;

	def pctcolor: if . >= 80 then red elif . >= 50 then amber else green end;

	def bar($p):                                   # 4-cell context bar
			([$p, 100] | min) as $c
		| (($c + 24) / 25 | floor) as $f
		| ([range(0;4) | if . < $f then "▓" else "░" end] | join(""));

	# Status drives the leading glyph and its colour.
	def glyph:                                     # \uXXXX escapes: byte-safe
			if   . == "running"   then {g:"\u25b8", c:"[38;5;39m"}
			elif . == "completed" then {g:"\u2713", c:"[38;5;71m"}
			elif . == "failed" or . == "error"   then {g:"\u2717", c:"[38;5;203m"}
			elif . == "queued" or . == "pending" then {g:"\u25cc", c:"[38;5;245m"}
			else {g:"\u00b7", c:"[38;5;245m"} end;

	# ---- rows ----------------------------------------------------------------
	(.columns // 120) as $cols
	| .tasks // []
	| .[]
	| . as $t
	| ($t.status // "" | glyph) as $gl
	| ($t.description // $t.name // $t.label // "task") as $desc
	| ($t.label // "") as $live
	| (($t.cwd // "") | split("/") | last // "") as $repo
	| (($t.model // "") | shortmodel) as $model
	| (if ($t.contextWindowSize // 0) > 0
				then (($t.tokenCount // 0) / $t.contextWindowSize * 100 | floor)
				else null end) as $pct
	| (if ($t.startTime // 0) > 0
				then ((now - ($t.startTime / 1000)) | elapsed)
				else "" end) as $age
	| ($t.effort // "") as $eff

	# Build the trailing (fixed-width) pieces first so the variable-width label
	# can be given exactly the space that is left over.
	| ([ (if $repo  != "" then $repo else empty end)
			, (if $model != "?" and $model != "" then $model + (if $eff != "" then "·" + $eff else "" end) else empty end)
			, (if $pct   != null then "\($pct)%" else empty end)
			, (if $age   != "" then $age else empty end)
			] | join(" · ")) as $tailplain

	# 2 for the glyph+space, 4 for the context bar, 3 per " · " separator.
	| (2 + ($desc | length) + 3 + ($tailplain | length) + (if $pct != null then 5 else 0 end)) as $used
	| (($cols - $used - 3) as $room | if $room < 8 then 0 else $room end) as $room
	| (if $live == "" or $live == $desc or $room == 0 then ""
			else (if ($live | length) > $room then ($live[0:$room-1] + "…") else $live end)
			end) as $liveshown

	| { id: $t.id
		, content:
				((esc + $gl.c) + $gl.g + rst + " "
					+ blue + $desc + rst
					+ (if $liveshown != "" then dim + " · " + $liveshown + rst else "" end)
					+ (if $repo != "" then dim + " · " + rst + viol + $repo + rst else "" end)
					+ (if $model != "?" and $model != "" then dim + " · " + $model + (if $eff != "" then "·" + $eff else "" end) + rst else "" end)
					+ (if $pct != null then dim + " · " + rst + ($pct|pctcolor) + bar($pct) + " \($pct)%" + rst else "" end)
					+ (if $age != "" then dim + " · " + $age + rst else "" end))
		}
' 2>/dev/null
