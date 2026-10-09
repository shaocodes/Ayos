"""Run every read-only check once and print what came back.

This changes nothing. It is the fastest way to see whether Ayos can read this PC correctly
and how fast the local model answers. The output is also saved to ayos_selftest.txt.
"""
from __future__ import annotations

import os
import platform
import sys
import time

from .brain import DEFAULT_MODEL, LocalModelBrain
from .server import NOT_CHAT, PREFERRED
from .tools import CHECKS, primary_adapter, run_check, snapshot


def run_selftest(system, model: str, url: str, api: str) -> int:
    lines = []

    def say(text=""):
        print(text)
        lines.append(text)

    say("Ayos self-test  (read-only, nothing is changed)")
    say(f"Python {platform.python_version()} on {platform.platform()}")
    say(f"PC: {system.name}   Administrator: {'yes' if system.is_admin() else 'no'}")
    if hasattr(system, "warm"):
        t0 = time.monotonic()
        system.warm()
        say(f"PowerShell: {system.ps_mode()} (start-up took {time.monotonic() - t0:.1f} s; the checks below reuse it)")
        system._changed()
    try:
        say(f"Main adapter: {primary_adapter(system)}")
    except Exception as e:
        say(f"Main adapter: FAILED {type(e).__name__}: {e}")
    say("")
    failed = 0
    ctx = {"target": None, "memory": {"baseline": None}}
    for name in CHECKS:
        t0 = time.monotonic()
        summary, data = run_check(name, system, ctx)
        ms = int((time.monotonic() - t0) * 1000)
        bad = "error" in data
        failed += bad
        say(f"[{'FAIL' if bad else ' ok '}] {name:22s} {ms:6d} ms  {summary}")
        if bad:
            say(f"        error: {data['error']}")
    say("")
    try:
        say(f"Snapshot: {snapshot(system)}")
    except Exception as e:
        say(f"Snapshot: FAILED {type(e).__name__}: {e}")
    say("")

    brain = LocalModelBrain(DEFAULT_MODEL if model == "auto" else model, url, api)
    models = brain.list_models()
    if not models:
        say(f"Model server at {url}: NOT reachable, or no models installed.")
        say("  Install Ollama from https://ollama.com, then run:  ollama pull gemma3:4b")
    else:
        chat = [m for m in models if not any(x in m.lower() for x in NOT_CHAT)]
        say(f"Model server at {url}: running. Installed models: {', '.join(models)}")
        if model == "auto" and brain.model not in chat and chat:
            brain.model = next((p for p in PREFERRED if p in chat), chat[0])
        say(f"Testing model: {brain.model}")
        warm = brain.warm_up()
        say(f"  load + read instructions: {warm['seconds']} s  {'ok' if warm['ok'] else 'FAILED: ' + str(warm.get('error'))}")
        if warm["ok"]:
            convo = brain.start("My internet is not working", ["There is no record yet of this PC's normal network settings."])
            view = {"question": "My internet is not working", "obs": {}, "route": "network", "has_baseline": False, "prefer": [], "allowed": list(CHECKS)}
            d = brain.step(convo, view, on_text=lambda text: None)  # streamed, the same way the app asks
            say(f"  first decision: {d['seconds']} s, source={d['source']}, action={d['action']}, speed={d.get('tok_per_s')} tokens/s")
            say(f"    read {d.get('prompt_tokens')} tokens in {d.get('prompt_seconds')} s, wrote {d.get('tokens')} tokens in {d.get('gen_seconds')} s")
            say(f"  thought: {d.get('thought')}")
            if d.get("model_error"):
                say(f"  model error: {d['model_error']}")
                failed += 1
            brain.observe(convo, "Result of " + d["action"] + ": (self-test, no real result)")
            d2 = brain.step(convo, {**view, "allowed": [c for c in CHECKS if c != d["action"]]}, on_text=lambda text: None)
            say(f"  second decision: {d2['seconds']} s, action={d2['action']}, speed={d2.get('tok_per_s')} tokens/s")
            say(f"    read {d2.get('prompt_tokens')} tokens in {d2.get('prompt_seconds')} s, wrote {d2.get('tokens')} tokens in {d2.get('gen_seconds')} s")
            say("  A real diagnosis usually needs 2 or 3 decisions like these, plus one longer one for the conclusion.")
        else:
            failed += 1
    say("")
    say("RESULT: " + ("all checks ran." if not failed else f"{failed} item(s) failed. Send this output back so it can be fixed."))
    try:
        out = os.path.join(os.getcwd(), "ayos_selftest.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"Saved to {out}")
    except OSError:
        pass
    return 1 if failed else 0


if __name__ == "__main__":
    from .system import WindowsSystem

    sys.exit(run_selftest(WindowsSystem(), "auto", "http://127.0.0.1:11434", "ollama"))
