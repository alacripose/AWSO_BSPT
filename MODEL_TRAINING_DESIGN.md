# Local Model Training Architecture — AWSO_BSPT 4GB CPU Target

## Design Goal
A local LLM that "takes after gpt 6 astra with depth layers and efficient neuralese communication" — interpreted as:
- **Depth layers**: Mixture-of-Depths (MoD) / early-exit transformer blocks — compute only what's needed per token
- **Neuralese communication**: Efficient inter-layer routing, compressed latent representations between blocks, minimal KV cache growth
- **Target**: ≤2GB RAM (model + KV + runtime), CPU-only, <500ms/token on 4GB device, tool-call capable

## Base Model Selection (from FLEET_LOCAL_MODELS)

| Model | Params | RAM (q4_K_M) | Tool Calls | MoD Support | Verdict |
|-------|--------|--------------|------------|-------------|---------|
| qwen3:1.7b | 1.7B | ~1.3GB | ✓ | Sliding window attention | **Primary** — smallest reliable tool-caller |
| granite3.3:2b | 2.5B | ~1.8GB | ✓ | Standard attention | **Secondary** — tight tool schemas |
| llama3.2:3b | 3.2B | ~2.2GB | ✓ | Grouped-query attention | **Fallback** — if RAM allows |

**Decision**: Start with **qwen3:1.7b** — smallest footprint, native sliding attention (natural MoD proxy), proven tool calling.

## Architecture: "Astra-Lite" (Mixture of Depths + Neuralese)

```
┌─────────────────────────────────────────────────────────────┐
│  Input Tokens → Embedding (1.7B shared)                     │
├─────────────────────────────────────────────────────────────┤
│  Transformer Backbone (24 layers, qwen3 config)             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  Layer 1..N:                                        │   │
│  │  ┌─────────┐   ┌─────────────┐   ┌───────────────┐  │   │
│  │  │ Attention│→ │ Neuralese   │→ │  FFN          │  │   │
│  │  │ (sliding)│  │ Router      │  │  (SwiGLU)     │  │   │
│  │  └─────────┘  └─────────────┘  └───────────────┘  │   │
│  │         ↑              ↑                ↑          │   │
│  │    KV Cache      Compressed      Depth Gate      │   │
│  │    (standard)    Latent (16D)    (exit vs deepen) │   │
│  └─────────────────────────────────────────────────────┘   │
│                    ↑                                        │
│                    │  ┌─────────────────────────────────┐  │
│                    └─→│  DEPTH EXPERT LAYERS (optional)  │  │
│                       │  Layer N+1..N+K: additional       │  │
│                       │  capacity for complex tokens      │  │
│                       │  (only activated by router)       │  │
│                       └─────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  Output Head → Logits (tied embedding)                      │
└─────────────────────────────────────────────────────────────┘
```

### Key Components

1. **Neuralese Router** (per backbone layer, ~50K params)
   - Input: hidden state (2048D for qwen3:1.7b)
   - Output: 
     - Routing weights (num_experts=4) for neuralese compression
     - **Depth gate**: binary — EXIT (output logits) vs DEEPEN (continue to next layer OR enter expert stack)
   - Compressed latent: 16D bottleneck between layers (Neuralese communication)
   - Trained with auxiliary loss: `L_router = CE(depth_gate, target_depth) + MSE(latent_reconstruction)`

2. **Depth Gates — Vertical Depth Control**
   - Per layer, per token: `gate = sigmoid(W_gate @ hidden + b_gate)`
   - `gate > 0.5` → EXIT (token resolved, skip remaining backbone layers)
   - `gate ≤ 0.5` → DEEPEN (continue to next backbone layer)
   - **Expert stack entry**: If `gate ≤ 0.5` AND `layer >= EXPERT_ENTRY_LAYER` (e.g., layer 12), token enters expert stack (K extra layers)
   - Target: 40-60% tokens exit by layer 12; 10-20% enter expert stack (layers 13-16)

3. **Expert Stack (Vertical Depth)**
   - K=4 additional transformer layers (shared across all tokens that enter)
   - Same architecture as backbone but specialized for "hard" reasoning
   - Only activated for routed tokens — **sparse vertical computation**
   - Neuralese latent (16D) carries compressed context from backbone → expert entry

