#!/usr/bin/env python3
"""AWSO_BSPT local model backends — llama-server (device) or bindings (dev).

The device is an OFFLINE Linux box: no pip, no compiler, no network. Its
backend is llama.cpp's prebuilt llama-server binary (deploy/offline bundle),
probed and driven over localhost HTTP with stdlib urllib. The dev box may
instead use llama-cpp-python in-process (.venv-ml). Both expose the same
chat_call adapter (the astra_effort model_call interface), so agentd's
effort loop never knows which one answered.

create_runner() chain: $AWSO_LLAMA_SERVER -> http://127.0.0.1:8010
-> llama-cpp bindings + local GGUF (dev) -> None. None means agentd falls
back to the Hermes cloud one-shot, then to rule-based replies — a duty is
never blocked by a missing model.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import astra_effort as ae

DEFAULT_SERVER_URL = "http://127.0.0.1:8010"

# Device bundle installs the GGUF under /opt/awso/models (root-owned);
# upgrades replace the model file, never operator data in the DB.
MODEL_DIRS = (
    Path("/opt/awso/models"),
    Path.home() / ".awso" / "models",
    Path(__file__).parent / "models",
)

_llama_cpp = None


def _get_llama_cpp():
    """Importable llama-cpp-python? Dev box only; the device never has it."""
    global _llama_cpp
    if _llama_cpp is None:
        try:
            from llama_cpp import Llama

            _llama_cpp = Llama
        except ImportError:
            _llama_cpp = False
    return _llama_cpp


def find_local_model() -> Path | None:
    """First *.gguf in the known model dirs (any trained model name)."""
    for d in MODEL_DIRS:
        try:
            hits = sorted(d.glob("*.gguf"))
        except OSError:
            hits = []
        if hits:
            return hits[0]
    return None


class LlamaServerRunner:
    """Client for llama-server (localhost HTTP, stdlib only).

    The server owns the model + memory; this class is a pure client, so
    'load' is a health probe and memory limits live in the systemd unit
    (MemoryMax), not in Python. Raw /completion gives exact prompt-format
    control: the ChatML document astra_effort renders is the SAME string
    the model was fine-tuned on — no server-side template drift.
    """

    def __init__(
        self,
        url: str = DEFAULT_SERVER_URL,
        timeout_s: float = 120.0,
        health_timeout_s: float = 2.0,
    ):
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.health_timeout_s = health_timeout_s
        self._last_error: str | None = None

    def is_ready(self) -> bool:
        try:
            with urllib.request.urlopen(
                self.url + "/health", timeout=self.health_timeout_s
            ) as r:
                return r.status == 200
        except Exception as e:  # noqa: BLE001 — any transport error = down
            self._last_error = str(e)
            return False

    # agentd's duck-typed probe
    def load(self) -> bool:
        return self.is_ready()

    def chat_call(
        self, system: str, history: list, max_tokens: int, temperature: float = 0.2
    ) -> str:
        """astra_effort model_call adapter: transcript -> reply text."""
        doc = ae.render_chatml(system, history)
        payload = json.dumps(
            {
                "prompt": doc,
                "n_predict": max_tokens,
                "temperature": temperature,
                "top_p": 0.9,
                "stop": ["<|im_end|>", "<|im_start|>"],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url + "/completion",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            out = json.load(r)
        return ae.strip_think(out.get("content", ""))


class LocalModelRunner:
    """In-process GGUF via llama-cpp-python (dev-box convenience path)."""

    def __init__(
        self,
        model_path: Path,
        n_threads: int | None = None,
        n_ctx: int = 2048,
        n_gpu_layers: int = 0,
        verbose: bool = False,
    ):
        self.model_path = Path(model_path)
        self.n_threads = n_threads or max(1, (os.cpu_count() or 2) - 1)
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.verbose = verbose
        self._llm = None
        self._loaded = False
        self._load_error: str | None = None

    def load(self) -> bool:
        if self._loaded:
            return True
        if self._load_error:
            return False
        Llama = _get_llama_cpp()
        if Llama is False:
            self._load_error = "llama-cpp-python not installed"
            return False
        if not self.model_path.exists():
            self._load_error = f"Model not found: {self.model_path}"
            return False
        try:
            self._llm = Llama(
                model_path=str(self.model_path),
                n_threads=self.n_threads,
                n_gpu_layers=self.n_gpu_layers,
                n_ctx=self.n_ctx,
                verbose=self.verbose,
                use_mmap=True,  # memory-map weights (critical on 4GB)
                use_mlock=False,  # swap OK on a solar device
            )
            self._loaded = True
            return True
        except Exception as e:  # noqa: BLE001 — surface as fallback, not crash
            self._load_error = f"Load failed: {e}"
            return False

    def is_ready(self) -> bool:
        return self._loaded and self._llm is not None

    def chat_call(
        self, system: str, history: list, max_tokens: int, temperature: float = 0.2
    ) -> str:
        """astra_effort model_call adapter (same protocol as the server)."""
        if not self.load():
            raise RuntimeError(f"Model not loaded: {self._load_error}")
        doc = ae.render_chatml(system, history)
        out = self._llm(
            doc,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=0.9,
            stop=["<|im_end|>", "<|im_start|>"],
            stream=False,
        )
        return ae.strip_think(out["choices"][0]["text"])

    def unload(self):
        self._llm = None
        self._loaded = False
        import gc

        gc.collect()


def create_runner(model_path: Path | None = None):
    """Pick the best available backend; None = use Hermes fallback."""
    urls = []
    env_url = os.environ.get("AWSO_LLAMA_SERVER")
    if env_url:
        urls.append(env_url)
    urls.append(DEFAULT_SERVER_URL)
    for url in urls:
        runner = LlamaServerRunner(url=url)
        if runner.is_ready():
            return runner
    Llama = _get_llama_cpp()
    path = model_path or find_local_model()
    if Llama and path:
        runner = LocalModelRunner(path)
        if runner.load():
            return runner
    return None


# --- CLI (dev-box sanity checks) -------------------------------------------


def main():
    import argparse

    ap = argparse.ArgumentParser(
        prog="local_runner", description="AWSO_BSPT local model inference"
    )
    ap.add_argument(
        "--server",
        default=None,
        help=f"llama-server URL (default $AWSO_LLAMA_SERVER or {DEFAULT_SERVER_URL})",
    )
    ap.add_argument(
        "--model", default=None, help="GGUF path for the in-process (dev) backend"
    )
    ap.add_argument(
        "--chat", default=None, help="One-shot chat prompt (interactive if omitted)"
    )
    args = ap.parse_args()

    runner = LlamaServerRunner(
        url=args.server or os.environ.get("AWSO_LLAMA_SERVER") or DEFAULT_SERVER_URL
    )
    if runner.is_ready():
        print(f"backend: llama-server at {runner.url}")
    else:
        path = Path(args.model) if args.model else find_local_model()
        if path and _get_llama_cpp():
            runner = LocalModelRunner(path)
            if not runner.load():
                print(
                    f"No backend: server down, local load failed: {runner._load_error}"
                )
                return 1
            print(f"backend: llama-cpp bindings, model {path}")
        else:
            print(
                f"No backend: no llama-server at {runner.url}, "
                "no llama-cpp-python + local GGUF"
            )
            return 1

    sys_prompt = "You are a helpful assistant. Be terse."
    if args.chat is not None:
        prompt = args.chat or input("Prompt: ")
        t0 = time.perf_counter()
        out = runner.chat_call(sys_prompt, [{"role": "user", "content": prompt}], 256)
        print(f"\n({time.perf_counter() - t0:.1f}s)\n{out}")
    else:
        print("Interactive chat (Ctrl+C to exit)")
        while True:
            try:
                prompt = input("\n> ")
            except (KeyboardInterrupt, EOFError):
                break
            if not prompt.strip():
                continue
            t0 = time.perf_counter()
            print(
                runner.chat_call(sys_prompt, [{"role": "user", "content": prompt}], 256)
                + f"  [{time.perf_counter() - t0:.1f}s]"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
