"""Tests for the Windows layer, run anywhere.

PowerShell is replaced by a small stand-in that answers the exact commands Ayos sends, with
output shaped like the real cmdlets'. This cannot prove the commands work on Windows (only a
Windows PC can), but it does prove Ayos reads their output correctly and survives odd shapes.
"""
import base64
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ayos import fixes, system as S  # noqa: E402
from ayos.agent import Session  # noqa: E402
from ayos.brain import RuleBrain  # noqa: E402
from ayos.memory import Memory  # noqa: E402
from ayos.tools import check_adapters, check_dns, primary_adapter, snapshot  # noqa: E402


class Proc:
    def __init__(self, out="", err="", rc=0):
        self.stdout, self.stderr, self.returncode = out, err, rc


def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


class PsWorld:
    """A laptop with Wi-Fi, an unplugged Ethernet port and a Bluetooth adapter, as PowerShell would describe it."""

    def __init__(self, admin=True, ipv6_dns_from_router=True):
        self.admin = admin
        self.status = {"Wi-Fi": "Up", "Ethernet": "Disconnected", "Bluetooth Network Connection": "Disconnected"}
        self.manual = {}  # adapter -> list of hand-set servers
        self.router_v6 = ["fe80::1"] if ipv6_dns_from_router else []
        self.scripts = []
        self.proxy = {"enabled": False, "server": "", "auto_config_url": ""}

    def servers(self, name):
        if self.status[name] != "Up":
            return self.manual.get(name, [])
        return self.manual.get(name) or (["192.168.100.1"] + self.router_v6)

    def run(self, cmd, **kw):
        if cmd[0] == "ping":
            return Proc("Reply from 192.168.100.1: bytes=32 time=2ms TTL=64" if self.status["Wi-Fi"] == "Up" else "General failure.")
        if cmd[0] == "ipconfig":
            return Proc("")
        script = cmd[-1].replace(S.WindowsSystem.PS_PREFIX, "")
        m = re.match(r"\$j = & \{ (.*) \}; if \(\$j\)", script, flags=re.S)
        if m:
            script = m.group(1)
        self.scripts.append(script)
        if "Select-Object Name,InterfaceDescription,Status,PhysicalMediaType" in script:
            media = {"Wi-Fi": "Native 802.11", "Ethernet": "802.3", "Bluetooth Network Connection": "BlueTooth"}
            return Proc(b64([{"Name": n, "InterfaceDescription": n + " adapter", "Status": st, "PhysicalMediaType": media[n]} for n, st in self.status.items()]))
        if "Get-NetIPConfiguration" in script:
            if self.status["Wi-Fi"] != "Up":
                return Proc("")
            return Proc(b64({"adapter": "Wi-Fi", "ipv4": "192.168.100.24", "gateway": "192.168.100.1"}))  # one object, not a list
        if "Get-DnsClientServerAddress" in script:
            rows = []
            for n in self.status:
                m = self.manual.get(n, [])
                rows.append({"adapter": n, "servers": self.servers(n), "manual4": ",".join(x for x in m if ":" not in x), "manual6": ",".join(x for x in m if ":" in x)})
            return Proc(b64(rows))
        m = re.match(r"Set-DnsClientServerAddress -InterfaceAlias '(.+?)' -ServerAddresses \((.+)\)", script)
        if m:
            if not self.admin:
                return Proc("", "Set-DnsClientServerAddress : Access is denied.\nAt line:1 char:1\n    + CategoryInfo : PermissionDenied", 1)
            self.manual[m.group(1)] = [x.strip("'") for x in m.group(2).split(",")]
            return Proc("")
        m = re.match(r"Set-DnsClientServerAddress -InterfaceAlias '(.+?)' -ResetServerAddresses", script)
        if m:
            if not self.admin:
                return Proc("", "Set-DnsClientServerAddress : Access is denied.", 1)
            self.manual.pop(m.group(1), None)
            return Proc("")
        m = re.match(r"(Enable|Disable)-NetAdapter -Name '(.+?)' -Confirm:\$false", script)
        if m:
            self.status[m.group(2)] = "Up" if m.group(1) == "Enable" else "Disabled"
            return Proc("")
        if "Clear-DnsClientCache" in script:
            return Proc("")
        raise AssertionError("unexpected PowerShell: " + script)


