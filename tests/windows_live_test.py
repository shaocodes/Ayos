"""Live test on a real Windows PC: break a setting, let Ayos find and fix it, check it is really fixed.

    python tests/windows_live_test.py            DNS, proxy and hosts-file faults
    python tests/windows_live_test.py --adapter  also switch the network adapter off and on

Needs administrator rights. It changes real network settings for a few seconds at a time and
always puts them back, even if a step fails. It uses the built-in rules, so no model is needed:
the point is to prove that the Windows commands, the checks and the fixes work on this machine.
"""
import os
import platform
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ayos import fixes  # noqa: E402
from ayos.agent import Session  # noqa: E402
from ayos.brain import RuleBrain  # noqa: E402
from ayos.memory import Memory  # noqa: E402
from ayos.sim import REAL_FAULT_CASES  # noqa: E402
from ayos.system import WindowsSystem  # noqa: E402
from ayos.tools import CHECKS, TEST_URL, primary_adapter, run_check, snapshot  # noqa: E402

LINES = []


def say(text=""):
    print(text, flush=True)
    LINES.append(text)


def internet(pc):
    return bool(pc.http_get(TEST_URL, use_system_proxy=True, timeout=5.0)["ok"])


def wait_internet(pc, want=True, seconds=40):
    end = time.time() + seconds
    while time.time() < end:
        if internet(pc) == want:
            return True
        time.sleep(2)
    return internet(pc) == want


def main() -> int:
    if os.name != "nt":
        print("This test only runs on Windows.")
        return 0
    pc = WindowsSystem()
    say(f"Windows live test on {platform.platform()}, Python {platform.python_version()}")
    say(f"Administrator: {pc.is_admin()}")
    if not pc.is_admin():
        say("FAILED: run this as administrator.")
        return 1
    t0 = time.time()
    pc.warm()
    say(f"PowerShell mode: {pc.ps_mode()}  (first use took {time.time() - t0:.1f} s)")
    t0 = time.time()
    pc._changed()
    pc.adapters()
    say(f"PowerShell mode after warm-up: {pc.ps_mode()}  (a repeat read took {time.time() - t0:.2f} s)")
    say(f"Adapters: {pc.adapters()}")
    say(f"Main adapter: {primary_adapter(pc)}")
    say(f"IP: {pc.ip_config()}")
    say(f"DNS: {pc.dns_config()}")
    say(f"Proxy: {pc.proxy_get()}")
    say("")
    for name in CHECKS:
        t0 = time.time()
        summary, data = run_check(name, pc, {"target": None, "memory": {"baseline": None}})
        say(f"[{'FAIL' if 'error' in data else ' ok '}] {name:22s} {int((time.time() - t0) * 1000):6d} ms  {summary}")
    say("")
    if not internet(pc):
        say("FAILED: the internet is not working before the test starts, so the results would mean nothing.")
        return 1

    mem = Memory(None)
    mem.set_baseline(snapshot(pc))
    say(f"Baseline: {mem.baseline['snapshot']}")
    faults = ["proxy_on", "hosts_block", "wrong_dns"] + (["adapter_off"] if "--adapter" in sys.argv else [])
    failed = 0
    for fault in faults:
        question, expected = REAL_FAULT_CASES[fault]
        say("")
        say(f"=== {fault}: \"{question}\"")
        try:
            say("break:   " + fixes.apply_fault(pc, fault))
            broke = True if fault == "hosts_block" else wait_internet(pc, want=False, seconds=20)
            say(f"         internet now {'DOWN (fault took effect)' if fault != 'hosts_block' and broke else 'unchanged' if fault != 'hosts_block' else 'n/a (one site only)'}")
            if fault == "hosts_block":
                # Windows notices a changed hosts file after a moment, not instantly.
                t1 = time.time()
                broke = False
                while time.time() - t1 < 20 and not broke:
                    broke = not pc.http_get("http://example.com", timeout=5.0)["ok"]
                    if not broke:
                        time.sleep(1)
                say(f"         example.com stopped loading: {broke} (after {time.time() - t1:.1f} s)")
            t0 = time.time()
            s = Session("live", question, pc, RuleBrain(), mem, auto_approve=True)
            s.run()
            for e in s.events:
                if e["type"] == "check_result":
                    say(f"check:   {e['name']} ({e['ms']} ms) {e['summary']}")
                elif e["type"] == "diagnosis":
                    say(f"cause:   {e['cause']}  fix: {(e['fix'] or {}).get('title')}")
                elif e["type"] == "fix_result":
                    say(f"fix:     ok={e['ok']} verified={e.get('verified')} {e['message']}")
                    for c in e.get("checks") or []:
                        say(f"         again: {c['summary']}")
                elif e["type"] in ("error", "inconclusive", "note"):
                    say(f"{e['type']}: {e['message']}")
            took = time.time() - t0
            fixed = [e for e in s.events if e["type"] == "fix_result"]
            back = wait_internet(pc, want=True, seconds=40)
            good = broke and s.cause == expected and bool(fixed and fixed[-1]["ok"] and fixed[-1]["verified"]) and back
            say(f"result:  {'PASS' if good else 'FAIL'}  fault took effect={broke}, cause={s.cause} (wanted {expected}), internet back={back}, {took:.1f} s")
            if good and fault == "wrong_dns":
                say("undo:    " + str(s.undo()) + f"  -> DNS now {[d for d in pc.dns_config() if d['manual']]}")
            failed += not good
        except Exception:
            failed += 1
            say("EXCEPTION:\n" + traceback.format_exc())
        finally:
            try:
                say("restore: " + " ".join(fixes.restore_all(pc)))
            except Exception:
                say("RESTORE EXCEPTION:\n" + traceback.format_exc())
            wait_internet(pc, want=True, seconds=40)
    say("")
    say(f"After the test: internet works = {internet(pc)}; DNS = {pc.dns_config()}; proxy = {pc.proxy_get()}; hosts = {pc.hosts_entries()}")
    say("RESULT: " + ("ALL PASSED" if not failed else f"{failed} FAILED"))
    try:
        os.makedirs("ci_out", exist_ok=True)
        with open(os.path.join("ci_out", "windows_live.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(LINES) + "\n")
    except OSError:
        pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
