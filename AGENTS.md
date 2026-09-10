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

## Required: use the operator's installed skills — aggressively

Do not work bare — the operator's installed skill set is part of this project's
toolchain. Use skills as the default way of working, not a last resort: before
starting any non-trivial task, check whether an installed skill covers it, and
prefer following the skill over improvising. When in doubt, load the skill and
follow it — the cost of loading is trivial compared to the cost of an unskilled
mistake.

- Delegate bounded implementation work to an implementer CLI through the
  matching `*-delegate` skill (`claude-delegate`, `codex-delegate`,
  `opencode-delegate`, `cursor-delegate`, `aider-delegate`, ...) and stay the
  reviewer: review the implementer's diff and land it yourself.
- In Hermes, when a task matches an installed skill, load it with `skill_view`
  and follow it. Hermes sessions run the skill-retrieval plugin
  (moonlight-lupin/agent-skills — BM25 top-K injection), so the full skill list
  is not in context; skills remain discoverable by name and are injected per
  turn. If a task looks like it matches a skill that wasn't injected, look it
  up by name before assuming it doesn't exist.

### Quality & performance gates (installed skills — use them constantly)

- `ruff` — lint and format `habit_core.py` / `habit_gui.py` with Ruff before
  declaring any change complete. Sub-second; no reason to skip it.
- `pytest-coverage` — drive the test loop coverage-first: run pytest with
  `--cov`, review annotated gaps, close them. The engine is pure-stdlib and
  eminently unit-testable.
- `python-performance-optimization` — consult before optimizing anything in
  the engine. The device runs on solar + battery: measure first (cProfile /
  tracemalloc / py-spy), optimize one change at a time, re-measure. Never add
  a third-party dependency to `habit_core.py` to "fix" performance.
- `sqlite` — habits.db is WAL-mode with concurrent CLI + GUI access. Before
  touching any database code, follow this skill's rules (busy_timeout,
  BEGIN IMMEDIATE for read-then-write, WAL/-shm file handling on backup).
- `semgrep` — run a Semgrep scan (Trail of Bits rulesets) alongside the Mantis
  review pass for changes that touch the CLI, database layer, or `deploy/`.
  Deterministic SAST cross-checks the LLM review.
- `linux-hardening` — when touching `deploy/` or anything that configures the
  device (SSH, systemd, firewall), follow CIS/SSH hardening guidance from
  this skill.
- Mantis `mantis-advise` — consult the learned threat model and past-findings
  context before and during code edits, not just at review time.
