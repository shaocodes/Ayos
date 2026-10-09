"""Measure how well a local model diagnoses faults, on the simulated PC.

    python -m ayos.evalsim                      use the best installed model
    python -m ayos.evalsim --model gemma3:1b    test a specific model
    python -m ayos.evalsim --rules              the built-in rules alone, for comparison

Every number printed here is measured on this computer in this run. Nothing is estimated.
Two scores matter:
  model alone        - how often the model's own first conclusion was the right cause
  with safety check  - how often the final diagnosis was right after plain code checked the model
The gap between the two is what the safety check is for.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time

from . import sim
from .agent import Session
from .brain import DEFAULT_MODEL, DEFAULT_URL, LocalModelBrain, RuleBrain
from .memory import Memory
from .server import NOT_CHAT, PREFERRED
from .system import FakeSystem
from .tools import snapshot

HEALTHY = [
    ("healthy_network", "My internet is not working", "no_fault_found"),
    ("healthy_pc", "My laptop feels slow", "pc_looks_healthy"),
]


# Same faults, said the way people actually say them. "#n" only keeps the case names apart.
EXTRA = [
    ("wrong_dns#taglish", "Ayaw mag-load ng mga website pero connected naman yung wifi ko", "dns_misconfigured"),
    ("proxy_on#taglish", "Di ko mabuksan kahit anong site sa Chrome, pero gumagana naman yung ibang apps", "proxy_blocking"),
    ("hosts_block#vague", "example.com just shows an error page, everything else is fine", "hosts_block"),
    ("adapter_off#short", "wifi gone", "adapter_disabled"),
    ("disk_full#taglish", "Sobrang bagal na ng laptop ko, ano kaya problema", "disk_full"),
    ("healthy_network#site", "I think my internet is broken, can you check", "no_fault_found"),
]


def cases() -> list:
    out = [(fid, q, cause) for fid, (q, cause) in sim.REAL_FAULT_CASES.items()]
    out += [(fid, q, cause) for fid, (_t, _d, _fn, q, cause) in sim.SIM_ONLY.items()]
    return out + HEALTHY


def run_case(brain, fault: str, question: str, expected: str, with_memory: bool) -> dict:
    pc = FakeSystem()
    mem = Memory(None)
    if with_memory:
        mem.set_baseline(snapshot(pc))
    if not fault.startswith("healthy"):
        sim.apply(pc, fault)
    s = Session("eval", question, pc, brain, mem, auto_approve=True)
    t0 = time.time()
    s.run()
    fix = [e for e in s.events if e["type"] == "fix_result"]
    trace = []
    for e in s.events:
        if e["type"] == "check_start":
            trace.append(f"{e['source']:6s} -> {e['name']}" + (f"   why: {e['thought']}" if e.get("thought") else ""))
        elif e["type"] == "check_result":
            trace.append(f"          = {e['summary']}")
        elif e["type"] == "guard":
            trace.append(f"REFUSED   {e['message']}" + (f"   why: {e['thought']}" if e.get("thought") else ""))
        elif e["type"] == "note":
            trace.append(f"note      {e['message']}")
        elif e["type"] == "diagnosis":
            trace.append(f"{e['source']:6s} => {e['cause']}   says: {e['message'] or '(catalogue text)'}")
        elif e["type"] in ("answer", "inconclusive"):
            trace.append(f"{e['type']}: {e['message']}")
    return {
        "trace": trace,
        "model_calls": s.steps,
        "case": fault,
        "question": question,
        "expected": expected,
        "final": s.cause,
        "final_correct": s.cause == expected,
        "model_first": s.first_conclusion,
        "model_first_correct": s.first_conclusion == expected,
        "refused": s.stats["refusals"],
        "checks": len(s.obs),
        "model_steps": s.stats["model_steps"],
        "rule_steps": s.stats["rule_steps"],
        "seconds": round(time.time() - t0, 1),
        "model_seconds": round(s.stats["model_seconds"], 1),
        "tok_per_s": s.stats["tok_per_s"],
        "fix_verified": bool(fix and fix[-1].get("verified")),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ayos.evalsim", description="Measure a local model on simulated PC faults.")
    p.add_argument("--model", default="auto")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--api", choices=["ollama", "openai"], default="ollama")
    p.add_argument("--rules", action="store_true", help="test the built-in rules instead of a model")
    p.add_argument("--no-memory", action="store_true", help="run without the 'what changed since it last worked' memory")
    p.add_argument("--out", default="eval_results.json")
    p.add_argument("--trace", action="store_true", help="also print every step of every case")
    p.add_argument("--extra", action="store_true", help="add phrasing cases: Taglish complaints and a general question")
    args = p.parse_args(argv)

    if args.rules:
        brain = RuleBrain()
    else:
        brain = LocalModelBrain(DEFAULT_MODEL if args.model == "auto" else args.model, args.url, args.api)
        models = brain.list_models()
        chat = [m for m in models if not any(x in m.lower() for x in NOT_CHAT)]
        if not chat:
            print(f"No local model found at {args.url}. Start Ollama and run:  ollama pull gemma3:4b")
            return 1
        if args.model == "auto" and brain.model not in chat:
            brain.model = next((m for m in PREFERRED if m in chat), chat[0])
        print(f"Model: {brain.model}   loading...", flush=True)
        warm = brain.warm_up()
        if not warm["ok"]:
            print(f"Could not load the model: {warm.get('error')}")
            return 1
        print(f"Loaded in {warm['seconds']} s.")

    with_memory = not args.no_memory
    rows = []
    print(f"\n{'case':18s} {'expected':20s} {'model said first':20s} {'final':20s} {'ok':3s} {'checks':>6s} {'secs':>6s}")
    todo = cases() + (EXTRA if args.extra else [])
    for fault, question, expected in todo:
        r = run_case(brain, fault.split("#")[0], question, expected, with_memory)
        r["case"] = fault
        rows.append(r)
        print(
            f"{r['case']:18s} {r['expected']:20s} {str(r['model_first'] or '-'):20s} {str(r['final']):20s} "
            f"{'yes' if r['final_correct'] else 'NO':3s} {r['checks']:6d} {r['seconds']:6.1f}",
            flush=True,
        )

    if args.trace:
        for r in rows:
            print(f"\n=== {r['case']}: \"{r['question']}\"  expected {r['expected']}, got {r['final']}")
            for line in r["trace"]:
                print("   " + line)
            for c in r.get("model_calls") or []:
                print(f"   call: {c['action']:22s} {c['seconds']:6.1f} s total | read {c.get('prompt_tokens')} tokens in {c.get('prompt_seconds')} s | wrote {c.get('tokens')} tokens in {c.get('gen_seconds')} s")
    n = len(rows)
    final_ok = sum(r["final_correct"] for r in rows)
    first_ok = sum(r["model_first_correct"] for r in rows)
    concluded = sum(1 for r in rows if r["model_first"])
    secs = [r["seconds"] for r in rows]
    speeds = [r["tok_per_s"] for r in rows if r["tok_per_s"]]
    summary = {
        "brain": brain.label,
        "computer": f"{platform.processor() or platform.machine()}, {platform.system()} {platform.release()}",
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "with_memory": with_memory,
        "cases": n,
        "final_correct": final_ok,
        "model_first_correct": first_ok,
        "model_reached_a_conclusion": concluded,
        "conclusions_refused_by_safety_check": sum(r["refused"] for r in rows),
        "median_seconds": round(statistics.median(secs), 1),
        "slowest_seconds": max(secs),
        "tokens_per_second": round(statistics.median(speeds), 1) if speeds else None,
    }
    print("\n" + "-" * 60)
    print(f"Brain:                {summary['brain']}")
    print(f"Computer:             {summary['computer']}")
    if not args.rules:
        print(f"Model alone:          {first_ok}/{n} first conclusions were the right cause")
    print(f"With safety check:    {final_ok}/{n} final diagnoses were right")
    print(f"Conclusions refused:  {summary['conclusions_refused_by_safety_check']}")
    print(f"Time per diagnosis:   median {summary['median_seconds']} s, slowest {summary['slowest_seconds']} s")
    if speeds:
        print(f"Model speed:          {summary['tokens_per_second']} tokens a second")
    print("Note: simulated checks answer instantly. On a real PC add about 1 to 4 seconds per check.")
    try:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "rows": rows}, f, indent=2)
        print(f"Saved to {os.path.abspath(args.out)}")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
