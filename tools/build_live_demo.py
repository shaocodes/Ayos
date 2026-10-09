"""Build the web page that replays recorded Ayos sessions (docs/index.html).

Ayos is a local Windows program, so it cannot be "hosted". What can be put online is an honest
replay: real sessions, recorded event by event on the simulated PC, played back in the real
interface. Nothing on the page is invented; it shows what the recorded run did, including the
times it took.

    python tools/build_live_demo.py record [--model gemma3:4b | --rules] [--out docs/recording.json]
    python tools/build_live_demo.py build  [--recording docs/recording.json] [--out docs/index.html]
"""
import argparse
import json
import os
import platform
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ayos import sim  # noqa: E402
from ayos.agent import Session  # noqa: E402
from ayos.brain import DEFAULT_URL, LocalModelBrain, RuleBrain  # noqa: E402
from ayos.memory import Memory  # noqa: E402
from ayos.system import FakeSystem  # noqa: E402
from ayos.tools import snapshot  # noqa: E402

# (id, fault or None, what the owner says)
SCENARIOS = [
    ("wrong_dns", "wrong_dns", "Ayaw mag-load ng mga website pero connected naman yung wifi ko"),
    ("adapter_off", "adapter_off", "I have no internet at all"),
    ("proxy_on", "proxy_on", "My browser cannot open any website"),
    ("hosts_block", "hosts_block", "I cannot open example.com but other sites work"),
    ("isp_down", "isp_down", "The wifi is connected but there is no internet"),
    ("disk_full", "disk_full", "Sobrang bagal na ng laptop ko"),
    ("healthy", None, "I think my internet is broken, can you check"),
    ("question", None, "What is DNS?"),
]


def record(args) -> int:
    if args.rules:
        brain = RuleBrain()
    else:
        brain = LocalModelBrain(args.model, args.url)
        if not brain.available():
            print(f"Model {args.model} is not available at {args.url}.")
            return 1
        print("loading the model...", brain.warm_up(), flush=True)
    out = {
        "brain": brain.label,
        "model": getattr(brain, "model", "") or "",
        "uses_model": brain.kind == "model",
        "machine": f"{platform.processor() or platform.machine()}, {os.cpu_count()} CPU threads, no GPU, {platform.system()}",
        "recorded": time.strftime("%Y-%m-%d"),
        "scenarios": [],
    }
    for sid, fault, question in SCENARIOS:
        pc = FakeSystem()
        pc.pace = 0.6
        mem = Memory(None)
        mem.set_baseline(snapshot(pc))
        if fault:
            sim.apply(pc, fault)
        s = Session(sid, question, pc, brain, mem, auto_approve=True)
        s.run()
        title = desc = None
        for f in sim.fault_list(True):
            if f["id"] == fault:
                title, desc = f["title"], f["desc"]
        out["scenarios"].append({"id": sid, "fault": fault, "title": title, "desc": desc, "question": question, "cause": s.cause, "events": s.events})
        print(f"{sid:12s} -> {s.cause}  ({len(s.events)} events, {s.events[-1]['t']} s)", flush=True)
        if brain.kind == "model" and brain.last_speed:
            out["speed"] = brain.last_speed
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print("saved", args.out)
    return 0