def make_pc(world: PsWorld, hosts_text="# hosts\n127.0.0.1 localhost\n"):
    """A WindowsSystem wired to the stand-in PowerShell, a temp hosts file, and a pretend network."""
    pc = S.WindowsSystem()
    pc.CACHE_SECONDS = 0
    S.subprocess.run = world.run
    d = tempfile.mkdtemp()
    pc.HOSTS = os.path.join(d, "hosts")
    with open(pc.HOSTS, "w") as f:
        f.write(hosts_text)
    good = {"192.168.100.1", "fe80::1", "1.1.1.1", "8.8.8.8"}
    online = lambda: world.status["Wi-Fi"] == "Up"  # noqa: E731
    pc.is_admin = lambda: world.admin
    pc.tcp_reach = lambda host, port, timeout=2.5: online()
    pc.dns_query = lambda name, server, timeout=2.5: {"answered": online() and server in good, "ip": None, "rcode": 0, "ms": 5}

    def resolve(name, timeout=5.0):
        for h in pc.hosts_entries():
            if name.lower() in h["names"]:
                return h["ip"]
        return "93.184.216.34" if online() and any(x in good for x in world.servers("Wi-Fi")) else None

    def http_get(url, use_system_proxy=True, timeout=6.0):
        if use_system_proxy and world.proxy["enabled"]:
            return {"ok": False, "status": 0, "error": "proxy refused", "ms": 5}
        ip = resolve(S.urlparse(url).hostname)
        ok = bool(ip) and not ip.startswith("127.")
        return {"ok": ok, "status": 200 if ok else 0, "error": "" if ok else "could not connect", "ms": 5}

    pc.resolve, pc.http_get = resolve, http_get
    pc.proxy_get = lambda: dict(world.proxy)
    pc.proxy_set = lambda enabled, server=None: world.proxy.update({"enabled": bool(enabled), **({"server": server} if server is not None else {})})
    pc.disks = lambda: [{"drive": "C:", "total_gb": 475.0, "free_gb": 120.0}]
    pc.memory = lambda: {"total_gb": 15.8, "free_gb": 7.0}
    pc.top_processes = lambda n=5: [{"name": "chrome", "mem_mb": 900}]
    pc.startup_items = lambda: []
    return pc


class Reading(unittest.TestCase):
    def setUp(self):
        self.real_run = S.subprocess.run

    def tearDown(self):
        S.subprocess.run = self.real_run

    def test_adapters_skip_bluetooth_and_pick_wifi(self):
        pc = make_pc(PsWorld())
        self.assertEqual([a["name"] for a in pc.adapters()], ["Wi-Fi", "Ethernet"])
        self.assertTrue(pc.adapters()[0]["wifi"])
        self.assertEqual(primary_adapter(pc)["name"], "Wi-Fi")
        summary, data = check_adapters(pc, {})
        self.assertEqual(data["up"], ["Wi-Fi"])
        self.assertIn("Ethernet (cable): on but not connected", summary)

    def test_single_object_and_empty_output(self):
        w = PsWorld()
        pc = make_pc(w)
        self.assertEqual(pc.ip_config(), [{"adapter": "Wi-Fi", "ipv4": "192.168.100.24", "gateway": "192.168.100.1"}])
        w.status["Wi-Fi"] = "Disabled"
        self.assertEqual(pc.ip_config(), [])

    def test_automatic_dns_with_router_ipv6_is_not_called_manual(self):
        pc = make_pc(PsWorld(ipv6_dns_from_router=True))
        wifi = [d for d in pc.dns_config() if d["adapter"] == "Wi-Fi"][0]
        self.assertEqual(wifi, {"adapter": "Wi-Fi", "servers": ["192.168.100.1", "fe80::1"], "manual": False})
        self.assertNotIn("Bluetooth Network Connection", [d["adapter"] for d in pc.dns_config()])

    def test_decode_handles_odd_shapes(self):
        self.assertEqual(S.decode_ps_json(""), [])
        self.assertEqual(S.decode_ps_json(b64(None)), [])
        self.assertEqual(S.decode_ps_json(b64({"a": 1})), [{"a": 1}])
        self.assertEqual(S.decode_ps_json('[{"a": 1}]'), [{"a": 1}])  # plain JSON still accepted
        self.assertEqual(S.decode_ps_json(b64([{"Name": "Conexión de red"}]))[0]["Name"], "Conexión de red")

    def test_powershell_array_wrapper_quirk(self):
        w = PsWorld()
        pc = make_pc(w)
        real = w.run

        def quirky(cmd, **kw):
            if "Get-DnsClientServerAddress" in cmd[-1]:
                return Proc(b64([{"adapter": "Wi-Fi", "servers": {"value": ["192.168.100.1"], "Count": 1}, "manual4": "", "manual6": ""}]))
            return real(cmd, **kw)

        S.subprocess.run = quirky
        self.assertEqual(pc.dns_config()[0]["servers"], ["192.168.100.1"])

    def test_error_text_is_made_readable(self):
        self.assertIn("administrator rights", S.clean_ps_error("Set-DnsClientServerAddress : Access is denied.\nAt line:1"))
        xml = '#< CLIXML\n<Objs Version="1.1"><S S="Error">Enable-NetAdapter : No matching adapter_x000D__x000A_</S></Objs>'
        self.assertTrue(S.clean_ps_error(xml).startswith("Enable-NetAdapter : No matching adapter"))
        self.assertEqual(S.clean_ps_error(""), "PowerShell reported an error")

    def test_names_with_quotes_are_escaped(self):
        self.assertEqual(S.WindowsSystem._q("O'Brien LAN"), "'O''Brien LAN'")


