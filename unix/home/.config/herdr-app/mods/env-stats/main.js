import { View, div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import {
  checking,
  exec,
  home,
  machines,
  props,
  readTextFile,
  run,
  spaces,
  terminals,
  theme,
  watchFolder,
  window,
} from "herdr";

const TARGETS = [
  { id: "host", label: "Mac Host", arch: "darwin/arm64", tool: "btm --basic" },
  { id: "pc-host", label: "PC Host", arch: "linux/x86_64", tool: "/usr/bin/btop" },
  { id: "pc", label: "PC Container", arch: "linux/amd64", tool: "btm --basic" },
  { id: "mac", label: "Mac Container", arch: "linux/arm64", tool: "btm --basic" },
  { id: "vm", label: "Tart VM", arch: "darwin/arm64", tool: "btm --basic" },
];

const REFRESH_MS = 10000;
const AGY_SESSIONS_DIR = () => `${home()}/.cache/usage-telemetry/agy-sessions`;
const HERDR_LIMITS_DIR = () => `${home()}/.cache/herdr-usage-limits`;

function fmtTokens(n) {
  const num = Number(n) || 0;
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${Math.round(num / 1000)}k`;
  return String(num);
}

function barColor(pct, t) {
  if (pct == null) return t.dim;
  if (pct >= 90) return t.ansi1 ?? t.blocked;
  if (pct >= 70) return t.ansi3 ?? t.working;
  return t.ansi2 ?? t.done;
}

function vectorBar(percent, color, t, height = 6) {
  const bounded = percent == null ? 0 : Math.min(100, Math.max(0, Number(percent) || 0));
  return div()
    .h(height)
    .w_full()
    .rounded(3)
    .bg(t.border)
    .overflow_hidden()
    .when(percent != null, (track) =>
      track.child(div().h_full().w(`${bounded}%`).rounded(3).bg(color)),
    );
}

export default class EnvStats extends View {
  init(_props, cx) {
    this.hostStats = null;
    this.agySessions = [];
    this.agyQuota = null;
    this.paused = false;
    this.narrow = false;
    this.watches = [];
    this.refreshingHost = false;
    this.refreshingFiles = false;
    this.scroll = null;

    if (checking()) return;

    this.refreshHost(cx);
    this.refreshTelemetry(cx);
    this.startWatch(cx, AGY_SESSIONS_DIR());
    this.startWatch(cx, HERDR_LIMITS_DIR());

    cx.timer.every(REFRESH_MS, (timerCx) => {
      if (window()?.visible === false) {
        this.paused = true;
        return;
      }
      this.paused = false;
      this.refreshHost(timerCx);
      this.refreshTelemetry(timerCx);
    });
  }

  async close() {
    for (const w of this.watches) {
      try {
        w.cancel();
      } catch {
        // Ignore already settled watches on close.
      }
    }
    this.watches = [];
  }

  startWatch(cx, dir) {
    cx.spawn(async (spawnCx) => {
      for (;;) {
        const w = watchFolder(dir);
        this.watches.push(w);
        let paths = [];
        try {
          paths = await w;
        } catch {
          return;
        }
        if (!paths.length) return;
        if (window()?.visible !== false) {
          this.refreshTelemetry(spawnCx);
        } else {
          this.paused = true;
        }
      }
    });
  }

  refreshHost(cx) {
    if (this.refreshingHost || window()?.visible === false) return;
    this.refreshingHost = true;
    cx.spawn(async (spawnCx) => {
      try {
        const res = await exec(
          "/bin/sh",
          [
            "-c",
            [
              'l=$(sysctl -n vm.loadavg 2>/dev/null || uptime | sed "s/.*load average[s]*://")',
              "c=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 1)",
              "m=$(sysctl -n hw.memsize 2>/dev/null || echo 0)",
              'p=$(vm_stat 2>/dev/null | awk "/Pages active|Pages wired/{gsub(/[^0-9]/,\\\"\\\",\\$NF); s+=\\$NF} END{print s+0}" || echo 0)',
              "ps=$(pagesize 2>/dev/null || echo 4096)",
              'printf "%s\\t%s\\t%s\\t%s\\t%s\\n" "$l" "$c" "$m" "$p" "$ps"',
            ].join("; "),
          ],
          { timeout: 2500 },
        );
        if (res.code === 0) {
          const parts = res.stdout.trim().split("\t");
          const loadMatch = (parts[0] || "").match(/([0-9]+(?:\.[0-9]+)?)/);
          const load1 = loadMatch ? Number(loadMatch[1]) : null;
          const cpus = Math.max(1, Number(parts[1]) || 1);
          const totalBytes = Number(parts[2]) || 0;
          const activePages = Number(parts[3]) || 0;
          const pageSize = Number(parts[4]) || 4096;
          const usedBytes = activePages * pageSize;
          const memPct = totalBytes > 0 ? Math.min(100, (usedBytes * 100) / totalBytes) : null;
          const cpuPct = load1 != null ? Math.min(100, (load1 * 100) / cpus) : null;
          this.hostStats = { load1, cpus, memPct, cpuPct, usedBytes, totalBytes };
          spawnCx.notify();
        }
      } catch {
        // Keep previous host sample if transient probe fails.
      } finally {
        this.refreshingHost = false;
      }
    });
  }

  refreshTelemetry(cx) {
    if (this.refreshingFiles || window()?.visible === false) return;
    this.refreshingFiles = true;
    cx.spawn(async (spawnCx) => {
      try {
        const listRes = await exec(
          "/bin/sh",
          ["-c", `ls -1t "${AGY_SESSIONS_DIR()}"/*.json 2>/dev/null | head -n 12`],
          { timeout: 2000 },
        );
        const files =
          listRes.code === 0
            ? listRes.stdout
                .split("\n")
                .map((s) => s.trim())
                .filter(Boolean)
            : [];
        const sessions = [];
        for (const fpath of files) {
          const raw = await readTextFile(fpath).catch(() => null);
          if (!raw) continue;
          try {
            const parsed = JSON.parse(raw);
            if (parsed && typeof parsed === "object" && parsed.conversation_id) {
              sessions.push(parsed);
            }
          } catch {
            // Skip partial or malformed session JSON.
          }
        }
        this.agySessions = sessions;

        const qRaw = await readTextFile(`${HERDR_LIMITS_DIR()}/antigravity.json`).catch(() => null);
        if (qRaw) {
          try {
            this.agyQuota = JSON.parse(qRaw);
          } catch {
            // Keep existing quota snapshot on partial or malformed JSON.
          }
        } else {
          this.agyQuota = null;
        }
        spawnCx.notify();
      } catch {
        // Leave existing telemetry intact on transient FS error.
      } finally {
        this.refreshingFiles = false;
      }
    });
  }

  splitTop(targetId) {
    const pane = props()?.pane;
    const args = {
      direction: "right",
      argv: ["mise", "run", "env:top", targetId],
    };
    if (pane) args.pane = pane;
    run("pane.split", args);
  }

  openTopTab(targetId) {
    run("tab.new", {
      name: `top:${targetId}`,
      argv: ["mise", "run", "env:top", targetId, "--loop"],
    });
  }

  targetStatus(targetId) {
    if (targetId === "host") return ["ready", "Local"];
    const machineMap = new Map(machines().map((m) => [m.name, m.layout]));
    if (targetId === "pc-host") {
      const st = machineMap.get("pc");
      return st === "ready" ? ["ready", "SSH / btop"] : ["unknown", "SSH"];
    }
    const tsName = `dotfiles-v2-${targetId}`;
    const st = machineMap.get(tsName) ?? machineMap.get(targetId);
    if (st === "ready") return ["ready", "Tailscale"];
    if (st === "unavailable") return ["unavailable", "Offline"];
    return ["unknown", "On-demand"];
  }

  envCard(target, t) {
    const [state, badge] = this.targetStatus(target.id);
    const isHost = target.id === "host";
    const cpuPct = isHost ? (this.hostStats?.cpuPct ?? null) : null;
    const memPct = isHost ? (this.hostStats?.memPct ?? null) : null;
    const tone =
      state === "ready"
        ? (t.ansi2 ?? t.working)
        : state === "unavailable"
          ? (t.ansi1 ?? t.blocked)
          : t.dim;

    return v_flex()
      .id(`env-card-${target.id}`)
      .flex_1()
      .min_w(210)
      .gap(8)
      .p(12)
      .rounded(10)
      .bg(t.surface)
      .border(1)
      .border_color(t.border)
      .child(
        h_flex()
          .items_center()
          .justify_between()
          .gap(8)
          .child(
            v_flex()
              .gap(2)
              .child(
                div().text_size(13).font_semibold().text_color(t.foreground).child(target.label),
              )
              .child(
                div().text_size(11).text_color(t.muted).child(`${target.arch} · ${target.tool}`),
              ),
          )
          .child(
            div().px(6).py(2).rounded(4).bg(t.raised).text_size(10).text_color(tone).child(badge),
          ),
      )
      .when(isHost && this.hostStats != null, (card) =>
        card
          .child(
            v_flex()
              .gap(3)
              .child(
                h_flex()
                  .justify_between()
                  .text_size(11)
                  .text_color(t.muted)
                  .child(
                    `CPU load ${this.hostStats.load1?.toFixed(2) ?? "—"} / ${this.hostStats.cpus}c`,
                  )
                  .child(cpuPct != null ? `${Math.round(cpuPct)}%` : "—"),
              )
              .child(vectorBar(cpuPct, barColor(cpuPct, t), t)),
          )
          .child(
            v_flex()
              .gap(3)
              .child(
                h_flex()
                  .justify_between()
                  .text_size(11)
                  .text_color(t.muted)
                  .child("Memory active+wired")
                  .child(memPct != null ? `${Math.round(memPct)}%` : "—"),
              )
              .child(vectorBar(memPct, barColor(memPct, t), t)),
          ),
      )
      .child(
        h_flex()
          .gap(6)
          .pt(2)
          .child(
            div()
              .id(`split-top-${target.id}`)
              .px(8)
              .py(4)
              .rounded(6)
              .bg(t.raised)
              .border(1)
              .border_color(t.border)
              .cursor_pointer()
              .text_size(11)
              .text_color(t.foreground)
              .hover((s) => s.bg(t.hover))
              .on_click(() => this.splitTop(target.id))
              .child("Split Top"),
          )
          .child(
            div()
              .id(`tab-top-${target.id}`)
              .px(8)
              .py(4)
              .rounded(6)
              .border(1)
              .border_color(t.border)
              .cursor_pointer()
              .text_size(11)
              .text_color(t.muted)
              .hover((s) => s.bg(t.hover).text_color(t.foreground))
              .on_click(() => this.openTopTab(target.id))
              .child("New Tab"),
          ),
      );
  }

  sessionRow(sess, t) {
    const pct = Number(sess.compaction_used_percentage ?? sess.used_percentage ?? 0);
    const headroom = fmtTokens(sess.compaction_headroom_tokens ?? 0);
    const limit = fmtTokens(sess.compaction_limit ?? 508000);
    const used = fmtTokens(sess.used_tokens ?? 0);
    const liveTerm = terminals().find(
      (term) =>
        Array.isArray(term.argv) &&
        term.argv.some((arg) => String(arg).includes(sess.conversation_id)),
    );
    const swarmStr =
      sess.swarm_total > 0 ? `swarm ${sess.swarm_working}w/${sess.swarm_idle}i` : null;

    return v_flex()
      .id(`agy-sess-${sess.conversation_id}`)
      .gap(4)
      .p(10)
      .rounded(8)
      .bg(t.surface)
      .border(1)
      .border_color(t.border)
      .when(Boolean(liveTerm), (row) =>
        row
          .cursor_pointer()
          .hover((s) => s.bg(t.hover))
          .on_click(() => run("pane.focus", { terminal: liveTerm.address })),
      )
      .child(
        h_flex()
          .justify_between()
          .gap(8)
          .child(
            h_flex()
              .gap(6)
              .items_center()
              .min_w(0)
              .child(
                div()
                  .text_size(12)
                  .font_semibold()
                  .text_color(t.foreground)
                  .truncate()
                  .child(sess.model_display_name || sess.model_id || "Antigravity"),
              )
              .when(Boolean(sess.plan_id), (el) =>
                el.child(
                  div()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(t.raised)
                    .text_size(10)
                    .text_color(t.accent)
                    .child(sess.plan_id),
                ),
              ),
          )
          .child(
            div()
              .text_size(11)
              .text_color(sess.agent_state === "working" ? (t.working ?? t.accent) : t.muted)
              .child(sess.agent_state || "idle"),
          ),
      )
      .child(vectorBar(pct, barColor(pct, t), t, 5))
      .child(
        h_flex()
          .justify_between()
          .text_size(11)
          .text_color(t.muted)
          .child(`${used} / ~${limit} (${Math.round(pct)}% · ~${headroom} left)`)
          .child([swarmStr, sess.conversation_id.slice(0, 8)].filter(Boolean).join(" · ")),
      );
  }

  quotaSummary(t) {
    const quota = this.agyQuota?.quota;
    if (!quota || typeof quota !== "object") {
      return div()
        .text_size(11)
        .text_color(t.dim)
        .child("No Antigravity quota snapshot cached yet");
    }
    const entries = Object.entries(quota).filter(
      ([, v]) => v && typeof v.remaining_fraction === "number",
    );
    return h_flex()
      .gap(10)
      .children(
        entries.map(([key, val]) => {
          const usedPct = Math.max(
            0,
            Math.min(100, Math.round((1 - val.remaining_fraction) * 100)),
          );
          return v_flex()
            .flex_1()
            .min_w(120)
            .gap(4)
            .p(8)
            .rounded(8)
            .bg(t.surface)
            .border(1)
            .border_color(t.border)
            .child(
              h_flex()
                .justify_between()
                .text_size(11)
                .child(div().text_color(t.foreground).font_semibold().child(key))
                .child(div().text_color(barColor(usedPct, t)).child(`${usedPct}% used`)),
            )
            .child(vectorBar(usedPct, barColor(usedPct, t), t, 4));
        }),
      );
  }

  render(cx) {
    const visible = window()?.visible !== false;
    if (this.paused && visible && !checking()) {
      this.paused = false;
      this.refreshHost(cx);
      this.refreshTelemetry(cx);
    }

    const t = theme();
    const rtSpace = spaces().find((s) => s.name === "realtime-stats");
    const envGrid = this.narrow
      ? v_flex()
          .gap(8)
          .children(TARGETS.map((tgt) => this.envCard(tgt, t)))
      : v_flex()
          .gap(8)
          .child(
            h_flex()
              .gap(8)
              .children(TARGETS.slice(0, 3).map((tgt) => this.envCard(tgt, t))),
          )
          .child(
            h_flex()
              .gap(8)
              .children(TARGETS.slice(3).map((tgt) => this.envCard(tgt, t))),
          );

    return v_flex()
      .id("env-stats-root")
      .size_full()
      .p(16)
      .gap(12)
      .bg(t.canvas)
      .on_size_change((size, sizeCx) => {
        const narrow = size.width < 760;
        if (narrow !== this.narrow) {
          this.narrow = narrow;
          sizeCx.notify();
        }
      })
      .child(
        h_flex()
          .items_center()
          .justify_between()
          .gap(12)
          .child(
            v_flex()
              .gap(2)
              .child(
                div()
                  .text_size(18)
                  .font_semibold()
                  .text_color(t.foreground)
                  .child("Environment & Harness Stats"),
              )
              .child(
                div()
                  .text_size(11)
                  .text_color(t.muted)
                  .child("Pure GPUI telemetry · 1-click split into mise run env:top <target>"),
              ),
          )
          .child(
            h_flex()
              .gap(8)
              .when(Boolean(rtSpace), (el) =>
                el.child(
                  div()
                    .id("env-stats-rt-space")
                    .px(10)
                    .py(5)
                    .rounded(6)
                    .border(1)
                    .border_color(t.border)
                    .cursor_pointer()
                    .text_size(11)
                    .text_color(t.foreground)
                    .hover((s) => s.bg(t.hover))
                    .on_click(() => run("space.select", { id: rtSpace.id }))
                    .child("realtime-stats"),
                ),
              )
              .child(
                div()
                  .id("env-stats-open-grid")
                  .px(10)
                  .py(5)
                  .rounded(6)
                  .bg(t.solid)
                  .text_size(11)
                  .text_color(t.solid_foreground)
                  .cursor_pointer()
                  .on_click(() =>
                    exec(
                      "/bin/sh",
                      [
                        "-c",
                        'if command -v mise >/dev/null 2>&1; then exec mise run herdr:realtime-stats --grid; elif [ -x "$HOME/.local/bin/mise" ]; then exec "$HOME/.local/bin/mise" run herdr:realtime-stats --grid; else exec /usr/local/bin/mise run herdr:realtime-stats --grid; fi',
                      ],
                      { timeout: 15000 },
                    ).catch(() => {}),
                  )
                  .child("5-Pane Top Grid"),
              ),
          ),
      )
      .child(
        v_flex()
          .id("env-stats-scroll")
          .flex_1()
          .min_h(0)
          .gap(12)
          .overflow_y_scrollbar()
          .child(envGrid)
          .child(
            v_flex()
              .gap(6)
              .child(
                div()
                  .text_size(13)
                  .font_semibold()
                  .text_color(t.foreground)
                  .child("Antigravity Quota Cache"),
              )
              .child(this.quotaSummary(t)),
          )
          .child(
            v_flex()
              .gap(6)
              .child(
                div()
                  .text_size(13)
                  .font_semibold()
                  .text_color(t.foreground)
                  .child(`Active AGY Sessions (${this.agySessions.length})`),
              )
              .children(
                this.agySessions.length
                  ? this.agySessions.map((sess) => this.sessionRow(sess, t))
                  : [
                      div()
                        .text_size(11)
                        .text_color(t.dim)
                        .child("No active agy-sessions/*.json snapshots yet"),
                    ],
              ),
          ),
      );
  }
}