REPLAY_JS = r"""
/* ---- Replay of recorded sessions. Replaces the calls to the local Ayos server. ---- */
(function () {
  const REC = window.AYOS_RECORDING;
  const byId = {}; for (const s of REC.scenarios) byId[s.id] = s;
  const st = { fault: null, session: null, memory: { has_baseline: true, baseline_saved_at: REC.recorded, incidents: 0, recent: [] } };
  const MAX_GAP = 2.2;   // seconds; longer recorded waits are shortened on this page
  function schedule(events) {            // recorded times -> shortened playback times
    let last = 0, shown = 0; const out = [];
    for (const e of events) { const gap = Math.max(0, e.t - last); shown += Math.min(gap, MAX_GAP); last = e.t; out.push({ at: shown, ev: e }); }
    return out;
  }
  function internetNow() { return !(st.fault && ["wrong_dns", "adapter_off", "proxy_on", "isp_down"].includes(st.fault)); }
  function status() {
    const faults = REC.scenarios.filter((s) => s.fault).map((s) => ({ id: s.fault, title: s.title, desc: s.desc, needs_admin: false, real: true, say: s.question }));
    return {
      version: "replay", simulated: true, system_name: "Simulated PC", admin: true, internet: internetNow(),
      model: { name: REC.model || "built-in rules", api: "ollama", url: "http://127.0.0.1:11434", server_up: REC.uses_model, installed: REC.uses_model, models: REC.uses_model ? [REC.model] : [],
               warm: "yes", warm_seconds: null, warm_error: null, speed: REC.speed || null, using_model: REC.uses_model, forced_rules: false },
      memory: st.memory, faults, session: null,
    };
  }
  function pick(question) {
    const exact = REC.scenarios.find((s) => !s.fault && s.question === question);
    if (exact) return exact;
    if (st.fault && byId[st.fault]) return byId[st.fault];
    return byId.healthy;
  }
  window.AYOS_REPLAY = async function (path, body) {
    if (path === "/api/status") return status();
    if (path === "/api/break") { st.fault = body.fault; const s = byId[body.fault]; return { ok: true, message: "Simulated: " + s.title.toLowerCase() + ". " + s.desc }; }
    if (path === "/api/restore") { st.fault = null; return { ok: true, done: ["Simulated PC put back to healthy."] }; }
    if (path === "/api/memory/clear") { st.memory.recent = []; st.memory.incidents = 0; return { ok: true }; }
    if (path === "/api/model") return { ok: true };
    if (path === "/api/ask") {
      const sc = pick(body.question);
      const cut = sc.events.findIndex((e) => e.type === "fix_start");
      const before = cut < 0 ? sc.events : sc.events.slice(0, cut), after = cut < 0 ? [] : sc.events.slice(cut);
      st.session = { sc, started: performance.now(), plan: schedule(before), after, extra: [], state: "running", n: before.length, afterStart: null };
      return { id: "replay-" + sc.id };
    }
    const s = st.session;
    if (path.startsWith("/api/events")) {
      const after = parseInt(new URLSearchParams(path.split("?")[1]).get("after") || "0", 10);
      const now = (performance.now() - s.started) / 1000;
      let ready = s.plan.filter((p) => p.at <= now).map((p) => p.ev);
      if (ready.length === s.plan.length && s.state === "running") s.state = s.after.length ? "awaiting" : "done";
      if (s.afterStart !== null) {
        const t = (performance.now() - s.afterStart) / 1000;
        const more = s.afterPlan.filter((p) => p.at <= t).map((p) => p.ev);
        ready = ready.concat(more);
        if (more.length === s.afterPlan.length && s.state === "fixing") {
          const fr = s.after.find((e) => e.type === "fix_result") || {};
          s.state = fr.can_undo ? "fixed" : "done"; if (fr.verified) st.fault = null;
          st.memory.recent.unshift({ title: s.sc.events.find((e) => e.type === "diagnosis").title, fixed: true, when: "just now", seconds: s.sc.events.find((e) => e.type === "summary").seconds });
          st.memory.incidents += 1;
        }
      }
      ready = ready.concat(s.extra).map((e, i) => Object.assign({}, e, { i }));
      return { events: ready.slice(after), state: s.state, pending: 0 };
    }
    if (path === "/api/approve") {
      const base = s.after.length ? s.after[0].t : 0;
      s.afterPlan = schedule(s.after.map((e) => Object.assign({}, e, { t: e.t - base })));
      s.afterStart = performance.now(); s.state = "fixing"; return { ok: true };
    }
    if (path === "/api/decline") { s.extra.push({ type: "declined", message: "No change was made. You can ask again any time." }, { type: "done" }); s.state = "done"; return { ok: true }; }
    if (path === "/api/undo") { s.extra.push({ type: "undo_result", ok: true, message: "Undone. The setting is back to what it was before the fix." }); s.state = "done"; st.fault = s.sc.fault; return { ok: true }; }
    if (path === "/api/followup") {
      s.extra.push({ type: "followup_q", message: body.question }, { type: "followup_a", ok: true, message: "This page replays recorded sessions, so it cannot answer new questions. In the real app, the model on your PC answers this from the check results." });
      return { ok: true };
    }
    return {};
  };
})();
"""