4. **Compressed Inter-Layer Latents (Neuralese)**
   - Instead of full hidden state (2048D) between layers → project to 16D
   - Next layer: project 16D → 2048D (learned expansion)
   - Expert entry: 16D latent → expert layer input projection
   - Saves KV cache bandwidth, enables speculative decode, enables depth routing

5. **Sliding Window Attention** (native qwen3)
   - Window: 4096 tokens (configurable)
   - Already in base model — no architectural change needed

### Compute Flow (Per Token)

```
Token enters Layer 1
  → Attention → Neuralese Router → Depth Gate
    ├─ EXIT (gate > 0.5) → Output Head → Logits (DONE)
    └─ DEEPEN (gate ≤ 0.5) 
        → Layer 2 → ... → Layer 12
            → Depth Gate
              ├─ EXIT → Output Head
              └─ DEEPEN → Expert Stack (Layers 13-16) → Output Head
```

**This is true vertical depth**: tokens dynamically choose their computation depth. Standard transformers force ALL tokens through ALL layers (left-to-right only). MoD + Expert Stack = adaptive vertical depth.

## Training Pipeline (Two-Stage)

### Stage 1: Continued Pre-training / Domain Adaptation (Optional)
- Corpus: AWSO_BSPT chat + events + sos + telemetry + habitctl help texts
- ~50K tokens total — too small for pre-training, skip
- **Decision**: Go straight to Stage 2

### Stage 2: LoRA Fine-Tune + Router Training
```
Base: qwen3:1.7b (frozen) + LoRA adapters (r=16, alpha=32) on q,k,v,o,gate,up,down
Added: Neuralese routers (trainable) + Early-exit gates (trainable)
Data: 
  - Synthetic: habitctl verb → JSON tool call pairs (500 samples)
  - Real: agentd chat history (human→agent, agent→tool→result)
  - Replay: SOS triage traces, briefing compositions
Format: ChatML with tool calls as <|tool_call_begin|>...<|tool_call_end|>
```

### Quantization & Deployment
1. Merge LoRA → base model
2. Convert to GGUF (llama.cpp): `q4_K_M` (4-bit, K-quant medium)
3. Verify: `llama-cli -m model.gguf -p "test" -n 128 --benchmark`
4. Register in FLEET_LOCAL_MODELS as `qwen3:1.7b-awso-q4km`

## Data Pipeline

```python
# model_trainer.py:prepare_training_data()
def prepare_training_data(db_path: Path) -> list[dict]:
    """Extract training pairs from SQLite."""
    conn = connect(db_path)
    pairs = []
    
    # 1. Chat mailbox: human message → agent reply (with tool calls)
    for row in conn.execute("SELECT text FROM chat WHERE sender='human'"):
        human = row["text"]
        # Find next agent reply
        agent = conn.execute("""
            SELECT text FROM chat 
            WHERE sender='agent:awso-agentd' AND id > ?
            ORDER BY id LIMIT 1
        """, (row["id"],)).fetchone()
        if agent:
            pairs.append({"prompt": human, "completion": agent["text"]})
    
    # 2. SOS triage: sos row → agent action sequence
    for sos in conn.execute("SELECT * FROM sos WHERE status='acked'"):
        # Reconstruct: sos context → tool calls → ack note
        pass
    
    # 3. Synthetic: every habitctl verb → example invocation
    for verb in CONSOLE_SAFE_VERBS:
        pairs.append({"prompt": f"How do I {verb}?", "completion": f"habitctl {verb} ..."})
    
    return pairs
```

## Evaluation Benchmarks

| Metric | Target | Method |
|--------|--------|--------|
| RAM (model+KV+runtime) | <2GB | `psutil.Process().memory_info().rss` during inference |
| Latency (token) | <500ms | `llama-cli --benchmark` on 4GB CPU (simulated via cgroups) |
| Tool call accuracy | >90% | Held-out habitctl verb test set (50 samples) |
| Early-exit rate | 40-60% | Router gate stats on validation |
| Perplexity (domain) | <5.0 | Validation split of chat/sos data |

## Integration Points

1. **habit_core.py**: Extend `FLEET_LOCAL_MODELS` with trained model entry
2. **habit_core.py**: `recommend_local_model()` prefers `*_awso_*` variants
3. **agentd.py**: New `LocalModelRunner` class — loads GGUF via llama.cpp Python bindings (optional dep), falls back to Hermes cloud
4. **CLI**: `habitctl model train|export|bench|list`
5. **fleet_scan()**: Shows trained model status (installed, benchmarked)

