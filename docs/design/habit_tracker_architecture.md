# Design — AWSO_BSPT Habit Tracker

## What it is

A standardized, business-centric habit tracker built on three runtimes of the
same core:

1. **Headless core** (`habit_core.py`) — pure stdlib Python (sqlite3,
   argparse). No Qt. Runs anywhere, cheaply. This is what lives on the
   solar/battery device.
2. **GUI** (`habit_gui.py`) — one PySide6 window, four tabs, attaches to the
   same SQLite file. Reuses the parent project's theme/QSS conventions.
3. **CLI surface** (`python3 habit_core.py …` / `habitctl` shim) — the control
   surface for humans and subagents alike. Every GUI mutation routes through
   the same core functions, so GUI and agents share one truth.
4. **Resident agent harness** (`agentd.py`) — the always-on agent: heartbeats
   presence into meta (GUI shows ONLINE/OFFLINE), polls the chat mailbox and
   answers via one-shot Hermes subagents, posts a daily briefing, sweeps SOS
   and stale telemetry. Deployed as `deploy/awso-agentd.service`. The GUI is a
   pure view and never spawns agents (agent-operated-apps architecture).

The fork deletes the parent's entire window-manager chassis (drag/dock/split
panes, taskbar, 10 sample apps, ~2,300 lines). "As few apps as possible" is a
design constraint: **one binary-ish core, one window, one DB file.**

## Design decisions (log as you go)

| # | Decision | Why |
|---|----------|-----|
| D1 | SQLite single-file, WAL | Zero-resident telemetry ingestion, one-file backup, standard on every Linux install |
| D2 | Pure-stdlib core, Qt only in GUI | Solar device never loads Qt; GUI still runs on cheap framebuffer displays — Qt Widgets uses a raster backend, **no OpenGL required** |
| D3 | CLI as the agent surface | Subagents already have terminal tools; `--json` everywhere makes them machine-readable; same verbs the GUI uses |
| D4 | Checkins carry `source` (human/agent:name) | Audit trail for agent-driven habit management |
| D5 | Capacity modes GREEN/YELLOW/RED + per-habit effort tier | Carried from parent project's operator-capacity design; low capacity defers heavy habits at read time |
| D6 | `watch` loop spawns GUI when display appears | Device boots headless; plugging a monitor in later gets you the GUI without restarting the core |
| D6b| GUI always attached when display exists | `watch` launches the GUI by default whenever a display is present, honoring "always goes to the GUI eventually"; `--headless` pins headless |
| D7 | Remote control = SSH/WireGuard, no custom server | A battery box should not run a new listening daemon; SSH gives encryption + auth + the same CLI |
| D8 | Telemetry as generic key/value readings | Solar/battery monitoring without hardcoding a vendor; `log-reading` from cron |
| D9 | Streaks never break mid-period | Today is grace: streaks only break when the period ends unmet |
| D9b| `weekdays` cadence: Sat/Sun count as grace | Weekend remains due-on-paper but never breaks a streak (deliberate; adjust per habit if needed) |

## Schema

```sql
habits  (id, name, description, cadence, effort, category, archived, created)
checkins(habit_id, ts, source, note)          -- source: 'human' | 'agent:<name>' | 'device'
readings(key, ts, value REAL, unit)            -- telemetry: solar W, battery V/SoC...
meta    (mode, next_id)                        -- operator mode + id sequence
```

Cadences: `daily` | `weekdays` | `weekly:N` (N days) | `interval:Nh` (N hours).

## Capacity modes

| Mode | Meaning (from parent design) | Effect |
|------|-------------------------------|--------|
| 🟢 GREEN | Active / full operations | All habits due |
| 🟡 YELLOW | Limited capacity | Heavy (yellow/red) habits deferred — suggested, not deleted |
| 🔴 RED | Away / continuity | Only green-effort habits due |

## Subagent control (Hermes)

Day-to-day verbs for a subagent: `list --json`, `due --json`, `check`,
`uncheck`, `info`, `streaks --json`, `mode`, `report`, `log-reading`,
`readings --json`, `stats --json`, `watch --headless`. Over SSH:

```
ssh device habitctl due --json
ssh device habitctl check <id> --source agent:overnight-bot
ssh device habitctl log-reading solar_w 842.5 W
```

## Deployment shape (device)

```
awso-habit.service      → habitctl watch        (spawns GUI if display)
awso-telemetry.timer     → reader script → habitctl log-reading ...
```

Details: `docs/design/deployment.md`.

## Ceilings (deliberate, upgrade paths named)

- SQLite single file → networked sync only if multi-device reality appears.
- One GUI window → tabbed layout is the ceiling before a second window is earned.
- Pull-based `watch` for display detection → udev/DRM hotplug notification if 60 s feels slow.
- No push nags → add when missed-habit escalation is actually wanted.
- Streak model is local-time | no TZ travel edge cases | TZ-aware periods if the fleet crosses timezones.
