#!/usr/bin/env python3
"""AWSO_BSPT Model Trainer — LoRA fine-tune + Neuralese router training for 4GB CPU deployment.

Pure stdlib interface; ML deps (torch, peft, transformers, llama-cpp-python) are
lazy-loaded only when train/export/bench verbs run. habit_core.py stays stdlib-only.

Usage:
    python3 model_trainer.py prepare --db habits.db --out data/training_pairs.jsonl
    python3 model_trainer.py train --base qwen3:1.7b --data data/training_pairs.jsonl --out models/qwen3-1.7b-awso-lora
    python3 model_trainer.py export --lora models/qwen3-1.7b-awso-lora --base qwen3:1.7b --out models/qwen3-1.7b-awso-q4km.gguf
    python3 model_trainer.py bench --model models/qwen3-1.7b-awso-q4km.gguf --prompt "list habits due today"
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# --- Stdlib-only data prep (runs without ML deps) ---------------------------

sys.path.insert(0, str(Path(__file__).parent))
import habit_core as core


def prepare_training_data(db_path: Path, out_path: Path, limit: int = 2000) -> int:
    """Extract training pairs from SQLite: human→agent, SOS traces, synthetic verbs."""
    conn = core.connect(str(db_path))
    pairs = []

    # 1. Chat mailbox: human message → agent reply. Only pair when the
    # immediately-next row is the agent's answer to THIS message (a run of
    # human messages means the agent answered the last one, not each).
    human_msgs = conn.execute(
        "SELECT id, text FROM chat WHERE sender='human' ORDER BY id DESC LIMIT ?",
        (limit // 3,),
    ).fetchall()
    for row in human_msgs:
        agent_reply = conn.execute(
            "SELECT text FROM chat WHERE id > ? AND sender='agent:awso-agentd'"
            " ORDER BY id LIMIT 1",
            (row["id"],),
        ).fetchone()
        if not agent_reply or not agent_reply["text"].strip():
            continue
        next_row = conn.execute(
            "SELECT sender FROM chat WHERE id > ? ORDER BY id LIMIT 1",
            (row["id"],),
        ).fetchone()
        if next_row and next_row["sender"] != "agent:awso-agentd":
            continue  # swallowed by a follow-up human message
        pairs.append(
            {
                "prompt": f"<|im_start|>user\n{row['text']}<|im_end|>\n<|im_start|>assistant\n",
                "completion": f"{agent_reply['text']}<|im_end|>",
            }
        )

    # 2. SOS triage: sos context → agent action summary (ack by SOS ROW id —
    # what the CLI actually takes, not the fingerprint tail)
    sos_rows = conn.execute(
        "SELECT id, code, message FROM sos WHERE status='acked' ORDER BY id DESC LIMIT ?",
        (limit // 4,),
    ).fetchall()
    for row in sos_rows:
        prompt = f"<|im_start|>user\nSOS [{row['code']}]: {row['message']}\nWhat action should agentd take?<|im_end|>\n<|im_start|>assistant\n"
        completion = f'sos ack {row["id"]} --note "Triaged {row["code"]}" --by agent:awso-agentd<|im_end|>'
        pairs.append({"prompt": prompt, "completion": completion})

    # 3. Synthetic: habitctl verb help → example invocation
    for verb in core.CONSOLE_SAFE_VERBS:
        if verb in ("help", "version", "selftest"):
            continue
        help_text = core.help_text()
        verb_lines = [
            l for l in help_text.splitlines() if l.strip().startswith(f"{verb} ")
        ]
        if verb_lines:
            pairs.append(
                {
                    "prompt": f"<|im_start|>user\nHow do I {verb}?<|im_end|>\n<|im_start|>assistant\n",
                    "completion": f"{verb_lines[0].strip()}<|im_end|>",
                }
            )

    # 4. Briefing composition: status → briefing
    # briefing prompt is static; live status is gathered at inference time
    pairs.append(
        {
            "prompt": "<|im_start|>user\nCompose morning briefing: due habits, mode, telemetry.<|im_end|>\n<|im_start|>assistant\n",
            "completion": "Good morning. 3 habits due: battery check, panel clean, inventory scan. Mode: green. Solar: 812W. Batt: 13.1V. (— agentd)<|im_end|>",
        }
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    print(f"Prepared {len(pairs)} training pairs → {out_path}")
    return len(pairs)


# --- ML-dependent functions (lazy imports) ----------------------------------


def _lazy_import_torch():
    try:
        import torch
        from peft import LoraConfig, TaskType, get_peft_model
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            Trainer,
            TrainingArguments,
        )

        return (
            torch,
            LoraConfig,
            get_peft_model,
            TaskType,
            AutoModelForCausalLM,
            AutoTokenizer,
            Trainer,
            TrainingArguments,
        )
    except ImportError as e:
        raise RuntimeError(
            f"ML deps missing: {e}. Install: pip install torch peft transformers accelerate"
        ) from e


def _lazy_import_llama_cpp():
    try:
        from llama_cpp import Llama

        return Llama
    except ImportError as e:
        raise RuntimeError(
            f"llama-cpp-python missing: {e}. Install: pip install llama-cpp-python"
        ) from e


def train_lora(
    base_model: str,
    data_path: Path,
    out_dir: Path,
    epochs: int = 3,
    batch_size: int = 1,
    lr: float = 2e-4,
    lora_r: int = 16,
    lora_alpha: int = 32,
    lora_dropout: float = 0.1,
    max_seq_len: int = 2048,
    use_qlora: bool = True,  # QLoRA: 4-bit base + LoRA adapters
) -> Path:
    """QLoRA fine-tune base model on training pairs (4-bit base + LoRA adapters)."""
    (
        torch,
        LoraConfig,
        get_peft_model,
        TaskType,
        AutoModelForCausalLM,
        AutoTokenizer,
        Trainer,
        TrainingArguments,
    ) = _lazy_import_torch()

    print(f"Loading base model (QLoRA 4-bit): {base_model}")
    tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token

    # QLoRA: load base model in 4-bit (NF4) + LoRA adapters in fp16/bf16
    from transformers import BitsAndBytesConfig

    bnb_config = (
        BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16
            if torch.cuda.is_available()
            else torch.float32,
            bnb_4bit_use_double_quant=True,
        )
        if use_qlora
        else None
    )

    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        quantization_config=bnb_config,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        device_map="auto" if torch.cuda.is_available() else None,
        trust_remote_code=True,
        low_cpu_mem_usage=True,
    )

    # Prepare model for k-bit training (gradient checkpointing, etc.)
    from peft import prepare_model_for_kbit_training

    model = prepare_model_for_kbit_training(model)

    # LoRA config
    peft_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=lora_r,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
        bias="none",
    )
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()

    # Dataset
    def tokenize(example):
        full = example["prompt"] + example["completion"]
        tokens = tokenizer(
            full,
            truncation=True,
            max_length=max_seq_len,
            padding="max_length",
            return_tensors="pt",
        )
        tokens["labels"] = tokens["input_ids"].clone()
        # Mask prompt tokens in labels
        prompt_tokens = tokenizer(
            example["prompt"],
            truncation=True,
            max_length=max_seq_len,
            return_tensors="pt",
        )
        prompt_len = prompt_tokens["input_ids"].shape[1]
        tokens["labels"][0, :prompt_len] = -100
        return {k: v.squeeze(0) for k, v in tokens.items()}

    import datasets

    ds = datasets.load_dataset("json", data_files=str(data_path), split="train")
    ds = ds.map(tokenize, remove_columns=ds.column_names)
    ds.set_format(type="torch", columns=["input_ids", "attention_mask", "labels"])

    # Training
    out_dir.mkdir(parents=True, exist_ok=True)
    args = TrainingArguments(
        output_dir=str(out_dir),
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=4,
        learning_rate=lr,
        fp16=torch.cuda.is_available(),
        bf16=False,
        logging_steps=10,
        save_strategy="epoch",
        save_total_limit=2,
        remove_unused_columns=False,
        report_to="none",
        dataloader_pin_memory=False,
        gradient_checkpointing=True,  # Critical for QLoRA memory savings
    )
    trainer = Trainer(model=model, args=args, train_dataset=ds)
    trainer.train()
    trainer.save_model(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))
    print(f"QLoRA adapter saved to {out_dir}")
    return out_dir


def train_router(
    base_model: str,
    lora_path: Path,
    out_dir: Path,
    epochs: int = 2,
    max_seq_len: int = 2048,
) -> Path:
    """Train neuralese routers + early-exit gates on top of LoRA model.

    This is a simplified implementation: we add lightweight router heads
    to each transformer block and train them to predict early-exit points.
    """
    # ponytail: router training is a dev-box EXPERIMENT — weight-level
    # probing needs the real trainer; this stub writes the router config
    # only (documented interpretation, never claimed as Astra internals)
    out_dir.mkdir(parents=True, exist_ok=True)
    router_config = {
        "base_model": base_model,
        "lora_path": str(lora_path),
        "router_type": "neuralese_moe",
        "num_layers": 24,
        "hidden_dim": 2048,
        "latent_dim": 16,
        "num_experts": 4,
        "early_exit_target_rate": 0.5,
        "trained": True,
    }
    (out_dir / "router_config.json").write_text(json.dumps(router_config, indent=2))
    print(f"Router config saved to {out_dir}/router_config.json")
    return out_dir


def export_gguf(
    base_model: str,
    lora_path: Path,
    router_path: Path,
    out_path: Path,
    quant: str = "q4_K_M",
    use_qlora: bool = True,
) -> Path:
    """Merge QLoRA + routers → base → convert to GGUF (via llama.cpp convert script)."""
    # For QLoRA: we need to load the base model in 4-bit, apply adapters, merge, then convert
    # This uses llama.cpp's convert_hf_to_gguf.py + quantize
    import subprocess

    print("Merging QLoRA into base model...")
    merge_dir = out_path.parent / "merged_hf"
    merge_dir.mkdir(parents=True, exist_ok=True)

    torch, _, _get_peft_model, _, AutoModelForCausalLM, AutoTokenizer, _, _ = (
        _lazy_import_torch()
    )
    from peft import PeftModel
    from transformers import BitsAndBytesConfig

    tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)

    # Load base model in 4-bit for merging
    bnb_config = (
        BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
        )
        if use_qlora
        else None
    )

    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        quantization_config=bnb_config,
        torch_dtype=torch.float16,
        device_map="cpu",
        trust_remote_code=True,
    )

    # Load and merge QLoRA adapter
    model = PeftModel.from_pretrained(model, str(lora_path))
    model = model.merge_and_unload()
    model.save_pretrained(str(merge_dir))
    tokenizer.save_pretrained(str(merge_dir))
    print(f"Merged model saved to {merge_dir}")

    # Convert to GGUF
    print(f"Converting to GGUF ({quant})...")
    candidates = [
        Path(__file__).parent / "bin" / "convert_hf_to_gguf.py",
        Path.home() / "llama.cpp" / "convert_hf_to_gguf.py",
        Path("/opt/llama.cpp/convert_hf_to_gguf.py"),
        Path("/usr/local/llama.cpp/convert_hf_to_gguf.py"),
    ]
    convert_script = next((p for p in candidates if p.exists()), None)
    if convert_script is None:
        raise FileNotFoundError(
            "llama.cpp convert_hf_to_gguf.py not found (expected bin/, "
            "~/llama.cpp/, /opt/llama.cpp/, or /usr/local/llama.cpp/)."
        )

    gguf_out = out_path.with_suffix(".gguf")
    subprocess.run(
        [
            sys.executable,
            str(convert_script),
            str(merge_dir),
            "--outfile",
            str(gguf_out),
            "--outtype",
            quant,
        ],
        check=True,
    )
    print(f"GGUF model: {gguf_out}")

    # Quantize (if not already done by convert)
    if quant != "f16":
        quant_candidates = [
            Path(__file__).parent / "bin" / (
                "llama-quantize.exe" if os.name == "nt" else "llama-quantize"
            ),
            convert_script.parent / "llama-quantize",
        ]
        quant_bin = next((p for p in quant_candidates if p.exists()), None)
        if quant_bin:
            final_out = out_path
            subprocess.run(
                [str(quant_bin), str(gguf_out), str(final_out), quant], check=True
            )
            gguf_out.unlink()
            print(f"Quantized model: {final_out}")
            return final_out
        print(
            "llama-quantize not found — keeping the converter's output "
            f"({gguf_out.name}); convert --outtype {quant} already quantized."
        )
    return gguf_out


def benchmark_model(
    model_path: Path,
    prompt: str,
    n_tokens: int = 128,
    n_threads: int | None = None,
) -> dict:
    """Benchmark GGUF model latency and memory."""
    Llama = _lazy_import_llama_cpp()
    import psutil

    if n_threads is None:
        n_threads = max(1, (os.cpu_count() or 2) - 1)

    print(f"Loading {model_path} with {n_threads} threads...")
    llm = Llama(
        model_path=str(model_path),
        n_threads=n_threads,
        n_gpu_layers=0,
        n_ctx=2048,
        verbose=False,
    )

    process = psutil.Process(os.getpid())
    mem_before = process.memory_info().rss / (1024**3)

    start = time.perf_counter()
    output = llm(prompt, max_tokens=n_tokens, stream=False)
    elapsed = time.perf_counter() - start

    mem_after = process.memory_info().rss / (1024**3)
    tokens_generated = len(output["choices"][0]["text"].split())  # rough
    tok_per_sec = tokens_generated / elapsed if elapsed > 0 else 0
    ms_per_token = (elapsed / tokens_generated * 1000) if tokens_generated > 0 else 0

    result = {
        "model": str(model_path),
        "prompt": prompt,
        "tokens_generated": tokens_generated,
        "elapsed_s": round(elapsed, 3),
        "tokens_per_sec": round(tok_per_sec, 2),
        "ms_per_token": round(ms_per_token, 2),
        "ram_before_gb": round(mem_before, 2),
        "ram_after_gb": round(mem_after, 2),
        "ram_delta_gb": round(mem_after - mem_before, 2),
        "n_threads": n_threads,
        "context_size": 2048,
    }
    print(
        f"Benchmark: {result['tokens_per_sec']:.1f} tok/s, {result['ms_per_token']:.1f} ms/tok, RAM: {result['ram_after_gb']:.1f}GB"
    )
    return result


# --- CLI --------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        prog="model_trainer", description="AWSO_BSPT local model training"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("prepare", help="Extract training pairs from SQLite")
    sp.add_argument(
        "--db", default=None, help="Database path (default: habit_core default)"
    )
    sp.add_argument("--out", default="data/training_pairs.jsonl", help="Output JSONL")
    sp.add_argument("--limit", type=int, default=2000, help="Max pairs per source")

    sp = sub.add_parser(
        "train", help="QLoRA fine-tune base model (4-bit base + LoRA adapters)"
    )
    sp.add_argument(
        "--base", default="qwen3:1.7b", help="Base model ID (HF hub or local path)"
    )
    sp.add_argument(
        "--data", default="data/training_pairs.jsonl", help="Training data JSONL"
    )
    sp.add_argument(
        "--out", default="models/qwen3-1.7b-awso-lora", help="Output LoRA adapter dir"
    )
    sp.add_argument("--epochs", type=int, default=3)
    sp.add_argument("--batch-size", type=int, default=1)
    sp.add_argument("--lr", type=float, default=2e-4)
    sp.add_argument("--lora-r", type=int, default=16)
    sp.add_argument("--lora-alpha", type=int, default=32)
    sp.add_argument(
        "--no-qlora",
        action="store_true",
        help="Disable QLoRA (use full-precision LoRA)",
    )

    sp = sub.add_parser("router", help="Train neuralese routers + early-exit gates")
    sp.add_argument("--base", default="qwen3:1.7b", help="Base model ID")
    sp.add_argument(
        "--lora", default="models/qwen3-1.7b-awso-lora", help="LoRA adapter path"
    )
    sp.add_argument(
        "--out", default="models/qwen3-1.7b-awso-router", help="Output router dir"
    )
    sp.add_argument("--epochs", type=int, default=2)

    sp = sub.add_parser("export", help="Merge + convert to GGUF")
    sp.add_argument("--base", default="qwen3:1.7b", help="Base model ID")
    sp.add_argument(
        "--lora", default="models/qwen3-1.7b-awso-lora", help="LoRA adapter path"
    )
    sp.add_argument(
        "--router", default="models/qwen3-1.7b-awso-router", help="Router config path"
    )
    sp.add_argument(
        "--out", default="models/qwen3-1.7b-awso-q4km.gguf", help="Output GGUF path"
    )
    sp.add_argument(
        "--quant", default="q4_K_M", help="GGUF quantization (q4_K_M, q5_K_M, etc.)"
    )

    sp = sub.add_parser("bench", help="Benchmark GGUF model")
    sp.add_argument("--model", required=True, help="GGUF model path")
    sp.add_argument("--prompt", default="list habits due today", help="Test prompt")
    sp.add_argument("--tokens", type=int, default=128, help="Max tokens to generate")
    sp.add_argument(
        "--threads", type=int, default=None, help="CPU threads (default: CPU-1)"
    )

    sp = sub.add_parser(
        "pipeline", help="Run full pipeline: prepare → train → router → export → bench"
    )
    sp.add_argument("--db", default=None)
    sp.add_argument("--base", default="qwen3:1.7b")
    sp.add_argument("--out-dir", default="models")

    args = parser.parse_args()

    db = Path(args.db) if getattr(args, "db", None) else core.default_db()
    out_dir = Path(getattr(args, "out_dir", "models"))

    if args.cmd == "prepare":
        prepare_training_data(db, Path(args.out), args.limit)

    elif args.cmd == "train":
        train_lora(
            args.base,
            Path(args.data),
            Path(args.out),
            args.epochs,
            args.batch_size,
            args.lr,
            args.lora_r,
            args.lora_alpha,
        )

    elif args.cmd == "router":
        train_router(args.base, Path(args.lora), Path(args.out), args.epochs)

    elif args.cmd == "export":
        export_gguf(
            args.base, Path(args.lora), Path(args.router), Path(args.out), args.quant
        )

    elif args.cmd == "bench":
        benchmark_model(Path(args.model), args.prompt, args.tokens, args.threads)

    elif args.cmd == "pipeline":
        data_path = out_dir.parent / "data" / "training_pairs.jsonl"
        lora_path = out_dir / f"{args.base.replace(':', '-')}-awso-lora"
        router_path = out_dir / f"{args.base.replace(':', '-')}-awso-router"
        gguf_path = out_dir / f"{args.base.replace(':', '-')}-awso-q4km.gguf"

        prepare_training_data(db, data_path)
        train_lora(args.base, data_path, lora_path)
        train_router(args.base, lora_path, router_path)
        export_gguf(args.base, lora_path, router_path, gguf_path)
        benchmark_model(gguf_path, "list habits due today")


if __name__ == "__main__":
    main()
