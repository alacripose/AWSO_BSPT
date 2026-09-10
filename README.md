# AWSO_BSPT — Habit Tracker

Fork of `~/Documents/simulators/portal_simulator.py` (business-autonomy portal)
into a standardized, business-centric habit tracker. One core, one CLI, one
GUI window. Built for a small Linux device (solar + battery monitoring)
managed by Hermes — headless-cheap by default, GUI whenever a display exists.

## Quick start

```bash
python3 habit_core.py add "Morning inventory scan" --cadence daily --effort green
python3 habit_core.py list
python3 habit_core.py check 1 --note "all zones green"
python3 habit_core.py gui            # attach the GUI (spawns only with a display)
python3 habit_core.py selftest        # run the engine self-check
```

`habitctl` is a two-line shell shim over `python3 habit_core.py` for
device use (see `deploy/habitctl`).

## Layout

```
habit_core.py    pure-stdlib engine + CLI (the whole headless product)
habit_gui.py     PySide6 GUI (one window, four tabs) — optional, display-only
deploy/          habitctl shim + systemd units + telemetry reader example
docs/            implementation plans, design log, build log
```

## Subagent control

Hermes subagents drive habits via the CLI (local or over SSH):

```bash
habitctl due --json                          # what's due right now
habitctl check 2 --source agent:hermes-ops   # check in with audit trail
habitctl streaks --json                       # how the business is doing
habitctl mode yellow                          # operator capacity dropped
```

Every checkin records who/what did it (`human`, `agent:<name>`, `device`).

## Capacity modes

🟢 GREEN (full operations) · 🟡 YELLOW (limited — heavy habits deferred) ·
🔴 RED (away — only green-effort habits due). Same design language as the
parent business-autonomy portal.

## Data

Everything lives in one SQLite file (`habits.db`, gitignored). Back up by
copying that file. WAL mode means concurrent CLI + GUI access is safe.