class WholeFlowOnWindowsLayer(unittest.TestCase):
    def setUp(self):
        self.real_run = S.subprocess.run

    def tearDown(self):
        S.subprocess.run = self.real_run

    def run_fault(self, fault, question, cause, world=None):
        w = world or PsWorld()
        pc = make_pc(w)
        mem = Memory(None)
        mem.set_baseline(snapshot(pc))
        fixes.apply_fault(pc, fault)
        s = Session("t", question, pc, RuleBrain(), mem, auto_approve=True)
        s._verify_tries = 1
        s.run()
        self.assertEqual(s.cause, cause)
        res = [e for e in s.events if e["type"] == "fix_result"][-1]
        self.assertTrue(res["ok"] and res["verified"], res)
        return w, pc, s

    def test_wrong_dns(self):
        w, pc, s = self.run_fault("wrong_dns", "my internet is not working", "dns_misconfigured")
        self.assertEqual(w.manual, {})
        self.assertTrue(any("-ResetServerAddresses" in x for x in w.scripts))
        self.assertTrue(any("'192.0.2.53','2001:db8::53'" in x for x in w.scripts))

    def test_adapter_off(self):
        w, pc, s = self.run_fault("adapter_off", "no internet at all", "adapter_disabled")
        self.assertEqual(w.status["Wi-Fi"], "Up")
        self.assertEqual(w.status["Ethernet"], "Disconnected")  # the unplugged port was left alone

    def test_proxy(self):
        w, pc, s = self.run_fault("proxy_on", "my browser cannot open any website", "proxy_blocking")
        self.assertFalse(w.proxy["enabled"])

    def test_hosts_block_edits_only_its_own_lines(self):
        w = PsWorld()
        pc = make_pc(w, "# my hosts\n127.0.0.1 localhost\n10.0.0.5 nas.home\n")
        fixes.apply_fault(pc, "hosts_block")
        with open(pc.HOSTS) as f:
            self.assertIn("127.0.0.1 example.com # ayos-demo", f.read())
        s = Session("t", "I can't open example.com", pc, RuleBrain(), Memory(None), auto_approve=True)
        s.run()
        self.assertEqual(s.cause, "hosts_block")
        with open(pc.HOSTS) as f:
            text = f.read()
        self.assertNotIn("example.com", text)
        self.assertIn("10.0.0.5 nas.home", text)
        self.assertIn("# my hosts", text)

    def test_dns_check_reads_the_broken_state(self):
        w = PsWorld()
        pc = make_pc(w)
        fixes.apply_fault(pc, "wrong_dns")
        summary, data = check_dns(pc, {})
        self.assertEqual(data["adapter"], "Wi-Fi")
        self.assertTrue(data["manual"])
        self.assertFalse(data["configured_answers"])
        self.assertTrue(data["public_answers"])
        self.assertFalse(data["system_resolves"])

    def test_without_admin_the_fix_is_refused_cleanly(self):
        w = PsWorld()
        pc = make_pc(w)
        fixes.apply_fault(pc, "wrong_dns")
        w.admin = False
        s = Session("t", "internet not working", pc, RuleBrain(), Memory(None), auto_approve=True)
        s.run()
        res = [e for e in s.events if e["type"] == "fix_result"][-1]
        self.assertFalse(res["ok"])
        self.assertIn("administrator", res["message"])
        self.assertTrue(w.manual)  # nothing changed

    def test_restore_all(self):
        w = PsWorld()
        pc = make_pc(w)
        for f in ("wrong_dns", "proxy_on", "hosts_block", "adapter_off"):
            fixes.apply_fault(pc, f)
        done = fixes.restore_all(pc)
        self.assertEqual(w.status["Wi-Fi"], "Up")
        self.assertEqual(w.manual, {})
        self.assertFalse(w.proxy["enabled"])
        self.assertEqual(pc.hosts_entries(), [])
        self.assertGreaterEqual(len(done), 4)


if __name__ == "__main__":
    unittest.main()
