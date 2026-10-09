"""The only code that changes anything on the PC.

Three groups:
  FIXES   - repairs Ayos may propose. Each one says exactly what it will change and can be undone.
  FAULTS  - demo helpers that break a setting on purpose, for testing and for the live demo.
  restore_all() - puts every network setting Ayos knows about back to normal.

The language model never writes commands. It can only name one of the fix ids below,
and nothing runs until the user approves it.
"""
from __future__ import annotations

import time

from .system import BOGUS_DNS, PUBLIC_DNS, System
from .tools import primary_adapter

DEMO_PROXY = "127.0.0.1:9"
DEMO_DOMAIN = "example.com"


def _wait(fn, seconds: float, step: float = 0.5):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return bool(fn())


# ----------------------------------------------------------------- repairs
def _enable_adapter(system: System, args: dict):
    system.enable_adapter(args["adapter"])
    if not system.simulated:
        _wait(lambda: any(a["name"] == args["adapter"] and a["status"] == "Up" for a in system.adapters()), 20)
        _wait(lambda: any(c.get("gateway") for c in system.ip_config()), 15)
    return {"adapter": args["adapter"]}


def _reset_dns(system: System, args: dict):
    system.set_dns(args["adapter"], args.get("restore_to") or None)
    return {"adapter": args["adapter"], "previous": args.get("previous")}


def _use_public_dns(system: System, args: dict):
    system.set_dns(args["adapter"], PUBLIC_DNS)
    return {"adapter": args["adapter"]}


def _disable_proxy(system: System, args: dict):
    system.proxy_set(False)
    return {"previous": args.get("previous", "")}


def _remove_hosts(system: System, args: dict):
    pairs = [{"ip": e["ip"], "name": n} for e in args.get("entries", []) for n in e["names"]]
    removed_names = set()
    for n in sorted({p["name"] for p in pairs}):
        if system.hosts_remove(n):
            removed_names.add(n)
    return {"removed": [p for p in pairs if p["name"] in removed_names]}


def _renew_ip(system: System, args: dict):
    system.renew_ip()
    return {}


def _open(page: str):
    def go(system: System, args: dict):
        system.open_settings(page)
        return {}

    return go


FIXES = {
    "enable_adapter": {
        "title": lambda a: f"Turn the '{a['adapter']}' adapter back on",
        "detail": lambda a: f"Enables the network adapter '{a['adapter']}' in Windows. Nothing else is changed.",
        "apply": _enable_adapter,
        "undo": lambda s, u: s.disable_adapter(u["adapter"]),
        "admin": True,
        "changes": True,
    },
    "reset_dns": {
        "title": lambda a: ("Set DNS back to what worked before" if a.get("restore_to") else "Set DNS back to automatic"),
        "detail": lambda a: (
            f"Sets the DNS servers on '{a['adapter']}' back to {', '.join(a['restore_to'])}, which this PC used the last time the internet worked, then clears the DNS cache."
            if a.get("restore_to")
            else f"Removes the hand-set DNS servers on '{a['adapter']}' so Windows uses the ones from your router again, then clears the DNS cache."
        ),
        "apply": _reset_dns,
        "undo": lambda s, u: s.set_dns(u["adapter"], u["previous"]) if u.get("previous") else None,
        "admin": True,
        "changes": True,
    },
    "use_public_dns": {
        "title": lambda a: "Use a public DNS server",
        "detail": lambda a: f"Sets DNS on '{a['adapter']}' to 1.1.1.1 and 8.8.8.8, which are answering, instead of the router's DNS.",
        "apply": _use_public_dns,
        "undo": lambda s, u: s.set_dns(u["adapter"], None),
        "admin": True,
        "changes": True,
    },
    "disable_proxy": {
        "title": lambda a: "Turn the proxy off",
        "detail": lambda a: "Turns off 'Use a proxy server' in Windows. The proxy address is kept, only the switch is turned off.",
        "apply": _disable_proxy,
        "undo": lambda s, u: s.proxy_set(True, u.get("previous") or None),
        "admin": False,
        "changes": True,
    },
    "remove_hosts_entry": {
        "title": lambda a: "Remove the blocking line from the hosts file",
        "detail": lambda a: "Removes these names from the hosts file: " + ", ".join(sorted({n for e in a.get("entries", []) for n in e["names"]})) + ". Other lines are left alone.",
        "apply": _remove_hosts,
        "undo": lambda s, u: [s.hosts_add(r["ip"], r["name"]) for r in u.get("removed", [])],
        "admin": True,
        "changes": True,
    },
    "renew_ip": {
        "title": lambda a: "Ask the router for an address again",
        "detail": lambda a: "Runs the Windows command that requests a fresh address from the router. No setting is changed.",
        "apply": _renew_ip,
        "undo": None,
        "admin": False,
        "changes": True,
    },
    "open_wifi_settings": {
        "title": lambda a: "Open Wi-Fi settings",
        "detail": lambda a: "Opens the Wi-Fi page in Windows Settings so you can pick your network. Ayos changes nothing.",
        "apply": _open("network-wifi"),
        "undo": None,
        "admin": False,
        "changes": False,
    },
    "open_storage_settings": {
        "title": lambda a: "Open Storage settings",
        "detail": lambda a: "Opens the Storage page in Windows Settings, which shows what is using space. Ayos deletes nothing.",
        "apply": _open("storagesense"),
        "undo": None,
        "admin": False,
        "changes": False,
    },
    "open_startup_settings": {
        "title": lambda a: "Open Startup apps settings",
        "detail": lambda a: "Opens the Startup apps page in Windows Settings so you can switch off what you do not need. Ayos changes nothing.",
        "apply": _open("startupapps"),
        "undo": None,
        "admin": False,
        "changes": False,
    },
}