## Dependencies (Optional, Lazy-Loaded)

```python
# model_trainer.py — only imported when `habitctl model train` runs
try:
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer
except ImportError:
    torch = peft = transformers = None

# Runtime (agentd.py): llama-cpp-python for GGUF inference
try:
    from llama_cpp import Llama
except ImportError:
    Llama = None
```

**Constraint**: `habit_core.py` stays pure stdlib. All ML deps isolated in `model_trainer.py` and optional `local_runner.py`.

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Training too slow on 4GB CPU | Train on dev machine (GPU), deploy quantized GGUF to device |
| LoRA + Router overfits tiny data | Strong regularization (dropout=0.1, weight_decay=0.01), early stopping |
| GGUF conversion fails | Test conversion pipeline first with base qwen3:1.7b |
| Tool call format mismatch | Use same ChatML format as Hermes; validate with `habitctl` verbs |
| OOM at runtime | `llama.cpp` context size 2048, n_threads=CPU-1, n_gpu_layers=0 |

## File Structure (New)

```
AWSO_BSPT/
├── model_trainer.py          # Training pipeline (LoRA + Router)
├── local_runner.py           # GGUF inference wrapper for agentd
├── deploy/
│   └── model-download.sh     # Fetch base model, convert to GGUF
└── data/
    └── training_pairs.jsonl  # Generated from SQLite
```

## Acceptance Criteria (Phase 2 Complete)

1. ✅ Design doc written (this file)
2. ✅ `model_trainer.py` skeleton with `prepare_training_data()`, `train_lora()`, `train_router()`, `export_gguf()`
3. ✅ `local_runner.py` with `LocalModelRunner` class (load, generate, benchmark)
4. ✅ Integration hooks in `habit_core.py` (FLEET_LOCAL_MODELS entry, recommend_local_model pref)
5. ✅ CLI verb `habitctl model` with subcommands
6. ✅ Ruff clean, pytest stubs pass

## Ruling & Device Runtime (2026-09-10, user-corrected)

**Outline fidelity.** OPENAI_OUTLINE.txt is the spec, as written. Its own
§43–44 draw a hard line between documented behavior and architectural
guesswork: the documented, normative semantics are (a) five reasoning-effort
levels as a compute budget over deliberation (§1/§22), (b) the
act → observe → evaluate → verify/replan loop with a preference for executing
over guessing (§5/§28/§42). Those live HARNESS-side in `astra_effort.py`
around a small model — the outline itself (§16/§34) frames harness depth as
the multiplier of model capability. The weight-level reading below (MoD
routers, early-exit gates, compressed latents) remains valid as our own
"depth layers/neuralese" experiment, clearly labeled interpretation; it is
never claimed to be Astra's internal mechanism.

**Device runtime pivot.** The device is offline Linux: no pip, no compiler,
no wheel match — so llama-cpp-python is a dev-box path only. The device runs
llama.cpp's prebuilt `llama-server` (pinned nightly b10809, per-arch asset)
bound to 127.0.0.1:8010, driven by `local_runner.LlamaServerRunner` over
stdlib HTTP with raw ChatML `/completion` — the exact prompt string the model
was tuned on, so there is zero template drift between training and runtime.

**Quantization per size.** 0.6B → q8_0 (~0.7GB; q4 over-compresses at that
scale), 1.7B → q4_K_M (~1.1GB). Budget on the 4GB box stays ≤ half RAM
(recommend_local_model policy); `MemoryMax` on the llama unit is the
guardrail, and agentd falls back server→bindings→Hermes→rules if the server
is killed for memory.

**Fleet honesty.** FLEET_LOCAL_MODELS entries for trained models get
MEASURED numbers (RAM via RSS during bench, ms/token, tool-protocol success
rate) — never estimates.

**Data safety.** Training pairs derive from operator chat/SOS logs: `data/`
is gitignored, and the offline bundle ships the GGUF only, never
`training_pairs.jsonl`. The device DB lives at `/var/lib/awso/habits.db`
(outside `/opt/awso`) so bundle upgrades can never clobber operator data.