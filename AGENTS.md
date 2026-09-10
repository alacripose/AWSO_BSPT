# AWSO_BSPT — Agent Rules

Applies to every agent working in this repo: Hermes sessions, Hermes subagents,
and delegated implementer CLIs (Claude Code, Codex, OpenCode, Cursor, ...).

## Required: Mantis security review (google/mantis)

The Mantis security-review skill suite is a hard requirement of this project.

- Before declaring any change that touches `habit_core.py`, `habit_gui.py`, or
  `deploy/` complete, run a Mantis review pass over it (`mantis-review` at
  minimum; `mantis-plan` / `mantis-researcher` for a full sweep).
- Crash-like findings must be reproduced via `mantis-reproduce` inside an
  isolated, network-disabled container. Never run reproducers on the host.
- Mantis findings are leads, not proof: verify manually before reporting, and
  never mass-file unverified AI-generated reports.

## Required: use the operator's installed skills

Do not work bare — the operator's installed skill set is part of this project's
toolchain.

- Delegate bounded implementation work to an implementer CLI through the
  matching `*-delegate` skill (`claude-delegate`, `codex-delegate`,
  `opencode-delegate`, `cursor-delegate`, `aider-delegate`, ...) and stay the
  reviewer: review the implementer's diff and land it yourself.
- In Hermes, when a task matches an installed skill, load it with `skill_view`
  and follow it. Hermes sessions run the skill-retrieval plugin
  (moonlight-lupin/agent-skills — BM25 top-K injection), so the full skill list
  is not in context; skills remain discoverable by name and are injected per
  turn.
