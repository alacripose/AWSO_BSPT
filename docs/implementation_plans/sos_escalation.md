# Implementation Plan — SOS escalation to Hermes (agent responds with changes)

## Goal

The program (kiosk/device) must send "SOS"-style messages to the Hermes
instance when something is wrong, and the agent must respond **as soon as
possible** with actual changes — not just a notification.

## Design

```
[sos table: pending rows, deduped by fingerprint]
        ↑ sos_scan()                ↓ every 1 minute
habit_core watch tick        awso_sos_watch.py (cron monitor script,
(kiosk refresh too)           pure SQL check — zero LLM cost when clean)
                                      ↓ output changes only when rows appear
                               Hermes cron wakes the AGENT (fresh session)
                                      ↓ agent triages, acts via habitctl CLI,
                                        acks SOS, replies in kiosk chat
```

- **Detection rules** (`sos_scan`): `missed_daily` (high — daily habit missed
  yesterday AND still unchecked today, habit existence respected), 
  `streak_broken` (medium — streak 0 after >=3 lifetime check-ins),
  `telemetry_stale` (medium — any known key silent >48h). DB-down is reported
  by the monitor itself (CLI failure → agent wake).
- **Dedupe**: partial unique index on `fingerprint WHERE status='pending'` —
  a crash loop files one SOS, not one per minute. Ack clears the slot.
- **Transport**: cron `monitor` script — output is empty while clean (no LLM
  run), changes to "URGENT: N pending" when SOS exists → agent wakes ≤1 min
  after the program files it.
- **Agent contract** (in the cron prompt): read AGENTS.md, triage each SOS,
  act via CLI, `sos ack <id> --note <fix>`, reply in kiosk chat so the
  operator sees it.

## Verification

- selftest: SOS detect/dedupe/ack/stale-telemetry (6 new checks, 25/25 green)
- monitor script: fires on pending, silent on clean (verified against temp DB)
- cron: monitor-gated every 1m, workdir injects AGENTS.md
- End-to-end live test: seed a real overdue habit → SOS → agent wakes, fixes,
  acks, reports.
