import { View, div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { checking, exec, home, readTextFile, theme, writeTextFile } from "herdr";

// Controls for the env:board API-credit balance lane. The board re-reads
// settings.json every ~2 s, so a click here applies within seconds. This mod
// only writes settings.json and only reads balance-cache.json.
const MIN = 5;
const MAX = 240;
const STEP = 5;
const DEFAULT_INTERVAL = 10;
const TICK_MS = 2000;

function clamp(n) {
  return Math.max(MIN, Math.min(MAX, n));
}

function ago(iso) {
  const ms = Date.now() - Date.parse(iso);
  if (!iso || Number.isNaN(ms)) return "?";
  const m = Math.floor(ms / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m ago`;
}

export default class EnvBoardControls extends View {
  init(_props, cx) {
    this.dir = `${home()}/.local/state/env-board`;
    this.settings = {
      balance_interval_min: DEFAULT_INTERVAL,
      balance_paused: false,
      refresh_requested_at: null,
    };
    this.cache = null;
    this.busy = false;
    this.note = "";
    if (checking()) return;

    cx.spawn(async (spawnCx) => {
      // Honour XDG_STATE_HOME the same way the board does.
      try {
        const res = await exec(
          "/bin/sh",
          ["-c", 'printf %s "${XDG_STATE_HOME:-$HOME/.local/state}"'],
          { timeout: 2000 },
        );
        if (res.code === 0 && res.stdout) this.dir = `${res.stdout.trim()}/env-board`;
      } catch {
        // Keep the default state dir.
      }
      await this.reload();
      spawnCx.notify();
    });
    cx.timer.every(TICK_MS, async (timerCx) => {
      await this.reload();
      timerCx.notify();
    });
  }

  async reload() {
    const rawSettings = await readTextFile(`${this.dir}/settings.json`).catch(() => null);
    if (rawSettings) {
      try {
        const s = JSON.parse(rawSettings);
        if (Number.isInteger(s.balance_interval_min))
          this.settings.balance_interval_min = clamp(s.balance_interval_min);
        if (typeof s.balance_paused === "boolean") this.settings.balance_paused = s.balance_paused;
        this.settings.refresh_requested_at = s.refresh_requested_at ?? null;
      } catch {
        // Keep the last good settings on a partial write.
      }
    }
    const rawCache = await readTextFile(`${this.dir}/balance-cache.json`).catch(() => null);
    if (rawCache) {
      try {
        this.cache = JSON.parse(rawCache);
      } catch {
        // Keep the last good cache snapshot.
      }
    }
  }

  // Read-modify-write settings.json atomically; stale snapshots reject and retry once.
  async update(patch, cx) {
    if (checking() || this.busy) return;
    this.busy = true;
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        const path = `${this.dir}/settings.json`;
        const raw = await readTextFile(path).catch(() => null);
        let current = {};
        try {
          current = raw ? JSON.parse(raw) : {};
        } catch {
          current = {};
        }
        const next = { ...this.settings, ...current, ...patch };
        next.balance_interval_min = clamp(
          Number.isInteger(next.balance_interval_min)
            ? next.balance_interval_min
            : DEFAULT_INTERVAL,
        );
        await exec("/bin/mkdir", ["-p", "-m", "700", this.dir], { timeout: 2000 }).catch(
          () => null,
        );
        try {
          await writeTextFile(path, `${JSON.stringify(next, null, 2)}\n`, raw);
          // writeTextFile takes no mode: keep settings owner-only like the cache (-h: never follow a symlink).
          await exec("/bin/chmod", ["-h", "600", path], { timeout: 2000 }).catch(() => null);
          this.settings = next;
          this.note = "";
          break;
        } catch {
          this.note = attempt === 0 ? "settings changed underneath; retrying" : "write failed";
        }
      }
    } finally {
      this.busy = false;
      cx.notify();
    }
  }

  button(id, label, action, t, active = false) {
    return div()
      .id(id)
      .px(10)
      .py(5)
      .rounded(6)
      .border(1)
      .border_color(active ? t.accent : t.border)
      .bg(active ? t.raised : t.surface)
      .cursor_pointer()
      .text_size(12)
      .text_color(t.foreground)
      .hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => action(cx))
      .child(label);
  }

  render(cx) {
    const t = theme();
    const s = this.settings;
    const read = this.cache?.read ?? null;
    const err = this.cache?.last_error ?? null;
    return v_flex()
      .id("env-board-controls")
      .size_full()
      .gap(10)
      .p(12)
      .bg(t.canvas ?? t.surface)
      .child(
        div().text_size(13).font_semibold().text_color(t.foreground).child("API balance controls"),
      )
      .child(
        h_flex()
          .gap(8)
          .items_center()
          .child(
            this.button(
              "interval-dec",
              "−",
              (c) => this.update({ balance_interval_min: clamp(s.balance_interval_min - STEP) }, c),
              t,
            ),
          )
          .child(
            div()
              .text_size(13)
              .text_color(t.foreground)
              .child(`every ${s.balance_interval_min} min`),
          )
          .child(
            this.button(
              "interval-inc",
              "+",
              (c) => this.update({ balance_interval_min: clamp(s.balance_interval_min + STEP) }, c),
              t,
            ),
          ),
      )
      .child(
        h_flex()
          .flex_wrap()
          .gap(8)
          .child(
            this.button(
              "pause-toggle",
              s.balance_paused ? "Resume" : "Pause",
              (c) => this.update({ balance_paused: !s.balance_paused }, c),
              t,
              s.balance_paused,
            ),
          )
          .child(
            this.button(
              "refresh-now",
              "Refresh now",
              (c) => this.update({ refresh_requested_at: new Date().toISOString() }, c),
              t,
            ),
          ),
      )
      .child(
        div()
          .text_size(11)
          .text_color(t.muted)
          .child(read ? `last read ${ago(read.read_at)}` : "no balance read yet"),
      )
      .when(Boolean(err), (el) =>
        el.child(
          div()
            .text_size(11)
            .text_color(t.ansi1 ?? t.blocked)
            .child(`error: ${err}`),
        ),
      )
      .when(Boolean(this.note), (el) =>
        el.child(div().text_size(11).text_color(t.dim).child(this.note)),
      );
  }
}
