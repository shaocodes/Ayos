"""Agent tests on the simulated PC. Run:  python -m unittest discover -s tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ayos import fixes, sim  # noqa: E402
from ayos.agent import Session  # noqa: E402
from ayos.brain import RuleBrain, classify, parse_decision  # noqa: E402
from ayos.memory import Memory  # noqa: E402
from ayos.rules import supports  # noqa: E402
from ayos.system import FakeSystem  # noqa: E402
from ayos.tools import CHECKS, snapshot  # noqa: E402


class ScriptedBrain:
    """Stands in for a language model. Returns the decisions it was given, in order, then defers to the rules."""

    kind = "model"
    label = "scripted test model"
    model = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.heard = []
        self.rules = RuleBrain()

    def start(self, question, notes):
        return []

    def observe(self, convo, text):
        self.heard.append(text)

    def record(self, convo, decision):
        pass

    def step(self, convo, view):
        if self.script:
            d = dict(self.script.pop(0))
            d.setdefault("cause", "none")
            d.setdefault("thought", "scripted")
            d.setdefault("message", "scripted message")
            d.update({"source": "model", "seconds": 0.01})
            return d
        d = self.rules.step(convo, view)
        d["source"] = "model"
        return d


def run(system, question, brain=None, memory=None, auto=True):
    s = Session("t", question, system, brain or RuleBrain(), memory or Memory(None), auto_approve=auto)
    s.run()
    return s


def types(s):
    return [e["type"] for e in s.events]


def last(s, type_):
    return [e for e in s.events if e["type"] == type_][-1]


class EveryFault(unittest.TestCase):
    def test_real_faults_are_found_fixed_and_verified(self):
        for fault, (question, cause) in sim.REAL_FAULT_CASES.items():
            with self.subTest(fault=fault):
                pc = FakeSystem()
                mem = Memory(None)
                mem.set_baseline(snapshot(pc))
                sim.apply(pc, fault)
                s = run(pc, question, memory=mem)
                self.assertEqual(s.cause, cause)
                res = last(s, "fix_result")
                self.assertTrue(res["ok"] and res["verified"], res)
                self.assertTrue(pc.http_get("http://example.com")["ok"])

    def test_real_faults_without_any_memory(self):
        for fault, (question, cause) in sim.REAL_FAULT_CASES.items():
            with self.subTest(fault=fault):
                pc = FakeSystem()
                sim.apply(pc, fault)
                s = run(pc, question)
                self.assertEqual(s.cause, cause)
                self.assertTrue(last(s, "fix_result")["verified"])

    def test_simulated_only_faults(self):
        for fault, (_t, _d, _fn, question, cause) in sim.SIM_ONLY.items():
            with self.subTest(fault=fault):
                pc = FakeSystem()
                sim.apply(pc, fault)
                s = run(pc, question)
                self.assertEqual(s.cause, cause)

    def test_healthy_pc_reports_no_fault(self):
        s = run(FakeSystem(), "my internet is not working")
        self.assertEqual(s.cause, "no_fault_found")
        self.assertNotIn("fix_start", types(s))
        s = run(FakeSystem(), "my laptop is slow")
        self.assertEqual(s.cause, "pc_looks_healthy")


class SafetyCheck(unittest.TestCase):
    def test_wrong_conclusion_is_refused_and_corrected(self):
        pc = FakeSystem()
        sim.apply(pc, "wrong_dns")
        brain = ScriptedBrain([{"action": "check_adapters"}, {"action": "conclude", "cause": "isp_outage"}])
        s = run(pc, "my internet is not working", brain=brain)
        guards = [e for e in s.events if e["type"] == "guard"]
        self.assertTrue(guards and not guards[0]["passed"])
        self.assertEqual(s.first_conclusion, "isp_outage")
        self.assertEqual(s.cause, "dns_misconfigured")
        self.assertTrue(last(s, "fix_result")["verified"])

    def test_conclusion_with_no_evidence_never_reaches_a_fix(self):
        pc = FakeSystem()  # healthy
        brain = ScriptedBrain([{"action": "conclude", "cause": "adapter_disabled"}] * 6)
        s = run(pc, "internet is broken", brain=brain)
        self.assertNotEqual(s.cause, "adapter_disabled")
        self.assertNotIn("fix_start", types(s))
        self.assertEqual(pc.adapter["status"], "Up")

    def test_model_cannot_skip_checks_by_answering(self):
        pc = FakeSystem()
        sim.apply(pc, "proxy_on")
        brain = ScriptedBrain([{"action": "answer", "message": "Try restarting your router."}])
        s = run(pc, "my browser can't open any website", brain=brain)
        self.assertEqual(s.cause, "proxy_blocking")
        self.assertTrue(any("reporting a problem" in h for h in brain.heard))

    def test_unknown_or_repeated_actions_do_not_hang(self):
        pc = FakeSystem()
        sim.apply(pc, "hosts_block")
        brain = ScriptedBrain([{"action": "check_dns"}, {"action": "check_dns"}, {"action": "check_dns"}, {"action": "check_dns"}])
        s = run(pc, "I can't open example.com", brain=brain)
        self.assertEqual(s.cause, "hosts_block")

    def test_supports_needs_the_right_check(self):
        self.assertEqual(supports("dns_misconfigured", {}), (False, "check_dns"))
        self.assertEqual(supports("proxy_blocking", {}), (False, "check_proxy"))
        self.assertEqual(supports("made_up_cause", {}), (False, None))


class Approval(unittest.TestCase):
    def test_nothing_changes_until_the_user_approves(self):
        pc = FakeSystem()
        sim.apply(pc, "wrong_dns")
        s = run(pc, "internet not working", auto=False)
        self.assertEqual(s.state, "awaiting")
        self.assertIsNotNone(pc.manual_dns)  # still broken
        self.assertTrue(s.approve())
        s.thread = None
        for _ in range(200):
            if s.state in ("fixed", "done"):
                break
            import time

            time.sleep(0.01)
        self.assertEqual(s.state, "fixed")
        self.assertIsNone(pc.manual_dns)

    def test_decline_changes_nothing(self):
        pc = FakeSystem()
        sim.apply(pc, "proxy_on")
        s = run(pc, "browser cannot open websites", auto=False)
        self.assertTrue(s.decline())
        self.assertTrue(pc.proxy["enabled"])
        self.assertFalse(s.approve())

    def test_undo_puts_the_setting_back(self):
        pc = FakeSystem()
        sim.apply(pc, "wrong_dns")
        s = run(pc, "internet not working")
        self.assertEqual(s.state, "fixed")
        self.assertTrue(s.undo())
        self.assertEqual(pc.manual_dns, ["192.0.2.53", "2001:db8::53"])

    def test_fix_refused_without_admin_and_offer_stays_open(self):
        pc = FakeSystem(admin=True)
        sim.apply(pc, "wrong_dns")
        pc.admin = False
        s = run(pc, "internet not working")
        res = last(s, "fix_result")
        self.assertFalse(res["ok"])
        self.assertIn("administrator", res["message"].lower())
        self.assertEqual(s.state, "awaiting")
        self.assertTrue(last(s, "diagnosis")["fix"]["blocked"])


class MemoryTests(unittest.TestCase):
    def test_baseline_shows_what_changed(self):
        pc = FakeSystem()
        mem = Memory(None)
        mem.set_baseline(snapshot(pc))
        sim.apply(pc, "wrong_dns")
        s = run(pc, "my internet is not working", memory=mem)
        first = [e for e in s.events if e["type"] == "check_result"][0]
        self.assertEqual(first["name"], "compare_with_normal")
        self.assertIn("DNS servers", first["summary"])

    def test_repeat_problem_takes_fewer_checks(self):
        mem = Memory(None)
        pc = FakeSystem()
        sim.apply(pc, "proxy_on")
        first = run(pc, "my internet is not working", memory=mem)
        mem.data["baseline"] = None  # isolate the effect of incident history from the baseline
        sim.apply(pc, "proxy_on")
        second = run(pc, "my internet is not working", memory=mem)
        self.assertEqual(second.cause, "proxy_blocking")
        self.assertLess(len(second.obs), len(first.obs))
        self.assertEqual(last(second, "diagnosis")["seen"]["times"], 1)

    def test_memory_survives_restart(self):
        import tempfile

        d = tempfile.mkdtemp()
        path = os.path.join(d, "m.json")
        pc = FakeSystem()
        sim.apply(pc, "hosts_block")
        run(pc, "cannot open example.com", memory=Memory(path))
        again = Memory(path)
        self.assertEqual(again.seen("hosts_block")["times"], 1)
        self.assertTrue(again.baseline)

    def test_restores_hand_set_dns_from_memory(self):
        pc = FakeSystem()
        pc.manual_dns = ["8.8.8.8"]  # this user really does use a hand-set DNS server
        mem = Memory(None)
        mem.set_baseline(snapshot(pc))
        sim.apply(pc, "wrong_dns")
        s = run(pc, "internet not working", memory=mem)
        self.assertEqual(pc.manual_dns, ["8.8.8.8"])
        self.assertIn("worked before", last(s, "diagnosis")["fix"]["title"])

    def test_damaged_memory_file_is_ignored(self):
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "m.json")
        with open(path, "w") as f:
            f.write("{not json")
        self.assertIsNone(Memory(path).baseline)


class RestoreTests(unittest.TestCase):
    def test_restore_all_clears_every_demo_fault(self):
        pc = FakeSystem()
        for f in fixes.FAULTS:
            sim.apply(pc, f)
        fixes.restore_all(pc)
        self.assertEqual(pc.adapter["status"], "Up")
        self.assertIsNone(pc.manual_dns)
        self.assertFalse(pc.proxy["enabled"])
        self.assertEqual(pc.hosts, [])
        self.assertTrue(pc.http_get("http://example.com")["ok"])

    def test_restore_leaves_the_owners_own_dns_and_proxy(self):
        pc = FakeSystem()
        pc.manual_dns = ["9.9.9.9"]
        pc.proxy = {"enabled": True, "server": "proxy.school.edu:8080", "auto_config_url": ""}
        with self.assertRaises(RuntimeError):
            sim.apply(pc, "proxy_on")
        fixes.restore_all(pc)
        self.assertEqual(pc.manual_dns, ["9.9.9.9"])
        self.assertEqual(pc.proxy["server"], "proxy.school.edu:8080")
        self.assertTrue(pc.proxy["enabled"])

    def test_restore_leaves_the_users_own_hosts_lines(self):
        pc = FakeSystem()
        pc.hosts.append({"ip": "10.0.0.5", "names": ["nas.home"], "demo": False})
        sim.apply(pc, "hosts_block")
        fixes.restore_all(pc)
        self.assertEqual([h["names"] for h in pc.hosts], [["nas.home"]])


class Reading(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("my internet is not working")["route"], "network")
        self.assertEqual(classify("ayaw gumana ng wifi ko")["route"], "network")
        self.assertEqual(classify("ang bagal ng laptop ko")["route"], "pc")
        self.assertEqual(classify("what is DNS?")["route"], "general")
        self.assertFalse(classify("what is DNS?")["is_fault"])
        self.assertTrue(classify("I can't open facebook.com")["is_fault"])

    def test_parse_decision(self):
        d = parse_decision('{"thought":"x","action":"check_dns","cause":"none","message":""}')
        self.assertEqual(d["action"], "check_dns")
        d = parse_decision('Sure! ```json\n{"action": "conclude", "cause": "hosts_block"}\n```')
        self.assertEqual((d["action"], d["cause"]), ("conclude", "hosts_block"))
        self.assertIsNone(parse_decision("I think it is DNS."))
        self.assertIsNone(parse_decision(""))
        self.assertIsNone(parse_decision('{"action": 5}'))

    def test_every_check_has_a_title_and_description(self):
        from ayos.tools import TITLES

        for name in CHECKS:
            self.assertIn(name, TITLES)


if __name__ == "__main__":
    unittest.main()
