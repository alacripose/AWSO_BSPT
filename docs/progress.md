# Progress — live demo prep (2026-09-11)

## Done this phase (demo readiness)
- Review + fixes committed on `feat/sos-escalation-and-agent-harness`; PR #1 open
  (github.com/alacripose/AWSO_BSPT/pull/1) — 59/59 pytest, ruff clean, semgrep 0.
- Demo needs the CUSTOM model: Phase 6 training never ran (no models/, no data/,
  no llama-server). Plan: run 0.6B smoke pipeline today, then live demo.
- llama-server b10809 win-cpu-x64 downloaded + extracted to bin/llama-server.exe
  (verified: runs, --version OK) + convert_hf_to_gguf.py (same tag) + llama-quantize.exe.
- ML stack installed in .venv-ml (torch 2.14 CPU, transformers 5.17, peft, gguf).
  CUDA wheels for torch 2.14 unavailable (cu124 index is ≤2.4-era) — CPU path is
  fine for the 0.6B smoke; 1.7B primary can use GPU wheels later.
- Base model Qwen3-0.6B in models/base (1.5GB safetensors).
- Trainer data-prep bugs found + fixed (chat pair only when next row is the
  agent's answer; SOS pair acks by ROW id not fingerprint tail; verb-help
  filter `strip().startswith("  verb")` never matched → `startswith(f"{verb} ")`).
  22 pairs now: 1 real chat, 20 verb invocations, 1 briefing.
- export_gguf fixed: convert script + llama-quantize looked only in ~/llama.cpp
  and /opt; now finds bin/ (Windows-aware), honest message when quantizer absent.
- Demo DB seeded (habits #1/2/3/12/17), mode green, chat primed (#21).
- docs/demo_runbook.md written — 5-minute demo flow.

## In flight
- [bg] LoRA train 0.6B (22 pairs, 3 ep, CPU) → models/qwen3-0.6b-awso-lora
  (session proc_1689202ae010; ~7 min CPU-time so far, on-CPU computing)

## Next
- export → merge + GGUF q8_0 → bench (protocol-probe) → serve :8010
- agentd --interval 5 + kiosk GUI → live demo
- commit trainer fixes on the branch, push (PR #1)
