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
from .brain import RuleBrain, classify, peek
from .memory import Memory
from .rules import CAUSES, KEY_CHECK, NETWORK_CAUSES, PC_CAUSES, changed_areas, evidence_lines, fix_args, infer, supports, why_not
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
        self.steps = []  # one line per model call, for measuring where the time goes
        self.started = time.monotonic()
        self.diagnosed_after = None
        self.first_conclusion = None  # what the model said first, before the safety check
        self.thread = None
        self.convo = []
        self.pending = 0  # follow-up answers still being written
        self.live = {}  # what the model has written so far in the reply it is working on right now
        self.ready = False  # True once the check results are enough to prove a cause
        self._told_ready = False

    # ------------------------------------------------------------ plumbing
    def emit(self, type_: str, **data) -> dict:
        with self.lock:
            ev = {"i": len(self.events), "type": type_, "t": round(time.monotonic() - self.started, 2), **data}
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
            self.emit("error", message=f"Something went wrong inside Resolv: {type(e).__name__}: {str(e)[:200]}")
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
        # The menu the model chooses from. For an internet problem only the network checks are offered,
        # and once the results already prove a cause, no more checks are offered: the model has to name it.
        menu = list(CHECKS)
        causes = None  # None means every cause may be named
        if self.cls["must_check"] and self.cls["route"] in ("network", "pc"):
            menu = PC_CHECKS if self.cls["route"] == "pc" else NETWORK_CHECKS
            causes = PC_CAUSES if self.cls["route"] == "pc" else NETWORK_CAUSES
        allowed = [c for c in menu if c not in self.obs]
        if not self.memory.baseline and "compare_with_normal" in allowed:
            allowed.remove("compare_with_normal")
        self.ready = bool(self.obs) and infer(self.obs, "pc" if route == "pc" else "network") is not None
        return {
            "question": self.question,
            "obs": self.obs,
            "route": route,
            "has_baseline": bool(self.memory.baseline),
            "prefer": self.memory.preferred_checks(),
            "allowed": [] if self.ready else allowed,
            "ready": self.ready,
            "causes": causes,
            # "What is DNS?" is a question, not a fault report: it gets an answer, not a round of checks.
            "answer_only": bool(self.cls["just_asking"] and not self.obs),
        }

    def _on_text(self, text: str) -> None:
        seen = peek(text)
        self.live = {"thought": seen.get("thought", ""), "action": seen.get("action", ""), "message": seen.get("message", ""), "chars": len(text)}

    def _run_check(self, name: str, source: str, thought: str = "") -> str:
        self.emit("check_start", name=name, title=TITLES.get(name, name), source=source, thought=thought)
        t0 = time.monotonic()
        pace = getattr(self.system, "pace", 0)
        if pace:
            time.sleep(pace)  # simulated PC only: checks on a real PC take time, so rehearsals should too
        summary, data = run_check(name, self.system, self.ctx)
        took = time.monotonic() - t0
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
        self.emit("start", question=self.question, brain=self.brain.label, brain_kind=self.brain.kind, target=self.ctx["target"])

        # Step 1 is always memory, with no model call: what changed since the internet last worked?
        # If memory shows that a setting changed, Resolv looks there straight away, without asking the model where to look.
        # The results are handed to the model as plain facts in its first message.
        if self.memory.baseline and self.cls["route"] == "network":
            summary = self._run_check("compare_with_normal", "memory", "First, compare with how this PC looked the last time the internet worked.")
            notes.append(f"Already checked from memory. compare_with_normal: {summary}")
            areas = changed_areas(self.obs)
            if areas:
                name, what = areas[0]
                summary = self._run_check(name, "memory", f"Memory shows {what} changed since the internet last worked, so look there first.")
                notes.append(f"{name}: {summary}")

        # "Your connection is not private" is what a browser says when the PC's date is wrong. If the owner's
        # words point there and memory found nothing else, check the clock before asking the model.
        if self.cls.get("mentions_clock") and self.cls["route"] == "network" and len(self.obs) <= 1:
            summary = self._run_check("check_clock", "hint", "The owner's words point at the date and time, so check the clock first.")
            notes.append(f"Already checked. check_clock: {summary}")

        # If those results already prove a cause, the model gets the short instructions: read, then name it.
        first_view = self._view()
        compact = bool(first_view["ready"] and first_view["causes"])
        convo = self.convo = self.brain.start(self.question, notes, compact=compact, route=self.cls["route"] if compact else None)

        use_rules = self.brain.kind == "rules"
        refused_answer = False
        repeats = 0
        model_failures = 0
        while not self.cancelled:
            brain = self.rules if use_rules else self.brain
            self.emit("thinking", who="rules" if use_rules else self.brain.kind)
            view = self._view(force_route=use_rules and self.brain.kind != "rules")
            if view["ready"] and not self._told_ready and not use_rules:
                self._told_ready = True
                self.emit("note", message="The results are now enough to prove a cause, so no more checks are offered. The model has to name it.")
                self.brain.observe(convo, "The results are now enough to name the cause. Conclude.")
            if getattr(brain, "streams", False):
                d = brain.step(convo, view, on_text=self._on_text)
            else:
                d = brain.step(convo, view)
            self.live = {}
            took = float(d.get("seconds") or 0)
            if d.get("source") == "model":
                self.stats["model_seconds"] += took
                self.stats["model_steps"] += 1
                if d.get("tok_per_s"):
                    self.stats["tok_per_s"] = d["tok_per_s"]
                self.steps.append({k: d.get(k) for k in ("action", "seconds", "tokens", "gen_seconds", "prompt_tokens", "prompt_seconds")})
            else:
                self.stats["rule_steps"] += 1
                if d.get("model_error"):
                    self.stats["model_seconds"] += took
                    model_failures += 1
                    if model_failures >= 2 and not use_rules:
                        use_rules = True  # stop asking a model that is not answering
                        self.emit("note", message=f"The language model is not answering ({d['model_error']}). The built-in rules are finishing this job.")
                    else:
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
                title = CAUSES[cause][0]
                if view.get("causes") and cause not in view["causes"]:
                    ok, missing = False, None  # an answer about the wrong kind of problem (a slow PC is not an internet fault)
                # Not proven yet? The safety check gathers the missing proof itself, then judges the conclusion again.
                extra = []
                while not ok and missing and missing not in self.obs and len(extra) < 6:
                    if not extra:
                        self.emit(
                            "guard",
                            kind="more_proof",
                            passed=None,
                            thought=d.get("thought", ""),
                            message=f"The model's conclusion: \"{title}\". Not proven yet, so the safety check runs what is missing before it decides.",
                        )
                    extra.append((missing, self._run_check(missing, "safety")))
                    ok, missing = supports(cause, self.obs)
                results = " ".join(f"Result of {n}: {t}" for n, t in extra)
                if ok:
                    if extra:
                        self.brain.observe(convo, f"The safety check needed more proof for '{cause}' and ran: {results}")
                    self._diagnosis(cause, d)
                    return
                self.stats["refusals"] += 1
                self.emit(
                    "guard",
                    kind="refused",
                    passed=False,
                    thought="" if extra else d.get("thought", ""),
                    message=f"The model's conclusion: \"{title}\". Refused: {why_not(cause, self.obs)}. Looking again.",
                )
                self.brain.observe(
                    convo,
                    (f"More checks were run. {results} " if extra else "")
                    + f"Safety check: '{cause}' is refused because {why_not(cause, self.obs)}. Read the results again and name a different cause"
                    + (", or run another check." if not infer(self.obs, "pc" if self._route() == "pc" else "network") else "."),
                )
                if self.stats["refusals"] >= MAX_REFUSALS:
                    use_rules = True

            elif action == "answer":
                if self.cls["must_check"] and d.get("source") == "model":
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
        self.diagnosed_after = round(time.monotonic() - self.started, 1)
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
        self.brain.observe(
            self.convo,
            f"The safety check confirmed the cause: {title}. "
            + (f"Proposed fix, waiting for the user's approval: {fix['title']}. {fix['detail']}" if fix else f"No automatic fix. Advice given: {advice}"),
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
            seconds=self.diagnosed_after if self.diagnosed_after is not None else round(time.monotonic() - self.started, 1),
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
        t0 = time.monotonic()
        try:
            self.undo_info = fixes.apply_fix(self.system, self.fix_id, self.fix_args)
        except Exception as e:
            self.emit("fix_result", ok=False, verified=False, message=str(e)[:300], can_undo=False, seconds=round(time.monotonic() - t0, 1))
            self.state = "awaiting"  # nothing changed, so the offer stays open
            return
        if not info["changes_settings"]:
            self.emit("fix_result", ok=True, verified=None, message="Opened the Settings page. Resolv changed nothing.", can_undo=False, checks=[], seconds=round(time.monotonic() - t0, 1))
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
            seconds=round(time.monotonic() - t0, 1),
        )
        self.brain.observe(self.convo, "The user approved the fix. It was applied. " + ("Checked again: the problem is gone." if verified else "Checked again: a problem is still showing."))
        if verified:
            self._remember_normal()
        self.state = "fixed" if info["can_undo"] else "done"
        self.emit("done")

    def _verify(self):
        """Run the deciding check again, then try a real page. Returns (verified, summaries)."""
        tries = getattr(self, "_verify_tries", None) or (1 if self.system.simulated else 6)
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

    def follow_up(self, question: str) -> bool:
        """A question about this result, answered by the model from the evidence already gathered."""
        question = question.strip()[:400]
        if not question or self.state in ("running", "fixing") or self.pending:
            return False
        self.pending += 1
        self.emit("followup_q", message=question)

        def go():
            try:
                if getattr(self.brain, "streams", False):
                    res = self.brain.say(self.convo, question, on_text=self._on_text)
                else:
                    res = self.brain.say(self.convo, question)
                self.live = {}
                self.emit("followup_a", message=res["text"], ok=res["ok"], seconds=res.get("seconds"), source=self.brain.kind)
            finally:
                self.pending -= 1

        threading.Thread(target=self._safe, args=(go,), daemon=True).start()
        return True

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
        return {"id": self.id, "state": self.state, "cause": self.cause, "question": self.question, "pending": self.pending}
