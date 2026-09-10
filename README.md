# AWSO_BSPT — Habit Tracker

Fork of `~/Documents/simulators/portal_simulator.py` (business-autonomy portal)
into a standardized, business-centric habit tracker: one core, one CLI, one
GUI window.

## How it's used

- **You** — live in the GUI: see what's due, click check-ins, create/edit
  habits, glance at telemetry and the activity feed.
- **Your agent bot** (a Hermes subagent) — fills the pages for you, day to
  day: checks habits in, logs readings, adjusts habits, sets your capacity
  mode. Same CLI, run locally or over SSH. Every action is tagged
  `agent:<name>` in the audit log and appears in the GUI's activity feed
  on the next refresh (≤30 s) — no restart needed.
- One SQLite file (WAL) is the shared truth; the GUI is a pure view.

## Quick start

```bash
python3 habit_core.py add "Morning inventory scan" --cadence daily
python3 habit_core.py check 1                 # or click it in the GUI
python3 habit_core.py gui                     # attach the GUI
python3 habit_core.py selftest                # engine self-check
```

`habitctl` is a two-line shim over `python3 habit_core.py` for device use.

## Layout

```
habit_core.py    pure-stdlib engine + CLI (headless product + agent surface)
habit_gui.py     PySide6 GUI (one window, four tabs) — display-only
deploy/          habitctl shim + systemd units + telemetry reader template
docs/            implementation plans, design log, build log
```

## Agent control (the fill-the-pages surface)

```bash
habitctl due --json                           # what needs doing right now
habitctl check 2 --source agent:hermes-ops    # check in, audit-tagged
habitctl log-reading <key> <value> <unit>     # any telemetry your site has
habitctl mode yellow                          # capacity dropped
habitctl streaks --json                        # how the business is doing
```

Readings are free-form key/value/unit — wire whatever your deployment
actually measures (sensors, prices, counters, anything).

## Capacity modes

🟢 GREEN (full operations) · 🟡 YELLOW (limited — heavy habits deferred) ·
🔴 RED (away — only green-effort habits due). Same design language as the
parent business-autonomy portal.

## Data

Everything lives in one SQLite file (`habits.db`, gitignored). Back up by
copying that file. WAL mode means concurrent GUI + agent access is safe.
