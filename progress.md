# Progress — fleet hamburger panel session (2026-09-11)

## Done this session
- Kiosk.qml: fleet Drawer (Qt Quick native hamburger) — ☰ header button,
  sectioned ListView (22 categories / 168 rows live), filter field,
  fixed 2-state row heights, rescan on open only. objectNames on
  fleetDrawer/fleetList for offscreen harness lookup.
- habit_gui.py: Bridge.fleetModel() slot (wraps core.fleet_scan, probe off);
  selfcheck gains 7 asserts (model blocked, fleet allowed, fleet model shape).
- habit_core.py: `fleet` added to CONSOLE_SAFE_VERBS; `model` REMOVED
  (Mantis F-06: torch/llama.cpp on Qt thread — F-05 freeze class);
  help_text + console hint updated.
- workspace/findings/F-06.json filed VALID (13-rule mantis-review, code_paths
  verified vs live tree, fix + history entries).
- task_plan.md: Phase 8 (hamburger) complete; renumbered closeout → Phase 9.

## Gates (all green)
- ruff check + format: clean (habit_core.py, habit_gui.py)
- pytest: 59/59 (habit_core 30, astra_effort 23, agentd 6)
- QML offscreen verify: drawer opens, sections built, filter works,
  console gated — zero QML warnings (scratch script removed after pass)
- GUI selfcheck offscreen: all green
- semgrep python/command-injection/secrets: 187 rules, 0 findings

## Not touched (pre-existing dirty state)
- OPENAI_OUTLINE.txt modified, bin/, llama_server.log, recurrent_depth.* —
  predate this session's work; left as-is.

## Left
- Phase 7 (offline end-package) still pending
- Commit with one-off identity per memory
