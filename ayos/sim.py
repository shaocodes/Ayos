"""Faults for the simulated PC.

The real PC only gets the four safe, reversible demo faults in fixes.py. The simulated PC can
also pretend that the router died or the provider is down, which lets us measure how well a
language model diagnoses problems that cannot be staged safely on a real machine.
"""
from __future__ import annotations

from . import fixes
from .system import FakeSystem


def _router_down(s: FakeSystem):
    s.router_up = False


def _isp_down(s: FakeSystem):
    s.isp_up = False


def _router_dns_down(s: FakeSystem):
    s.router_dns_up = False


def _no_ip(s: FakeSystem):
    s.ip = "169.254.31.7"


def _wifi_dropped(s: FakeSystem):
    s.adapter["status"] = "Disconnected"


def _portal(s: FakeSystem):
    s.portal = True


def _battery_saver(s: FakeSystem):
    s.batt = {"on_battery": True, "saver": True, "percent": 17}


def _needs_restart(s: FakeSystem):
    s.uptime = 23.4
    s.pending_restart = True


def _disk_full(s: FakeSystem):
    s.disk = [{"drive": "C:", "total_gb": 237.0, "free_gb": 3.1}]


def _low_memory(s: FakeSystem):
    s.mem = {"total_gb": 7.9, "free_gb": 0.5}
    s.procs = [{"name": "chrome", "mem_mb": 4120}, {"name": "Teams", "mem_mb": 1310}, {"name": "Code", "mem_mb": 980}]


def _many_startup(s: FakeSystem):
    s.startup = [{"name": n, "command": n + ".exe"} for n in ("Discord", "OneDrive", "Steam", "Spotify", "Teams", "Zoom", "EpicGames", "Skype", "Adobe Updater", "uTorrent")]


# id: (title, what the user would notice, function, what the user would say, expected cause)
SIM_ONLY = {
    "router_down": ("Router stops answering", "No connection.", _router_down, "My internet is not working", "router_unreachable"),
    "isp_down": ("Provider outage", "Wi-Fi is fine but nothing loads.", _isp_down, "The wifi is connected but no internet", "isp_outage"),
    "router_dns_down": ("Router's DNS dies", "Websites stop opening.", _router_dns_down, "Websites will not open", "dns_server_down"),
    "no_ip": ("No address from router", "Connected, but no internet.", _no_ip, "It says connected but there is no internet", "no_ip_address"),
    "wifi_dropped": ("Wi-Fi disconnected", "Not joined to any network.", _wifi_dropped, "I have no internet", "wifi_not_connected"),
    "captive_portal": ("Wi-Fi wants a sign-in", "Connected, but every page is held back.", _portal, "The wifi is connected but nothing opens", "captive_portal"),
    "battery_saver": ("Battery saver on", "The laptop is slow on battery.", _battery_saver, "My laptop got slow after I unplugged the charger", "battery_saver"),
    "needs_restart": ("Not restarted for weeks", "The PC keeps getting slower.", _needs_restart, "My PC has been getting slower and slower", "needs_restart"),
    "disk_full": ("Drive almost full", "The PC is slow.", _disk_full, "My laptop is very slow", "disk_full"),
    "low_memory": ("Memory almost full", "The PC is slow.", _low_memory, "My PC keeps hanging and lagging", "low_memory"),
    "many_startup": ("Too many start-up apps", "Slow to start.", _many_startup, "My computer takes forever to start up", "many_startup_apps"),
}

# The four faults that also work on a real PC: (what the user would say, expected cause)
REAL_FAULT_CASES = {
    "wrong_dns": ("My internet is not working, websites will not load", "dns_misconfigured"),
    "adapter_off": ("I have no internet at all", "adapter_disabled"),
    "proxy_on": ("My browser cannot open any website", "proxy_blocking"),
    "hosts_block": ("I cannot open example.com but other sites work", "hosts_block"),
    "clock_wrong": ("Websites say my connection is not private", "clock_wrong"),
}


def apply(system, fault_id: str) -> str:
    if fault_id in fixes.FAULTS:
        return fixes.apply_fault(system, fault_id)
    if fault_id in SIM_ONLY and getattr(system, "simulated", False):
        title, desc, fn, _q, _c = SIM_ONLY[fault_id]
        fn(system)
        return f"Simulated: {title.lower()}. {desc}"
    raise KeyError(fault_id)


def reset(system: FakeSystem) -> None:
    """Put the simulated PC back to healthy."""
    fresh = FakeSystem(admin=system.admin)
    for k, v in fresh.__dict__.items():
        if k not in ("opened", "calls", "pace"):
            setattr(system, k, v)


def fault_list(simulated: bool) -> list:
    out = [
        {"id": fid, "title": t, "desc": d, "needs_admin": adm, "real": True, "say": REAL_FAULT_CASES[fid][0]}
        for fid, (t, d, _fn, adm) in fixes.FAULTS.items()
    ]
    if simulated:
        out += [{"id": fid, "title": t, "desc": d, "needs_admin": False, "real": False, "say": q} for fid, (t, d, _fn, q, _c) in SIM_ONLY.items()]
    return out
