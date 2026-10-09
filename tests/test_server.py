"""Tests for the model client (against a stand-in model server) and for the local web server."""
import json
import os
import re
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ayos import sim  # noqa: E402
from ayos.agent import Session  # noqa: E402
from ayos.brain import LocalModelBrain  # noqa: E402
from ayos.memory import Memory  # noqa: E402
from ayos.server import App, make_server  # noqa: E402
from ayos.system import FakeSystem, build_dns_query, parse_dns_response, parse_hosts, remove_hosts_name, udp_dns_query  # noqa: E402
from ayos.tools import snapshot  # noqa: E402

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FakeModelServer:
    """Speaks just enough of the Ollama and OpenAI chat APIs to test our side of the conversation."""

    def __init__(self, replies, models=("gemma3:4b",), reject_think=False, delay=0.0):
        self.replies = list(replies)
        self.delay = delay
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self._send(200, {"models": [{"name": m} for m in models]})
                if self.path == "/v1/models":
                    return self._send(200, {"data": [{"id": m} for m in models]})
                self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body))
                if reject_think and "think" in body:
                    return self._send(400, {"error": '"gemma3:4b" does not support thinking'})
                if (body.get("options") or {}).get("num_predict") == 1 or body.get("max_tokens") == 1:
                    outer.requests.pop()  # a warm-up call: answer it, but it is not part of the script
                    text = "{}"
                    if self.path == "/api/chat":
                        return self._send(200, {"message": {"role": "assistant", "content": text}, "eval_count": 1, "eval_duration": 1})
                    return self._send(200, {"choices": [{"message": {"content": text}}], "usage": {"completion_tokens": 1}})
                if outer.delay:
                    time.sleep(outer.delay)
                text = outer.replies.pop(0) if outer.replies else '{"thought":"","action":"answer","cause":"none","message":"out of script"}'
                if self.path == "/api/chat" and body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    third = max(1, len(text) // 3)
                    for i in range(0, len(text), third):
                        self.wfile.write((json.dumps({"message": {"role": "assistant", "content": text[i:i + third]}, "done": False}) + "\n").encode())
                        self.wfile.flush()
                        if outer.delay:
                            time.sleep(outer.delay / 3)
                    self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 40, "eval_duration": 2_000_000_000, "prompt_eval_count": 12, "prompt_eval_duration": 500_000_000}) + "\n").encode())
                    return
                if self.path == "/api/chat":
                    return self._send(200, {"message": {"role": "assistant", "content": text}, "eval_count": 40, "eval_duration": 2_000_000_000})
                if self.path == "/v1/chat/completions":
                    return self._send(200, {"choices": [{"message": {"content": text}}], "usage": {"completion_tokens": 40}})
                self._send(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def J(action, cause="none", message="", thought="because"):
    return json.dumps({"thought": thought, "action": action, "cause": cause, "message": message})


def run_session(pc, brain, question="my internet is not working", memory=None):
    s = Session("t", question, pc, brain, memory or Memory(None), auto_approve=True)
    s.run()
    return s


class ModelClient(unittest.TestCase):
    def test_full_session_with_a_model(self):
        ms = FakeModelServer([J("check_dns"), J("conclude", "dns_misconfigured", "Your DNS setting points to a server that is not there.")])
        try:
            pc = FakeSystem()
            sim.apply(pc, "wrong_dns")
            brain = LocalModelBrain("gemma3:4b", ms.url)
            self.assertTrue(brain.available())
            s = run_session(pc, brain)
            diag = [e for e in s.events if e["type"] == "diagnosis"][0]
            self.assertEqual(diag["cause"], "dns_misconfigured")
            self.assertEqual(diag["source"], "model")
            self.assertIn("not there", diag["message"])
            self.assertEqual(s.stats["model_steps"], 2)
            self.assertEqual(s.stats["tok_per_s"], 20.0)
            # what we sent
            (p1, b1), (p2, b2) = ms.requests
            self.assertEqual(p1, "/api/chat")
            self.assertIs(b1["stream"], True)  # replies are streamed so the interface can show them being written
            self.assertEqual(b1["options"]["temperature"], 0)
            self.assertIn("check_dns", b1["format"]["properties"]["action"]["enum"])
            self.assertNotIn("check_dns", b2["format"]["properties"]["action"]["enum"])  # no repeats offered
            self.assertEqual(b2["messages"][: len(b1["messages"])], b1["messages"])  # only ever appended: cache friendly
            self.assertEqual(b2["messages"][-1]["role"], "user")
            self.assertIn("Result of check_dns", b2["messages"][-1]["content"])
        finally:
            ms.close()

    def test_once_the_evidence_is_enough_the_model_must_name_the_cause(self):
        ms = FakeModelServer([J("check_dns"), J("conclude", "dns_server_down", "Router DNS."), J("conclude", "dns_misconfigured", "DNS.")])
        try:
            pc = FakeSystem()
            sim.apply(pc, "wrong_dns")
            s = run_session(pc, LocalModelBrain("gemma3:4b", ms.url))
            b1, b2, b3 = [b for _p, b in ms.requests]
            menu = b1["format"]["properties"]["action"]["enum"]
            self.assertIn("check_dns", menu)
            self.assertNotIn("check_disk", menu)  # an internet problem is not offered the slow-PC checks
            self.assertNotIn("compare_with_normal", menu)  # nothing to compare with yet
            self.assertEqual(b2["format"]["properties"]["action"]["enum"], ["conclude"])
            self.assertIn("enough to name the cause", b2["messages"][-1]["content"])
            self.assertIn("refused because the DNS servers were set by hand", b3["messages"][-1]["content"])
            self.assertEqual(s.cause, "dns_misconfigured")
            self.assertEqual(s.first_conclusion, "dns_server_down")
            self.assertEqual(s.stats["model_steps"], 3)
        finally:
            ms.close()

    def test_memory_goes_straight_to_what_changed(self):
        ms = FakeModelServer([J("conclude", "dns_misconfigured", "x")])
        try:
            pc = FakeSystem()
            mem = Memory(None)
            mem.set_baseline(snapshot(pc))
            sim.apply(pc, "wrong_dns")
            s = run_session(pc, LocalModelBrain("gemma3:4b", ms.url), memory=mem)
            self.assertEqual(len(ms.requests), 1)  # one model call for the whole diagnosis
            body = ms.requests[0][1]
            msgs = body["messages"]
            self.assertEqual([m["role"] for m in msgs], ["system", "user"])  # memory's findings arrive as plain facts
            self.assertIn("Checks were already run", msgs[0]["content"])  # the short instructions
            self.assertLess(len(msgs[0]["content"]), 1300)
            self.assertIn("Already checked from memory. compare_with_normal: Changed since the internet last worked", msgs[1]["content"])
            self.assertIn("check_dns: DNS servers on 'Wi-Fi'", msgs[1]["content"])
            self.assertIn("enough to name the cause", msgs[1]["content"])
            self.assertEqual(body["format"]["properties"]["action"]["enum"], ["conclude"])
            sources = [e["source"] for e in s.events if e["type"] == "check_start"]
            self.assertEqual(sources, ["memory", "memory"])
            self.assertEqual(s.cause, "dns_misconfigured")
        finally:
            ms.close()

    def test_adapter_change_is_checked_before_the_dns_change_it_causes(self):
        pc = FakeSystem()
        mem = Memory(None)
        mem.set_baseline(snapshot(pc))
        sim.apply(pc, "adapter_off")
        ms = FakeModelServer([J("conclude", "adapter_disabled", "Naka-off ang adapter.")])
        try:
            s = run_session(pc, LocalModelBrain("gemma3:4b", ms.url), "wala akong internet", memory=mem)
            self.assertEqual([e["name"] for e in s.events if e["type"] == "check_start"], ["compare_with_normal", "check_adapters"])
            self.assertEqual(s.cause, "adapter_disabled")
        finally:
            ms.close()

    def test_system_prompt_stays_short(self):
        from ayos.brain import system_prompt

        self.assertLess(len(system_prompt()), 2900)  # about 650 tokens; every token costs time on a CPU
        short = system_prompt(True, "network")
        self.assertLess(len(short), 1300)
        self.assertIn("dns_misconfigured", short)
        self.assertNotIn("disk_full", short)  # a slow-PC cause is not offered for an internet problem
        self.assertNotIn("check_proxy", short)  # no list of checks: they were already run

    def test_unusable_reply_falls_back_for_that_step(self):
        ms = FakeModelServer(["I think you should restart the router!", J("conclude", "proxy_blocking", "A proxy is in the way.")])
        try:
            pc = FakeSystem()
            sim.apply(pc, "proxy_on")
            s = run_session(pc, LocalModelBrain("gemma3:4b", ms.url), "browser cannot open any site")
            self.assertEqual(s.cause, "proxy_blocking")
            self.assertTrue(any(e["type"] == "note" for e in s.events))
            self.assertGreaterEqual(s.stats["rule_steps"], 1)
        finally:
            ms.close()

    def test_wrong_model_conclusion_is_caught(self):
        ms = FakeModelServer([J("conclude", "isp_outage", "Your provider is down."), J("conclude", "isp_outage", "Still the provider."), J("conclude", "isp_outage", "Provider.")])
        try:
            pc = FakeSystem()
            sim.apply(pc, "hosts_block")
            s = run_session(pc, LocalModelBrain("gemma3:4b", ms.url), "I can't open example.com")
            self.assertEqual(s.first_conclusion, "isp_outage")
            self.assertEqual(s.cause, "hosts_block")
            self.assertGreaterEqual(s.stats["refusals"], 1)
        finally:
            ms.close()

    def test_think_flag_is_dropped_when_the_model_rejects_it(self):
        ms = FakeModelServer([J("check_adapters")], reject_think=True)
        try:
            brain = LocalModelBrain("gemma3:4b", ms.url)
            convo = brain.start("no internet", [])
            d = brain.step(convo, {"question": "no internet", "obs": {}, "route": "network", "has_baseline": False, "prefer": [], "allowed": ["check_adapters", "check_dns"]})
            self.assertEqual((d["action"], d["source"]), ("check_adapters", "model"))
            self.assertFalse(brain.use_think_flag)
            self.assertEqual(len(ms.requests), 2)
        finally:
            ms.close()

    def test_server_down_uses_rules_and_says_so(self):
        brain = LocalModelBrain("gemma3:4b", "http://127.0.0.1:9", timeout=2)
        self.assertFalse(brain.available())
        pc = FakeSystem()
        sim.apply(pc, "adapter_off")
        s = run_session(pc, brain, "no internet at all")
        self.assertEqual(s.cause, "adapter_disabled")
        self.assertEqual(s.stats["model_steps"], 0)
        self.assertTrue(any(e["type"] == "note" and "not running" in e["message"] for e in s.events))

    def test_openai_compatible_server(self):
        ms = FakeModelServer([J("check_proxy"), J("conclude", "proxy_blocking", "Proxy.")], models=("local-model",))
        try:
            brain = LocalModelBrain("local-model", ms.url, api="openai")
            self.assertEqual(brain.list_models(), ["local-model"])
            pc = FakeSystem()
            sim.apply(pc, "proxy_on")
            s = run_session(pc, brain, "browser cannot open any site")
            self.assertEqual(s.cause, "proxy_blocking")
            path, body = ms.requests[0]
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(body["response_format"]["type"], "json_schema")
        finally:
            ms.close()

    def test_no_fault_conclusion_is_proven_by_the_safety_check_without_more_model_calls(self):
        ms = FakeModelServer([J("check_adapters"), J("conclude", "no_fault_found", "Everything looks fine.")])
        try:
            s = run_session(FakeSystem(), LocalModelBrain("gemma3:4b", ms.url), "I think my internet is broken")
            self.assertEqual(s.cause, "no_fault_found")
            self.assertEqual(len(ms.requests), 2)
            self.assertEqual(s.stats["refusals"], 0)
            by = [(e["name"], e["source"]) for e in s.events if e["type"] == "check_start"]
            self.assertEqual(by[0], ("check_adapters", "model"))
            self.assertEqual({n for n, src in by if src == "safety"}, {"check_dns", "check_proxy", "check_hosts", "test_website"})
            guard = [e for e in s.events if e["type"] == "guard"][0]
            self.assertEqual(guard["kind"], "more_proof")
            self.assertEqual([e for e in s.events if e["type"] == "diagnosis"][0]["source"], "model")
        finally:
            ms.close()

    def test_wrong_no_fault_conclusion_is_still_refused(self):
        ms = FakeModelServer([J("conclude", "no_fault_found", "All good."), J("conclude", "no_fault_found", "All good.")])
        try:
            pc = FakeSystem()
            sim.apply(pc, "hosts_block")
            s = run_session(pc, LocalModelBrain("gemma3:4b", ms.url), "I can't open example.com")
            self.assertEqual(s.cause, "hosts_block")
            self.assertGreaterEqual(s.stats["refusals"], 1)
            self.assertEqual(s.first_conclusion, "no_fault_found")
        finally:
            ms.close()

    def test_a_verdict_about_the_wrong_kind_of_problem_is_refused(self):
        ms = FakeModelServer([J("conclude", "no_fault_found", "Internet is fine."), J("conclude", "no_fault_found", "Internet is fine.")])
        try:
            s = run_session(FakeSystem(), LocalModelBrain("gemma3:4b", ms.url), "My laptop feels slow")
            self.assertEqual(s.cause, "pc_looks_healthy")
            self.assertGreaterEqual(s.stats["refusals"], 1)
            self.assertNotIn("check_dns", s.obs)  # no internet checks were run to "prove" an internet verdict
            causes = ms.requests[0][1]["format"]["properties"]["cause"]["enum"]
            self.assertIn("disk_full", causes)
            self.assertNotIn("dns_misconfigured", causes)
        finally:
            ms.close()

    def test_a_plain_question_is_only_offered_answer(self):
        ms = FakeModelServer([J("answer", message="DNS turns names into numbers.")])
        try:
            run_session(FakeSystem(), LocalModelBrain("gemma3:4b", ms.url), "What is DNS?")
            self.assertEqual(ms.requests[0][1]["format"]["properties"]["action"]["enum"], ["answer"])
        finally:
            ms.close()

    def test_general_question_is_answered_by_the_model(self):
        ms = FakeModelServer([J("answer", message="DNS is the phone book of the internet.")])
        try:
            s = run_session(FakeSystem(), LocalModelBrain("gemma3:4b", ms.url), "What is DNS?")
            self.assertEqual([e["message"] for e in s.events if e["type"] == "answer"], ["DNS is the phone book of the internet."])
            self.assertEqual(s.obs, {})
        finally:
            ms.close()


class Streaming(unittest.TestCase):
    def test_peek_reads_unfinished_json(self):
        from ayos.brain import peek

        self.assertEqual(peek('{"thought": "The DNS setting cha'), {"thought": "The DNS setting cha"})
        self.assertEqual(peek('{"thought": "ok", "action": "check_dns", "cause": "no'), {"thought": "ok", "action": "check_dns", "cause": "no"})
        self.assertEqual(peek('{"thought": "say \\"hi\\" then\\'), {"thought": 'say "hi" then'})
        self.assertEqual(peek('{"thought": "x", "action": "conclude", "cause": "hosts_block", "message": "Naka-block ang si')["message"], "Naka-block ang si")
        self.assertEqual(peek(""), {})
        self.assertEqual(peek("not json at all"), {})

    def test_reply_is_reported_while_it_is_written(self):
        ms = FakeModelServer([J("check_dns", thought="DNS changed, so check DNS.")])
        try:
            brain = LocalModelBrain("gemma3:4b", ms.url)
            seen = []
            d = brain.step(brain.start("no internet", []), {"question": "", "obs": {}, "route": "network", "has_baseline": False, "prefer": [], "allowed": ["check_dns"]}, on_text=seen.append)
            self.assertEqual(d["action"], "check_dns")
            self.assertGreaterEqual(len(seen), 2)
            self.assertTrue(seen[-1].startswith(seen[0]))
            self.assertEqual(d["prompt_tokens"], 12)
            self.assertEqual(d["tok_per_s"], 20.0)
        finally:
            ms.close()

    def test_session_exposes_live_text_and_clears_it(self):
        ms = FakeModelServer([J("check_dns", thought="DNS changed."), J("conclude", "dns_misconfigured", "Sira ang DNS setting.")], delay=0.3)
        try:
            pc = FakeSystem()
            sim.apply(pc, "wrong_dns")
            s = Session("t", "walang internet", pc, LocalModelBrain("gemma3:4b", ms.url), Memory(None))
            lives = []
            real = s._on_text
            s._on_text = lambda text: (real(text), lives.append(dict(s.live)))
            s.run()
            self.assertTrue(any(v.get("thought") for v in lives))
            self.assertEqual(s.live, {})
            self.assertEqual(s.cause, "dns_misconfigured")
        finally:
            ms.close()


class FollowUp(unittest.TestCase):
    def test_an_answer_cut_off_mid_sentence_is_shown_as_text_not_json(self):
        ms = FakeModelServer(['{"message": "Because the DNS setting was changed by hand, and'])
        try:
            brain = LocalModelBrain("gemma3:4b", ms.url)
            res = brain.say(brain.start("no internet", []), "why did this happen?")
            self.assertTrue(res["ok"])
            self.assertEqual(res["text"], "Because the DNS setting was changed by hand, and")
        finally:
            ms.close()


class WebServer(unittest.TestCase):
    def setUp(self):
        self.pc = FakeSystem()
        self.app = App(self.pc, Memory(None), monitor=False)
        self.app.force_rules = True
        self.srv = make_server(self.app, 0)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, path, body=None, token=True, host=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Ayos-Token"] = self.app.token
        if host:
            headers["Host"] = host
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with NO_PROXY.open(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def wait_state(self, sid, states, timeout=5):
        s = self.app.sessions[sid]
        end = time.time() + timeout
        while time.time() < end and s.state not in states:
            time.sleep(0.02)
        return s.state

    def test_page_carries_the_token_and_api_needs_it(self):
        code, html = self.call("/", token=False)
        self.assertEqual(code, 200)
        self.assertIn(self.app.token, html)
        self.assertNotIn("__AYOS_TOKEN__", html)
        self.assertEqual(self.call("/api/ask", {"question": "x"}, token=False)[0], 403)
        self.assertEqual(self.call("/api/break", {"fault": "wrong_dns"}, token=False)[0], 403)
        self.assertIsNone(self.pc.manual_dns)

    def test_requests_for_another_host_are_refused(self):
        self.assertEqual(self.call("/api/status", host="evil.example.com")[0], 403)
        self.assertEqual(self.call("/api/break", {"fault": "wrong_dns"}, host="evil.example.com")[0], 403)

    def test_break_ask_approve_undo_restore(self):
        code, body = self.call("/api/break", {"fault": "wrong_dns"})
        self.assertEqual(code, 200, body)
        self.assertTrue(self.app.memory.baseline)  # normal was remembered before breaking
        code, body = self.call("/api/ask", {"question": "my internet is not working"})
        sid = json.loads(body)["id"]
        self.assertEqual(self.wait_state(sid, ("awaiting",)), "awaiting")
        self.assertIsNotNone(self.pc.manual_dns)
        code, body = self.call(f"/api/events?id={sid}&after=0")
        events = json.loads(body)["events"]
        self.assertIn("diagnosis", [e["type"] for e in events])
        self.assertTrue(json.loads(self.call("/api/approve", {"id": sid})[1])["ok"])
        self.assertEqual(self.wait_state(sid, ("fixed",)), "fixed")
        self.assertIsNone(self.pc.manual_dns)
        self.assertTrue(json.loads(self.call("/api/undo", {"id": sid})[1])["ok"])
        self.assertIsNotNone(self.pc.manual_dns)
        self.assertEqual(self.call("/api/restore", {})[0], 200)
        self.assertIsNone(self.pc.manual_dns)

    def test_memory_can_be_switched_off(self):
        self.call("/api/break", {"fault": "proxy_on"})
        self.assertTrue(self.app.memory.baseline)
        self.assertTrue(json.loads(self.call("/api/memory/enabled", {"on": False})[1])["ok"])
        self.assertFalse(json.loads(self.call("/api/status")[1])["memory"]["enabled"])
        sid = json.loads(self.call("/api/ask", {"question": "my browser cannot open any website"})[1])["id"]
        self.wait_state(sid, ("awaiting",))
        s = self.app.sessions[sid]
        self.assertNotIn("compare_with_normal", s.obs)  # investigated from scratch
        self.assertEqual(s.cause, "proxy_blocking")
        self.call("/api/approve", {"id": sid})
        self.wait_state(sid, ("fixed",))
        self.assertEqual(self.app.memory.public()["incidents"], 0)  # and nothing was saved

    def test_page_cannot_be_framed_and_no_second_job_during_a_fix(self):
        req = urllib.request.Request(self.base + "/", headers={})
        with NO_PROXY.open(req, timeout=5) as r:
            self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
            self.assertIn("frame-ancestors 'none'", r.headers.get("Content-Security-Policy", ""))
        self.call("/api/break", {"fault": "wrong_dns"})
        sid = json.loads(self.call("/api/ask", {"question": "my internet is not working"})[1])["id"]
        self.wait_state(sid, ("awaiting",))
        self.app.sessions[sid].state = "fixing"
        code, body = self.call("/api/ask", {"question": "and now?"})
        self.assertEqual(code, 500)
        self.assertIn("applying a fix", body)

    def test_bad_input(self):
        self.assertEqual(self.call("/api/ask", {"question": "   "})[0], 400)
        self.assertEqual(self.call("/api/break", {"fault": "format_c"})[0], 400)
        self.assertEqual(self.call("/api/approve", {"id": "nope"})[0], 404)
        self.assertEqual(self.call("/api/nothing", {})[0], 404)

    def test_status_shape(self):
        st = json.loads(self.call("/api/status")[1])
        for key in ("simulated", "admin", "internet", "model", "memory", "faults"):
            self.assertIn(key, st)
        self.assertTrue(st["simulated"])
        self.assertEqual(len([f for f in st["faults"] if f["real"]]), 4)

    def test_page_has_no_external_resources(self):
        html = self.call("/", token=False)[1]
        self.assertEqual(re.findall(r"""(?:src|href)\s*=\s*["']https?://""", html), [])


class LowLevel(unittest.TestCase):
    def test_dns_packet_round_trip(self):
        q = build_dns_query("www.example.com", qid=0x1234)
        self.assertEqual(q[:2], b"\x12\x34")
        self.assertIn(b"\x03www\x07example\x03com\x00", q)
        answer = q[:2] + b"\x81\x80" + b"\x00\x01\x00\x01\x00\x00\x00\x00" + q[12:] + b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x5d\xb8\xd8\x22"
        self.assertEqual(parse_dns_response(answer), {"answered": True, "ip": "93.184.216.34", "rcode": 0})
        self.assertFalse(parse_dns_response(b"\x00")["answered"])

    def test_dns_query_to_a_dead_server_fails_fast(self):
        t0 = time.time()
        r = udp_dns_query("example.com", "127.0.0.1", timeout=0.5, port=9)
        self.assertFalse(r["answered"])
        self.assertLess(time.time() - t0, 2)

    def test_hosts_file_parsing(self):
        text = "# comment\n127.0.0.1 localhost\n127.0.0.1 example.com www.example.com # ayos-demo\n10.0.0.5 nas.home\n\n::1 localhost\n"
        entries = parse_hosts(text)
        self.assertEqual([e["names"] for e in entries], [["example.com", "www.example.com"], ["nas.home"]])
        self.assertTrue(entries[0]["demo"])
        kept, n = remove_hosts_name(text, "example.com")
        self.assertEqual(n, 1)
        self.assertIn("127.0.0.1 www.example.com", kept)
        self.assertIn("10.0.0.5 nas.home", kept)
        self.assertIn("127.0.0.1 localhost", kept)
        kept, n = remove_hosts_name(kept, "www.example.com")
        self.assertNotIn("example.com", kept)


if __name__ == "__main__":
    unittest.main()
