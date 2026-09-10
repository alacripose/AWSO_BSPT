# Implementation Plan — AWSO_BSPT Habit Tracker (Fork of portal_simulator.py)

Fork `simulators/portal_simulator.py` into a standardized, business-centric habit
tracking system. The parent's window-manager chassis (drag/dock/split panes,
10 apps, taskbar) is **replaced**, not carried: the fork is one habit engine,
one CLI (`habitctl`), one GUI window. Target deployment is a small headless
Linux device (solar array + battery monitoring, Hermes-managed) that runs
cheaply and attaches a GUI whenever a display is present.

---

## Stage 0 — Scaffold & documentation home  ✅ (2026-09-10)

- Git repo at `AWSO_BSPT/`, docs tree mirroring parent repo conventions:
  `docs/implementation_plans/`, `docs/design/`, `docs/logs/`.
- This plan, the architecture/decision log, and the build log.
- **Verify**: files exist, initial commit made.

## Stage 1 — Core engine `habit_core.py`  ✅ (2026-09-10)

Pure stdlib (sqlite3 + argparse only, no Qt). Single SQLite file, WAL mode.

- Schema: `habits`, `checkins` (with `source` audit trail), `readings`
  (generic telemetry: solar/battery), `meta` (mode, id counter).
- Cadences: `daily`, `weekdays`, `weekly:N`, `interval:N` with streak math
  per cadence (open-period grace: today never breaks a streak until it ends).
- Operator capacity modes GREEN/YELLOW/RED carried over from the parent
  project's design language; each habit carries an effort tier
  (green/yellow/red). Low capacity defers heavy habits — computed at read
  time, never mutates data.
- Telemetry: `log-reading` / `readings` / `stats` — zero-resident-cost
  ingestion (a cron/timer line calls the CLI, inserts one row, exits).
  Keys are free-form key/value/unit: wire whatever the site measures.
- `watch` loop: 60 s tick, spawns the GUI when a display appears, never
  loads Qt itself. `--headless` pins it off.
- CLI = the agent-bot control surface (the bot fills the GUI pages):
  `add list check uncheck info edit due streaks mode report status
  log-reading readings stats watch gui selftest version`, with `--json`
  on every machine-readable verb. Human lives in the GUI; GUI's Today tab
  shows a Recent-activity feed so the human sees what the bot did.
- **Verify**: `python3 habit_core.py selftest` green; `py_compile` clean.

## Stage 2 — Subagent workflow proof  ✅ (2026-09-10)

- Seed representative business habits via CLI into a temp DB (not committed).
- Exercise every verb a Hermes subagent would use day-to-day; confirm JSON
  outputs parse and checkins carry `source=agent:<name>` for audit.
- Recipe documented in README (§ Agent control).
- **Verify**: end-to-end run in build log; live DB ships **empty** — the
  agent bot fills the pages (no demo clutter).

## Stage 3 — GUI `habit_gui.py`  ✅ (2026-09-10)

One `QMainWindow`, four tabs (Today / Habits / Telemetry / Console). Themes
and QSS approach reused from the parent file (trimmed to what's used). The
GUI is a *view*: every mutation goes through `habit_core` functions or the
CLI, so GUI and subagents can never diverge.

- Today: mode pills (🟢🟡🔴), due list with one-click check, streaks, deferrals.
- Habits: table + add/edit form (archive via edit).
- Telemetry: source filter, recent readings, last-24 h min/avg/max per key.
- Console: live core event log + input line that runs the same CLI verbs
  (single source of truth — the GUI console literally calls `cli()`).
- Headless guard: refuses to start without a display unless
  `QT_QPA_PLATFORM=offscreen`; `habitctl gui` is the documented attach path.
- **Verify**: `QT_QPA_PLATFORM=offscreen python3 habit_gui.py --selfcheck`
  green (proves headless operation), plus a normal windowed run.

## Stage 4 — Device deployment & secure remote control  ✅ (2026-09-10, docs)

No custom server code. The secure connection is the transport Linux already
ships:

- `systemd` units: `awso-habit.service` (watch loop) + `awso-telemetry.timer`
  (calls `habitctl log-reading` from a reader script).
- Remote subagent control over SSH (key-only) — no new listening surface on a
  battery-powered box. Tailscale/WireGuard when leaving the LAN.
- Backup story: WAL SQLite is one file; `scp habits.db` is the whole DR plan.
- **Verify**: unit files + commands documented in `docs/design/deployment.md`.

## Stage 5 — Future (deliberately not built yet)

- Push notifications / habit escalation nags.
- Multi-device sync, TUI dashboard, return-report digests for away periods.
- Add when a real need shows up (see design doc's "Ceilings" section).
