# AWSO_BSPT — Live Demo Runbook

**Scenario**: the 4GB solar/battery device, demoed on the dev box. One window
kiosk, one resident agent driven by the CUSTOM fine-tuned model.

## Pre-flight (done)
- [x] `bin/llama-server.exe` b10809 (same pinned build as the device unit)
- [x] `models/base` Qwen3-0.6B downloaded
- [x] ML venv (CPU torch) + trainer fixes (pair-matching, SOS row id, verb
      filter, convert/quantize binary lookup)
- [x] `data/training_pairs.jsonl` — 22 pairs from live DB (chat/SOS/verbs/briefing)
- [x] Demo DB seeded (habits #1/2/3/12/17), mode green, chat primed

## Demo flow (5 min)

1. **Serve the custom model** (device-style, localhost only):
   `bin/llama-server.exe -m models/qwen3-0.6b-awso-q8_0.gguf -c 2048 --port 8010 --host 127.0.0.1`
2. **Launch agentd** (resident harness; picks up the server automatically):
   `python3 agentd.py --interval 5`
3. **Launch the kiosk**: `python3 habit_gui.py`
4. Story beats:
   - Agent ONLINE heartbeat → agent status line live
   - Type in chat: "make coffee" style requests — agent answers through the
     model, live status shows act→observe→verify steps (R5 loaders)
   - SOS: agentd's sos_sweep files missed-daily → dispatches triage →
     verified ack lands in chat + SOS feed
   - Telemetry tab: `habitctl log-reading` (or timer) → readings appear
   - Capacity modes: red → yellow habits defer live
5. **Kill switch**: Ctrl-C agentd + llama-server; kiosk stays standalone.

## Notes
- Custom model = Qwen3-0.6B + LoRA (22 pairs, 3 epochs, CPU) → merged →
  GGUF q8_0 → llama-server. Harness does the "Astra" effort budgeting
  (astra_effort.py); weights are the tuned base.
- If the model misbehaves (tiny data), the loop still proves the system:
  tool validation + verified acks + effort budget + fallback chain.
- 1.7B q4_K_M primary train can run later (GPU wheels or overnight CPU).
