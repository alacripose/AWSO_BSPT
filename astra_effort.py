#!/usr/bin/env python3
"""Astra-style effort control for AWSO_BSPT — pure stdlib, no ML deps.

Implements the behavioral model of GPT-6 Astra reasoning effort as written
in OPENAI_OUTLINE.txt, adapted to a 4GB CPU solar/battery device:

  * Five effort levels (low..max) are a COMPUTE BUDGET over deliberation,
    not verbosity (outline §1, §22, §41).
  * Higher effort buys more iterations of the act -> observe -> evaluate
    -> verify/replan loop (outline §5, §42, §44) with a preference for
    executing over guessing (§28: don't reason about what you can run).
  * The budget lives in the HARNESS around a small model (outline §16/§34:
    harness depth multiplies model capability; §43: the weights' internals
    are not public — never claim them). Weight-level depth (MoD routing)
    is a separate dev-machine experiment in model_trainer.py.
  * Effort is chosen per duty (outline §25 task matrix) and can change per
    call (§21).

Loop (outline §42):  understand -> act (TOOL) -> observe -> evaluate ->
    success -> verify (ground-truth re-query) -> done
    failure -> replan -> more action -> ...

Everything here is deterministic and unit-testable without any model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# effort = compute budget. iterations = model calls in the loop (each may be
# followed by one tool execution); replans = corrective re-asks after a failed
# verification; verify = harness re-query of ground truth after the model
# claims success (higher effort spends compute on verification, not longer
# text).
EFFORT_BUDGETS = {
    "low": {"iterations": 1, "max_tokens": 96, "replans": 0, "verify": False},
    "medium": {"iterations": 2, "max_tokens": 160, "replans": 1, "verify": False},
    "high": {"iterations": 4, "max_tokens": 256, "replans": 2, "verify": True},
    "xhigh": {"iterations": 8, "max_tokens": 384, "replans": 4, "verify": True},
    "max": {"iterations": 16, "max_tokens": 512, "replans": 8, "verify": True},
}

DUTY_EFFORT = {
    "chat": "medium",
    "briefing": "medium",
    "telemetry": "high",
    "sos": "xhigh",
}

PROTOCOL = (
    "TOOL PROTOCOL: when you need facts or must act, reply with exactly "
    'one line of the form TOOL {{"verb": "<verb>", "argv": ["--flag", '
    '"value"]}} and nothing else; the harness runs it against habitctl and '
    "returns the output as the next observation. A reply with no TOOL line "
    "is your final answer for the operator. Allowed verbs: {verbs}."
)

MAX_OBS_CHARS = 1200  # per-observation cap fed back to the model
HISTORY_CHAR_BUDGET = 6000  # total transcript chars fed to the model

# Built by concatenation so nothing in this file ever contains the literal
# tag (Qwen3 think blocks) — a writer that strips angle-bracket
# tags must not be able to disable this regex (defense in depth; the
# regression tests would catch it, but this way the bug cannot reappear
# silently).
_T_OPEN = "<" + "think" + ">"
_T_CLOSE = "</" + "think" + ">"
_THINK_RE = re.compile(_T_OPEN + ".*?" + _T_CLOSE, re.DOTALL)

# Control markers that could forge a turn boundary in ANY renderer:
# ChatML (local models) and the plain-turn transcript (Hermes cloud
# fallback). Untrusted text — habit names, SOS messages, notes, chat —
# passes through sanitize() before entering a transcript (Mantis F-01).
_CHATML_MARKERS = (
    "<|im_start|>",
    "<|im_end|>",
    "HARNESS:",
    "ASSISTANT:",
    " RequestContext",  # legacy remnant kept: harmless to neutralize
)


def strip_think(text: str) -> str:
    """Remove Qwen3 think blocks a model may emit under raw completion."""
    return _THINK_RE.sub("", text or "").strip()


def sanitize(text: str) -> str:
    """Neutralize transcript-forging markers in untrusted text before it
    re-enters a model prompt (habitctl output, operator input, SOS text).
    A habit note containing '<|im_end|>' or 'ASSISTANT:' would otherwise
    forge a turn boundary in the ChatML or plain renderers."""
    for marker in _CHATML_MARKERS:
        if marker in text:
            text = text.replace(
                marker,
                marker.replace("<", "\u2039")
                .replace(">", "\u203a")
                .replace(":", "\u02d0"),
            )
    return text


def effort_budget(effort: str) -> dict:
    """Budget knobs for an effort level. Raises on unknown."""
    if effort not in EFFORT_LEVELS:
        raise ValueError(f"unknown effort {effort!r}; expected one of {EFFORT_LEVELS}")
    return dict(EFFORT_BUDGETS[effort])


def effort_rank(effort: str) -> int:
    """Ordinal of an effort level — used to cap cloud-model scaffolding."""
    return EFFORT_LEVELS.index(effort)


def parse_tool_call(text: str) -> dict | None:
    """Extract a TOOL call from a model reply.

    Think blocks are stripped first (Qwen3 raw-completion behavior). A
    reply is a tool call only if what remains starts with TOOL (the
    protocol contract: "exactly one line ... and nothing else"). Returns
    {"verb", "argv"} on success, {"verb": None} for a malformed TOOL line
    (the loop feeds the error back as an observation so the model can
    correct — outline §29 experiment loop), or None for a plain final
    answer.
    """
    stripped = strip_think(text)
    if not stripped.startswith("TOOL"):
        return None
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not m:
        return {"verb": None, "argv": None}
    try:
        call = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return {"verb": None, "argv": None}
    if not isinstance(call, dict):
        return {"verb": None, "argv": None}
    return {"verb": call.get("verb"), "argv": call.get("argv", [])}


class HabitctlTools:
    """Whitelisted habitctl executor for the TOOL protocol.

    Security (Mantis protocol 2 — input validation at the trust boundary):
    the model output is untrusted; verbs are checked against the console
    whitelist, argv is a bounded list of plain strings, and execution goes
    through core.cli's array-form parser — never a shell.
    """

    def __init__(
        self,
        run_verb: Callable[[str, list], str],
        allowed_verbs: tuple[str, ...] | list[str] | set[str],
    ):
        self.run_verb = run_verb
        self.allowed_verbs = set(allowed_verbs)

    def validate(self, call: dict) -> str | None:
        """Return an error string the model can correct from, or None."""
        if not isinstance(call, dict):
            return "tool call must be a JSON object"
        verb = call.get("verb")
        argv = call.get("argv", [])
        if verb is None:
            return (
                'could not parse the TOOL line; expected {"verb": "...", "argv": [...]}'
            )
        if verb not in self.allowed_verbs:
            return f"verb not allowed: {verb!r}"
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            return "argv must be a list of strings"
        if not 0 <= len(argv) <= 8:
            return "argv must have between 0 and 8 tokens"
        for a in argv:
            if len(a) > 200 or "\n" in a or "\r" in a:
                return f"bad argument: {a!r}"
        return None

    def run(self, verb: str, argv: list) -> str:
        return self.run_verb(verb, list(argv))


def _prune_history(history: list, budget: int = HISTORY_CHAR_BUDGET) -> None:
    """Keep the transcript inside the ctx-2048 budget (cheap compression:
    trim old observations, never the operator's ask). In-place."""
    total = sum(len(t["content"]) for t in history)
    i = 0
    while total > budget and i < len(history):
        t = history[i]
        if (
            t["role"] == "user"
            and t["content"].startswith("[observation]")
            and len(t["content"]) > 250
        ):
            total -= len(t["content"]) - 250
            t["content"] = t["content"][:250] + " …[pruned]"
        i += 1


def run_depth_loop(
    model_call: Callable[[str, list, int], str],
    system: str,
    tools: HabitctlTools,
    effort: str = "medium",
    verify: Callable[[], tuple[bool, str]] | None = None,
    history: list | None = None,
    on_step: Callable[[dict], None] | None = None,
) -> tuple[str, list[dict]]:
    """Effort-budgeted act->observe->evaluate->verify/replan loop.

    model_call(system, history, max_tokens) -> reply text. Implementations
    render the transcript however their model needs (llama-server HTTP and
    llama-cpp bindings use ChatML via render_chatml; the Hermes cloud
    fallback uses render_plain) — the loop is agnostic.

    on_step, when given, receives each trace event as it happens (live
    agent status for the kiosk GUI, R5). Best-effort: a raising on_step
    never kills the duty.

    Returns (final_answer, trace); trace is the outline §42 step record
    (act/observe/verify/replan/final/exhausted) for events + tests.
    """
    b = effort_budget(effort)
    hist = list(history or [])
    system_full = f"{system}\n\n" + PROTOCOL.format(
        verbs=", ".join(sorted(tools.allowed_verbs))
    )
    trace: list[dict] = []

    def emit(ev: dict) -> None:
        trace.append(ev)
        if on_step:
            try:
                on_step(ev)
            except Exception:  # noqa: S110, BLE001 — UI status is best-effort
                pass

    calls = 0
    replans_used = 0
    answer: str | None = None

    while calls < b["iterations"]:
        reply = model_call(system_full, hist, b["max_tokens"])
        calls += 1
        call = parse_tool_call(reply)

        if call is None:
            # evaluate: plain reply = the model's final answer
            answer = strip_think(reply)
            emit({"type": "final", "call": calls, "text": answer[:200]})
            if b["verify"] and verify is not None:
                ok, detail = verify()
                emit(
                    {"type": "verify", "call": calls, "ok": ok, "detail": detail[:200]}
                )
                if not ok and replans_used < b["replans"]:
                    # outline §42: failure -> replan -> more action
                    replans_used += 1
                    emit({"type": "replan", "round": replans_used})
                    hist.append({"role": "assistant", "content": answer})
                    hist.append(
                        {
                            "role": "user",
                            "content": (
                                "Verification FAILED: "
                                f"{sanitize(detail)}. Your claim did not hold "
                                "against the database. Re-examine, act with a "
                                "TOOL line if needed, then give a corrected "
                                "final answer."
                            ),
                        }
                    )
                    answer = None
                    continue
            break  # verified ok, or no verification budget left

        # act + observe (outline §29: don't reason about what you can run)
        err = tools.validate(call)
        if err:
            obs = f"error: {err}"
        else:
            try:
                obs = tools.run(call["verb"], call["argv"])
            except Exception as e:  # noqa: BLE001 — model output is untrusted
                obs = f"error: tool execution failed: {type(e).__name__}: {e}"
        emit(
            {
                "type": "act",
                "call": calls,
                "verb": call.get("verb"),
                "argv": call.get("argv"),
                "ok": not obs.startswith("error"),
            }
        )
        obs = sanitize(obs)[:MAX_OBS_CHARS]
        hist.append({"role": "assistant", "content": strip_think(reply)})
        hist.append({"role": "user", "content": f"[observation]\n{obs}"})
        _prune_history(hist)
        emit({"type": "observe", "call": calls, "chars": len(obs)})

    if answer is None:
        answer = (
            "(agentd: effort budget exhausted before a final answer — "
            f"last action: {trace[-1].get('verb', 'none') if trace else 'none'})"
        )
        emit({"type": "exhausted", "calls": calls})
    return answer, trace


# --- transcript renderers (single source of truth; model_trainer trains
# --- in exactly this format so runtime and training never drift) --------


def render_chatml(system: str, history: list) -> str:
    """ChatML document for the local GGUF model (qwen3 tokenizer)."""
    out = [f"<|im_start|>system\n{system}<|im_end|>"]
    for turn in history:
        out.append(f"<|im_start|>{turn['role']}\n{turn['content']}<|im_end|>")
    out.append("<|im_start|>assistant\n")
    return "\n".join(out)


def render_plain(system: str, history: list) -> str:
    """Plain-turn transcript for capable cloud models (Hermes fallback)."""
    names = {"user": "HARNESS", "assistant": "ASSISTANT"}
    parts = [system]
    for turn in history:
        parts.append(f"{names[turn['role']]}: {turn['content']}")
    parts.append("ASSISTANT:")
    return "\n\n".join(parts)