BANNER = """
<div class="replay-note" id="replayNote">
  <b>You are watching a replay.</b> Ayos is a Windows program that runs on your own PC, so it cannot be hosted on a website.
  This page plays back real sessions, recorded step by step on Ayos' simulated PC with <b>__BRAIN__</b> on __MACHINE__.
  Long waits are shortened here; the times printed in each result are the real ones.
  Pick a fault under <b>Practice bench</b>, then press <b>Check it</b>.
  <a href="https://github.com/shaocodes/Ayos">Code and how to run the real thing</a>
</div>
"""

BANNER_CSS = """
  .replay-note { max-width: 1500px; margin: 10px auto 0; padding: 12px 28px; font-size: .95rem; }
  .replay-note { background: #fff8dc; border-top: 3px solid var(--probe); border-bottom: 1px solid #e5d48a; }
  .replay-note a { color: var(--pen); font-weight: 600; margin-left: 6px; }
"""


def build(args) -> int:
    with open(os.path.join(ROOT, "web", "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    with open(args.recording, "r", encoding="utf-8") as f:
        rec = json.load(f)
    start = html.index("async function api(path, body) {")
    end = html.index("/* ---------------- the ticket ---------------- */")
    html = html[:start] + "async function api(path, body) { return window.AYOS_REPLAY(path, body); }\n\n" + html[end:]
    data = json.dumps(rec).replace("</", "<\\/")
    html = html.replace('<script>\n"use strict";', "<script>\nwindow.AYOS_RECORDING = " + data + ";\n" + REPLAY_JS + '</script>\n<script>\n"use strict";', 1)
    banner = BANNER.replace("__BRAIN__", rec["brain"]).replace("__MACHINE__", "a machine with " + rec["machine"].split(", ", 1)[-1] if rec.get("uses_model") else "no language model")
    html = html.replace('<header class="top">', banner + '<header class="top">', 1)
    html = html.replace("  @media (prefers-reduced-motion: reduce)", BANNER_CSS + "  @media (prefers-reduced-motion: reduce)", 1)
    html = html.replace("<title>Ayos repair desk</title>", "<title>Ayos: recorded demo</title>", 1)
    html = html.replace('<div class="sim-band" id="simBand" hidden><p>Simulated PC. Nothing on this computer is changed.</p></div>', '<div class="sim-band" id="simBand" hidden><p>Recorded replay on a simulated PC.</p></div>', 1)
    # the replay uses the recorded complaint, so typing a different one would be misleading
    picks = [sc["question"] for sc in rec["scenarios"] if not sc["fault"]]
    start = html.index("const PICKS = ")
    html = html[:start] + "const PICKS = " + json.dumps(picks) + ";" + html[html.index("\n", start):]
    html = html.replace('$("q").focus();', '$("q").readOnly = true; $("q").placeholder = "Pick a fault on the right. The recorded complaint appears here.";', 1)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print("built", args.out, f"({len(html) // 1024} KB) from a recording made with {rec['brain']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--model", default="gemma3:4b")
    r.add_argument("--url", default=DEFAULT_URL)
    r.add_argument("--rules", action="store_true")
    r.add_argument("--out", default=os.path.join(ROOT, "docs", "recording.json"))
    b = sub.add_parser("build")
    b.add_argument("--recording", default=os.path.join(ROOT, "docs", "recording.json"))
    b.add_argument("--out", default=os.path.join(ROOT, "docs", "index.html"))
    args = p.parse_args()
    return record(args) if args.cmd == "record" else build(args)


if __name__ == "__main__":
    sys.exit(main())