def describe(fix_id: str, args: dict) -> dict:
    f = FIXES[fix_id]
    return {
        "id": fix_id,
        "title": f["title"](args),
        "detail": f["detail"](args),
        "needs_admin": f["admin"],
        "changes_settings": f["changes"],
        "can_undo": f["undo"] is not None,
    }


def apply_fix(system: System, fix_id: str, args: dict) -> dict:
    f = FIXES[fix_id]
    if f["admin"] and not system.is_admin():
        raise PermissionError("This fix needs administrator rights. Close Ayos and start it with 'Run as administrator'.")
    return f["apply"](system, args) or {}


def undo_fix(system: System, fix_id: str, undo_info: dict) -> bool:
    f = FIXES[fix_id]
    if not f["undo"]:
        return False
    f["undo"](system, undo_info)
    return True


# ----------------------------------------------------------------- demo faults
def _need_adapter(system: System) -> str:
    a = primary_adapter(system)
    if not a:
        raise RuntimeError("No network adapter found.")
    return a["name"]


def break_wrong_dns(system: System):
    name = _need_adapter(system)
    system.set_dns(name, BOGUS_DNS)
    return f"DNS on '{name}' now points to addresses that never answer."


def break_adapter_off(system: System):
    name = _need_adapter(system)
    system.disable_adapter(name)
    return f"Adapter '{name}' is now turned off."


_stash = {}  # settings a practice fault overwrote, so "put everything back" can return them


def break_proxy_on(system: System):
    before = system.proxy_get()
    if before["enabled"] and before["server"] != DEMO_PROXY:
        raise RuntimeError("This PC already uses a proxy, so this practice fault was skipped to keep your setting safe.")
    if before["server"] != DEMO_PROXY:
        _stash["proxy_server"] = before["server"]
    system.proxy_set(True, DEMO_PROXY)
    return f"Windows now sends web traffic to a proxy that does not exist ({DEMO_PROXY})."


def break_hosts_block(system: System, domain: str = DEMO_DOMAIN):
    # Both address families, so the block also holds on networks that have IPv6.
    # 0.0.0.0 and :: mean "nowhere". Pointing at this PC itself (127.0.0.1, ::1) is not a reliable block:
    # on a real Windows test machine a local web service answered and the site appeared to load.
    for n in (domain, "www." + domain):
        system.hosts_add("0.0.0.0", n)
        system.hosts_add("::", n)
    return f"The hosts file now blocks {domain}."


FAULTS = {
    "wrong_dns": ("Wrong DNS server", "Websites stop opening, but Wi-Fi still shows connected.", break_wrong_dns, True),
    "adapter_off": ("Adapter turned off", "No connection at all.", break_adapter_off, True),
    "proxy_on": ("Fake proxy", "The browser cannot reach anything.", break_proxy_on, False),
    "hosts_block": (f"Block {DEMO_DOMAIN}", f"Only {DEMO_DOMAIN} fails to open.", break_hosts_block, True),
}


def apply_fault(system: System, fault_id: str) -> str:
    title, _desc, fn, needs_admin = FAULTS[fault_id]
    if needs_admin and not system.is_admin():
        raise PermissionError("Setting this fault needs administrator rights. Start Ayos with 'Run as administrator'.")
    return fn(system)


def restore_all(system: System) -> list:
    """Undo every practice fault. Settings the owner chose on purpose are left alone."""
    done = []
    for a in system.adapters():
        if a["status"] == "Disabled":
            try:
                system.enable_adapter(a["name"])
                done.append(f"Turned adapter '{a['name']}' back on.")
            except Exception as e:
                done.append(f"Could not turn on '{a['name']}': {e}")
    try:
        for d in system.dns_config():
            if d["manual"] and any(s in BOGUS_DNS for s in d["servers"]):
                try:
                    system.set_dns(d["adapter"], None)
                    done.append(f"Set DNS on '{d['adapter']}' back to automatic.")
                except Exception as e:
                    done.append(f"Could not reset DNS on '{d['adapter']}': {e}")
    except Exception as e:
        done.append(f"Could not read DNS settings: {e}")
    try:
        p = system.proxy_get()
        if p["server"] == DEMO_PROXY:
            system.proxy_set(False, _stash.pop("proxy_server", ""))
            done.append("Turned the practice proxy off.")
    except Exception as e:
        done.append(f"Could not check the proxy: {e}")
    try:
        demo = [n for h in system.hosts_entries() if h.get("demo") for n in h["names"]]
        for n in demo:
            system.hosts_remove(n)
        if demo:
            done.append("Removed practice lines from the hosts file: " + ", ".join(demo) + ".")
    except Exception as e:
        done.append(f"Could not clean the hosts file: {e}")
    try:
        system.flush_dns()
    except Exception:
        pass
    return done or ["Nothing needed putting back."]
