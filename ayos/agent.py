"""One repair session: recall, investigate, conclude, check the conclusion, ask, fix, verify, remember.

The language model only ever picks from a menu. The order of responsibility is:

  1. Memory     - compare the PC with how it looked the last time the internet worked.
  2. Model      - choose the next check, read the result, repeat, then name a cause.
  3. Safety     - plain code confirms that the check results really show that cause.
                  If they do not, the conclusion is refused and the model must look again.
  4. User       - sees the evidence and the exact change, and approves or declines.
  5. Fix        - one named, reversible action from fixes.py.
  6. Verify     - the checks are run again to prove the problem is gone.
  7. Memory     - the incident and the healthy settings are saved for next time.
"""
from __future__ import annotations

import threading
import time

from . import fixes
from .brain import RuleBrain, classify
from .memory import Memory
from .rules import CAUSES, KEY_CHECK, PC_CAUSES, evidence_lines, fix_args, infer, supports
from .system import System
from .tools import CHECKS, NETWORK_CHECKS, PC_CHECKS, TITLES, find_domain, run_check, snapshot, verdict

MAX_MODEL_STEPS = 8
MAX_REFUSALS = 2


class Session:
    def __init__(self, sid: str, question: str, system: System, brain, memory: Memory, auto_approve: bool = False, on_change=None):
        self.id = sid
        self.question = question.strip()
        self.system = system
        self.brain = brain
        self.memory = memory
        self.auto_approve = auto_approve
        self.on_change = on_change  # called after a setting was changed, so the status light can update at once
        self.rules = RuleBrain()
        self.events = []
        self.lock = threading.Lock()
        self.state = "running"  # running | awaiting | fixing | fixed | done
        self.cancelled = False
        self.obs = {}
        self.summaries = {}
        self.ctx = {"target": find_domain(self.question), "memory": memory.ctx()}
        self.cls = classify(self.question)
        self.cause = None
        self.fix_id = None
        self.fix_args = {}
        self.undo_info = None
        self.stats = {"model_seconds": 0.0, "check_seconds": 0.0, "model_steps": 0, "rule_steps": 0, "refusals": 0, "tok_per_s": None}
        self.started = time.time()
        self.diagnosed_after = None
        self.first_conclusion = None  # what the model said first, before the safety check
        self.thread = None

    # ------------------------------------------------------------ plumbing
    def emit(self, type_: str, **data) -> dict:
        with self.lock:
            ev = {"i": len(self.events), "type": type_, "t": round(time.time() - self.started, 2), **data}
            self.events.append(ev)
            return ev

    def since(self, n: int) -> list:
        with self.lock:
            return self.events[n:]

    def start(self):
        self.thread = threading.Thread(target=self._safe, args=(self.run,), daemon=True)
        self.thread.start()
        return self

    def _safe(self, fn):
        try:
            fn()
        except Exception as e:  # a session must always end with something the user can read
            self.emit("error", message=f"Something went wrong inside Ayos: {type(e).__name__}: {str(e)[:200]}")
            if self.state in ("running", "fixing"):
                self.state = "done" if self.state == "running" else "awaiting"
                if self.state == "done":
                    self._finish()

    def _route(self) -> str:
        if self.cls["route"] in ("network", "pc"):
            return self.cls["route"]
        ran_pc = any(c in self.obs for c in PC_CHECKS)
        ran_net = any(c in self.obs for c in NETWORK_CHECKS)
        if ran_pc and not ran_net:
            return "pc"
        if ran_net or ran_pc:
            return "network"
        return "general"

    def _view(self, force_route: bool = False) -> dict:
        route = self._route()
        if force_route and route == "general":
            route = "network"
        return {
            "question": self.question,
            "obs": self.obs,
            "route": route,
            "has_baseline": bool(self.memory.baseline),
            "prefer": self.memory.preferred_checks(),
            "allowed": [c for c in CHECKS if c not in self.obs],
        }

    def _run_check(self, name: str, source: str, thought: str = "") -> str:
        self.emit("check_start", name=name, title=TITLES.get(name, name), source=source, thought=thought)
        t0 = time.time()
        pace = getattr(self.system, "pace", 0)
        if pace:
            time.sleep(pace)  # simulated PC only: checks on a real PC take time, so rehearsals should too
        summary, data = run_check(name, self.system, self.ctx)
        took = time.time() - t0
        self.stats["check_seconds"] += took
        self.obs[name] = data
        self.summaries[name] = summary
        self.emit("check_result", name=name, title=TITLES.get(name, name), summary=summary, ms=int(took * 1000), failed="error" in data, verdict=verdict(name, data))
        return summary

    # ------------------------------------------------------------ investigate
    def run(self):
        notes = [
            self.memory.history_note(),
            "A record of this PC's normal network settings exists." if self.memory.baseline else "There is no record yet of this PC's normal network settings.",
        ]
        if self.ctx["target"]:
            notes.append(f"The website they mention is {self.ctx['target']}.")
        convo = self.brain.start(self.question, notes)
        self.emit("start", question=self.question, brain=self.brain.label, brain_kind=self.brain.kind, target=self.ctx["target"])

        # Step 1 is always memory, with no model call: what changed since the internet last worked?
        if self.memory.baseline and self.cls["route"] == "network":
            d = {"action": "compare_with_normal", "cause": "none", "thought": "First, compare with how this PC looked the last time the internet worked."}
            self.brain.record(convo, d)
            summary = self._run_check("compare_with_normal", "memory", d["thought"])
            self.brain.observe(convo, f"Result of compare_with_normal: {summary}")

        use_rules = self.brain.kind == "rules"
        refused_answer = False
        repeats = 0
        while not self.cancelled:
            if len(self.obs) >= len(CHECKS) + 2:
                break
            brain = self.rules if use_rules else self.brain
            self.emit("thinking", who="rules" if use_rules else self.brain.kind)
            d = brain.step(convo, self._view(force_route=use_rules and self.brain.kind != "rules"))
            took = float(d.get("seconds") or 0)
            if d.get("source") == "model":
                self.stats["model_seconds"] += took
                self.stats["model_steps"] += 1
                if d.get("tok_per_s"):
                    self.stats["tok_per_s"] = d["tok_per_s"]
            else:
                self.stats["rule_steps"] += 1
                if d.get("model_error"):
                    self.stats["model_seconds"] += took
                    self.emit("note", message=f"The language model did not help on this step ({d['model_error']}). The built-in rules chose instead.")
            action = d["action"]

            if action in CHECKS:
                if action in self.obs:
                    repeats += 1
                    self.brain.observe(convo, f"{action} was already run. Its result: {self.summaries[action]} Choose something else.")
                    if repeats >= 2:
                        use_rules = True
                    continue
                summary = self._run_check(action, d.get("source", "rules"), d.get("thought", ""))
                self.brain.observe(convo, f"Result of {action}: {summary}")

            elif action == "conclude":
                cause = d["cause"]
                if d.get("source") == "model" and self.first_conclusion is None:
                    self.first_conclusion = cause
                ok, missing = supports(cause, self.obs)
                if ok:
                    self._diagnosis(cause, d)
                    return
                title = CAUSES[cause][0]
                self.stats["refusals"] += 1
                if missing and missing not in self.obs:
                    self.emit(
                        "guard",
                        passed=False,
                        thought=d.get("thought", ""),
                        message=f"Suggested cause: \"{title}\". Safety check: not proven yet. {TITLES[missing]} first.",
                    )
                    summary = self._run_check(missing, "safety")
                    self.brain.observe(
                        convo,
                        f"Safety check: '{cause}' is not proven yet, so {missing} was run. Result of {missing}: {summary} Decide again from the results.",
                    )
                else:
                    self.emit(
                        "guard",
                        passed=False,
                        thought=d.get("thought", ""),
                        message=f"Suggested cause: \"{title}\". Safety check: the check results do not show that. Looking again.",
                    )
                    self.brain.observe(
                        convo,
                        f"Safety check: the results do not show '{cause}'. Read the results again, then run another check or name a different cause.",
                    )
                if self.stats["refusals"] >= MAX_REFUSALS + 3 or (not missing and self.stats["refusals"] >= MAX_REFUSALS):
                    use_rules = True

            elif action == "answer":
                if self.cls["is_fault"] and d.get("source") == "model":
                    # A fault report gets a diagnosis from evidence, never general advice from the model.
                    if refused_answer:
                        use_rules = True
                    refused_answer = True
                    self.brain.observe(convo, "The user is reporting a problem on this PC. Run a check before you answer.")
                    continue
                self.emit("answer", message=d["message"], source=d.get("source", "rules"))
                self.state = "done"
                self._finish()
                return

            else:  # the rules ran out of checks without a supported cause
                break

            if self.stats["model_steps"] >= MAX_MODEL_STEPS:
                use_rules = True

        if self.cancelled:
            self.state = "done"
            return
        cause = infer(self.obs, "pc" if self._route() == "pc" else "network")
        if cause:
            self._diagnosis(cause, {"source": "rules", "message": "", "thought": ""})
            return
        self.emit(
            "inconclusive",
            message="I could not pin this down to one cause. Here is everything I checked, so you or a technician can take it from here.",
            findings=[self.summaries[n] for n in self.summaries],
        )
        self.state = "done"
        self._finish()

    # ------------------------------------------------------------ conclude
    def _diagnosis(self, cause: str, d: dict):
        self.cause = cause
        self.diagnosed_after = round(time.time() - self.started, 1)
        title, explanation, fix_id, advice = CAUSES[cause]
        seen = self.memory.seen(cause)
        fix = None
        if fix_id:
            self.fix_id = fix_id
            self.fix_args = fix_args(cause, self.obs, self.memory.baseline)
            fix = fixes.describe(fix_id, self.fix_args)
            fix["blocked"] = bool(fix["needs_admin"] and not self.system.is_admin())
        self.emit(
            "diagnosis",
            cause=cause,
            title=title,
            explanation=explanation,
            message=d.get("message", "") if d.get("source") == "model" else "",
            thought=d.get("thought", ""),
            source=d.get("source", "rules"),
            evidence=evidence_lines(cause, self.obs, self.summaries),
            fix=fix,
            advice=advice,
            seen=seen,
            healthy=cause in ("no_fault_found", "pc_looks_healthy"),
            first_conclusion=self.first_conclusion,
        )
        if fix:
            self.state = "awaiting"
            self._summary()
            if self.auto_approve:
                self._apply()
            return
        self.memory.add_incident(self.question, cause, None, None, self.diagnosed_after, len(self.obs), self.brain.label)
        if cause == "no_fault_found":
            self._remember_normal()
        self.state = "done"
        self._finish()

    def _summary(self):
        s = self.stats
        self.emit(
            "summary",
            seconds=self.diagnosed_after if self.diagnosed_after is not None else round(time.time() - self.started, 1),
            model_seconds=round(s["model_seconds"], 1),
            check_seconds=round(s["check_seconds"], 1),
            checks=len(self.obs),
            model_steps=s["model_steps"],
            rule_steps=s["rule_steps"],
            refusals=s["refusals"],
            tok_per_s=s["tok_per_s"],
            brain=self.brain.label,
        )

    def _finish(self):
        self._summary()
        self.emit("done")

    def _remember_normal(self):
        """Save the healthy settings, but only when the internet demonstrably works right now."""
        if self.cause in PC_CAUSES:
            return
        try:
            snap = snapshot(self.system)
            if "error" not in snap and not (snap["proxy_enabled"] and snap["proxy_server"] == fixes.DEMO_PROXY):
                self.memory.set_baseline(snap)
                self.ctx["memory"] = self.memory.ctx()
                self.emit("memory", message="Remembered: this is what this PC looks like when the internet works.")
        except Exception:
            pass

    # ------------------------------------------------------------ user decisions
    def approve(self) -> bool:
        if self.state != "awaiting" or not self.fix_id:
            return False
        self.state = "fixing"
        threading.Thread(target=self._safe, args=(self._apply,), daemon=True).start()
        return True

    def decline(self) -> bool:
        if self.state != "awaiting":
            return False
        self.memory.add_incident(self.question, self.cause, self.fix_id, None, self.diagnosed_after or 0, len(self.obs), self.brain.label)
        self.emit("declined", message="No change was made. You can ask again any time.")
        self.state = "done"
        self.emit("done")
        return True

    def _apply(self):
        self.state = "fixing"
        info = fixes.describe(self.fix_id, self.fix_args)
        self.emit("fix_start", title=info["title"])
        t0 = time.time()
        try:
            self.undo_info = fixes.apply_fix(self.system, self.fix_id, self.fix_args)
        except Exception as e:
            self.emit("fix_result", ok=False, verified=False, message=str(e)[:300], can_undo=False, seconds=round(time.time() - t0, 1))
            self.state = "awaiting"  # nothing changed, so the offer stays open
            return
        if not info["changes_settings"]:
            self.emit("fix_result", ok=True, verified=None, message="Opened the Settings page. Ayos changed nothing.", can_undo=False, checks=[], seconds=round(time.time() - t0, 1))
            self.memory.add_incident(self.question, self.cause, self.fix_id, None, self.diagnosed_after or 0, len(self.obs), self.brain.label)
            self.state = "done"
            self.emit("done")
            return
        verified, lines = self._verify()
        self._changed()
        self.memory.add_incident(self.question, self.cause, self.fix_id, bool(verified), self.diagnosed_after or 0, len(self.obs), self.brain.label)
        self.emit(
            "fix_result",
            ok=True,
            verified=verified,
            message="Fixed. I ran the checks again and the problem is gone." if verified else "The change was made, but the checks still show a problem.",
            checks=lines,
            can_undo=info["can_undo"],
            seconds=round(time.time() - t0, 1),
        )
        if verified:
            self._remember_normal()
        self.state = "fixed" if info["can_undo"] else "done"
        self.emit("done")

    def _verify(self):
        """Run the deciding check again, then try a real page. Returns (verified, summaries)."""
        tries = 1 if self.system.simulated else 6
        key = KEY_CHECK.get(self.cause)
        lines, ok = [], False
        for attempt in range(tries):
            lines = []
            obs = {}
            for name in [key, "test_website"] if self.cause not in PC_CAUSES else [key]:
                if not name:
                    continue
                summary, data = run_check(name, self.system, self.ctx)
                obs[name] = data
                lines.append({"name": name, "summary": summary, "verdict": verdict(name, data)})
            still, _ = supports(self.cause, obs)
            page = obs.get("test_website")
            ok = not still and (page is None or bool(page.get("ok")))
            if ok:
                break
            if attempt < tries - 1:
                time.sleep(2.5)
        return ok, lines

    def undo(self) -> bool:
        if self.state != "fixed" or self.undo_info is None:
            return False
        try:
            done = fixes.undo_fix(self.system, self.fix_id, self.undo_info)
        except Exception as e:
            self.emit("undo_result", ok=False, message=str(e)[:300])
            return False
        self._changed()
        if done:
            self.memory.mark_last(self.cause, None)
            self.emit("undo_result", ok=True, message="Undone. The setting is back to what it was before the fix.")
        self.state = "done"
        return bool(done)

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def public(self) -> dict:
        return {"id": self.id, "state": self.state, "cause": self.cause, "question": self.question}
