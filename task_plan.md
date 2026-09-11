# Task Plan — AWSO_BSPT Review + Astra-style Local Model for 4GB CPU Device

## Goal
Per user request: (1) implement a local model that "takes after gpt 6 astra
with depth layers and efficient neuralese communication" targeting a 4GB CPU
solar/battery device, following OPENAI_OUTLINE.txt as the spec; (2) deliver an
offline end-package (code + model + llama-server + installer) that loads and
begins work on the device automatically.

## Governing ruling (user-corrected, 2026-09-10)
GPT-6 Astra is the current OpenAI model; OPENAI_OUTLINE.txt is followed as
written. Its NORMATIVE content (per its own §43–44): five effort levels as a
compute budget + the act→observe→evaluate→verify/replan loop — implemented
HARNESS-side (astra_effort.py) around a small model. Weight-level "depth
layers"/"neuralese" (MoD routing) stay as the dev-box experiment
(model_trainer.py), clearly labeled interpretation, never claimed as Astra
internals. Outline §16/§34 justify the harness split; §43 forbids pretending
to know the weights.

## Phases

### Phase 1 — Review R1-R5 vs codebase [complete]
Feasibility/risk/minimal-approach/plan/rulings delivered (<800 words) — see
review stdout in session log.

### Phase 2 — Design (MODEL_TRAINING_DESIGN.md) [complete]
- Base: Qwen/Qwen3-0.6B (smoke) + Qwen/Qwen3-1.7B (primary), LoRA r16
- "Depth layers" → harness effort budgets + MoD experiment (trainer)
- Target: ≤2GB RAM all-in on 4GB CPU box, tool-call capable

### Phase 3 — astra_effort.py (effort/depth harness) [complete]
- EFFORT_LEVELS/budgets (§1/§22), DUTY_EFFORT (§25), PROTOCOL, HabitctlTools
  whitelist executor, parse_tool_call, run_depth_loop w/ verify+replan+on_step,
  strip_think (Qwen3 raw completion), sanitize (ChatML injection boundary),
  render_chatml/render_plain. Tests: test_astra_effort.py (24 tests).

### Phase 4 — Backends + agentd integration [complete, gates pending]
- local_runner.py: LlamaServerRunner (device, stdlib HTTP to llama-server
  :8010) + LocalModelRunner (dev bindings). Pivot from llama-cpp-python was
  forced by the offline-device requirement (no pip/compiler on device).
- agentd.py: run_agent(user_content, db, duty, verify, on_step) w/ backend
  chain server→bindings→Hermes→rules; mid-duty backend-failure fallback;
  AGENTD_VERBS = console-safe − {chat,model,build,selftest}; per-duty verify
  closures (sos: re-query pending; telemetry: re-check staleness);
  _step_reporter → live agent_status (R5 loaders); trace → events audit.
- test_agentd.py (6 tests).

### Phase 5 — Gates [complete]
- ruff + format clean (9 files); pytest 59/59; selftest + QML selfcheck green
- semgrep (python/sql/command-injection rulesets): 0 findings
- Mantis F-01..F-05 all closed with regression tests (see workspace/findings/)
- Branch feat/sos-escalation-and-agent-harness pushed to github.com/alacripose/AWSO_BSPT — PR #1
- Mantis advise stage applied manually (advise.py absent at skill path — noted in log).

### Phase 6 — Training pipeline [in progress]
- model_trainer.py: prepare_training_data (pair types A/B in EXACT runtime
  ChatML+PROTOCOL via astra_effort — no train/runtime drift), scratch-DB
  real observations, think-stripped chat pairs; train_lora (CPU-safe, QLoRA
  auto-only-on-CUDA); export_gguf (plain merge — bnb 4-bit can't merge);
  benchmark w/ protocol-probe success rate (the real acceptance metric).
- Run order: 0.6B smoke (proves prepare→train→merge→GGUF→quant→bench),
  then 1.7B primary (background, notify). Quant: 0.6B→q8_0, 1.7B→q4_K_M.

### Phase 7 — Offline end-package (user requirement, 2026-09-10) [pending]
- deploy/offline/: make_bundle.py → dist tarball (app/ + bin/llama-server
  pinned b10809 per-arch + model/ GGUF + units + SHA256SUMS), install.sh
  (hash-verify before exec, /opt/awso code root-owned, DB at /var/lib/awso
  so upgrades never clobber operator data, --check-only, --arch check,
  LF normalization gate, --start-delay systemd timer for R2, enable --now,
  health checks = "begin work").
- Units: awso-llama.service (127.0.0.1:8010 only, -c 2048, MemoryMax,
  Restart=on-failure); awso-agentd.service update (After/Wants awso-llama,
  --db /var/lib/awso/habits.db, ReadWritePaths both roots).
- Acceptance: install.sh --check-only on dev box; honest report that device
  smoke is the ceiling unless operator boots real hardware.

### Phase 8 — Closeout [pending]
- FLEET_LOCAL_MODELS entries with MEASURED RAM/latency/protocol-success
- README/docs updates, final report (review R1-R5 + outline mapping + package)

## Errors Encountered
| Error | Resolution |
|-------|------------|
| Training from scratch infeasible on 4GB | LoRA fine-tune of small bases |
| llama-cpp-python on offline device (no pip/compiler) | Pivot to prebuilt llama-server binary + stdlib HTTP client |
| bnb 4-bit merge unsupported on CPU/Windows | Plain fp merge in export; QLoRA only when CUDA |
| llama.cpp release asset filter matched nothing | Release is a nightly pointer (b10809); re-queried by tag — ubuntu x64/arm64 + win-cpu assets exist |
| Mantis advise.py missing at skill path | Principles applied manually: input validation at boundaries, no shell, hash pinning, MemoryMax, localhost-only bind |
| 0.6B "low effort" briefing would exhaust on first TOOL | DUTY_EFFORT[briefing] = medium |
