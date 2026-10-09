"""Read-only checks. Each returns (summary, data).

`summary` is one or two plain sentences the language model reads.
`data` is the structured version that the safety rules and the fixes use.
Nothing in this file changes any setting.
"""
from __future__ import annotations

import concurrent.futures
import re

from .system import PUBLIC_DNS, System

TEST_HOST = "www.msftconnecttest.com"
TEST_URL = "http://www.msftconnecttest.com/connecttest.txt"
BLOCK_IPS = ("127.", "0.0.0.0", "::1")


def primary_adapter(system: System):
    """The adapter that should carry the internet: an Up one, Wi-Fi first."""
    ads = system.adapters()
    up = [a for a in ads if a["status"] == "Up"]
    pool = up or ads
    pool = sorted(pool, key=lambda a: (not a.get("wifi"),))
    return pool[0] if pool else None


def find_domain(text: str):
    m = re.search(r"\b((?:[a-z0-9-]+\.)+(?:com|net|org|ph|io|edu|gov|co|app|dev|tv|me)(?:\.[a-z]{2})?)\b", text.lower())
    return m.group(1) if m else None


def snapshot(system: System) -> dict:
    """A small picture of the settings that matter, used for 'what changed since it last worked'."""
    snap = {"adapters": {}, "dns": {}, "proxy_enabled": False, "proxy_server": "", "hosts_blocked": [], "gateway": None}
    try:
        for a in system.adapters():
            snap["adapters"][a["name"]] = a["status"]
        for d in system.dns_config():
            snap["dns"][d["adapter"]] = {"servers": d["servers"], "manual": d["manual"]}
        p = system.proxy_get()
        snap["proxy_enabled"], snap["proxy_server"] = p["enabled"], p["server"]
        snap["hosts_blocked"] = sorted(n for h in system.hosts_entries() if h["ip"].startswith(BLOCK_IPS) for n in h["names"])
        for c in system.ip_config():
            if c.get("gateway"):
                snap["gateway"] = c["gateway"]
                break
    except Exception as e:  # a snapshot is best effort
        snap["error"] = str(e)[:120]
    return snap


def diff_snapshots(normal: dict, now: dict) -> list:
    changes = []
    for name, status in now.get("adapters", {}).items():
        was = normal.get("adapters", {}).get(name)
        if was and was != status:
            changes.append(f"Adapter '{name}' was {was}, now {status}.")
    for name, cur in now.get("dns", {}).items():
        was = normal.get("dns", {}).get(name)
        if was and (was.get("servers") != cur.get("servers") or was.get("manual") != cur.get("manual")):
            a = ", ".join(was.get("servers") or []) or "none"
            b = ", ".join(cur.get("servers") or []) or "none"
            how = "set manually" if cur.get("manual") else "automatic"
            changes.append(f"DNS servers on '{name}' were {a}, now {b} ({how}).")
    if bool(normal.get("proxy_enabled")) != bool(now.get("proxy_enabled")):
        changes.append(
            f"Proxy is now {'ON (' + (now.get('proxy_server') or 'no address') + ')' if now.get('proxy_enabled') else 'off'}; it was "
            f"{'on' if normal.get('proxy_enabled') else 'off'}."
        )
    new_blocks = sorted(set(now.get("hosts_blocked", [])) - set(normal.get("hosts_blocked", [])))
    if new_blocks:
        changes.append("New blocked names in the hosts file: " + ", ".join(new_blocks) + ".")
    return changes


# ----------------------------------------------------------------- network checks
def compare_with_normal(system: System, ctx: dict):
    base = (ctx.get("memory") or {}).get("baseline")
    if not base:
        return "No record yet of what this PC looks like when the internet works.", {"has_baseline": False, "changes": []}
    changes = diff_snapshots(base["snapshot"], snapshot(system))
    if not changes:
        return f"Settings match the last time the internet worked ({base['saved_at']}). Nothing changed.", {"has_baseline": True, "changes": []}
    return "Changed since the internet last worked: " + " ".join(changes), {"has_baseline": True, "changes": changes}


def check_adapters(system: System, ctx: dict):
    ads = system.adapters()
    up = [a["name"] for a in ads if a["status"] == "Up"]
    disabled = [a["name"] for a in ads if a["status"] == "Disabled"]
    disconnected = [a["name"] for a in ads if a["status"] not in ("Up", "Disabled")]
    parts = []
    for a in ads:
        note = {"Up": "connected", "Disabled": "turned OFF in Windows"}.get(a["status"], "on but not connected to any network")
        kind = "Wi-Fi" if a.get("wifi") else "cable"
        label = a["name"] if kind.lower() in a["name"].lower() else f"{a['name']} ({kind})"
        parts.append(f"{label}: {note}")
    summary = "Network adapters: " + "; ".join(parts) + "." if parts else "No network adapter was found."
    return summary, {"adapters": ads, "up": up, "disabled": disabled, "disconnected": disconnected}


def check_ip_and_router(system: System, ctx: dict):
    cfg = None
    for c in system.ip_config():
        if c.get("ipv4"):
            cfg = c
            if c.get("gateway"):
                break
    ipv4 = cfg.get("ipv4") if cfg else None
    gateway = cfg.get("gateway") if cfg else None
    apipa = bool(ipv4 and ipv4.startswith("169.254."))
    ping = system.ping(gateway) if gateway else None
    passes = None
    if gateway and not ping:
        # Some routers (and many public Wi-Fi networks) ignore pings. Traffic getting through proves the router works.
        passes = bool(system.tcp_reach("1.1.1.1", 443) or system.tcp_reach("8.8.8.8", 53))
    router_ok = bool(ping or passes) if gateway else None
    if not ipv4:
        summary = "This PC has no IP address, so it is not really on a network."
    elif apipa:
        summary = f"This PC gave itself the address {ipv4}, which means the router did not hand out an address."
    elif not gateway:
        summary = f"IP address is {ipv4} but there is no router (gateway) address."
    elif ping:
        summary = f"IP address {ipv4}, router {gateway}. The router answers."
    elif passes:
        summary = f"IP address {ipv4}, router {gateway}. The router ignores pings, but traffic passes through it, so it is working."
    else:
        summary = f"IP address {ipv4}, router {gateway}. The router does NOT answer and nothing passes through it."
    return summary, {"ipv4": ipv4, "gateway": gateway, "apipa": apipa, "gateway_ping": ping, "router_ok": router_ok}


def check_internet_reach(system: System, ctx: dict):
    hits = [system.tcp_reach("1.1.1.1", 443), system.tcp_reach("8.8.8.8", 53)]
    ok = any(hits)
    summary = (
        "Internet addresses can be reached directly by number, so the line to the internet is working."
        if ok
        else "Internet addresses cannot be reached even by number, so traffic is not getting out to the internet."
    )
    return summary, {"reachable": ok}


def check_dns(system: System, ctx: dict):
    prim = primary_adapter(system)
    cfgs = system.dns_config()
    cfg = next((c for c in cfgs if prim and c["adapter"] == prim["name"]), cfgs[0] if cfgs else None)
    servers = (cfg or {}).get("servers", [])
    manual = bool((cfg or {}).get("manual"))
    # Ask every server at the same time, so a dead one costs one timeout instead of several.
    asked = list(servers[:4])
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        f_conf = [pool.submit(system.dns_query, TEST_HOST, s) for s in asked]
        f_pub = [pool.submit(system.dns_query, TEST_HOST, s) for s in PUBLIC_DNS]
        f_res = pool.submit(system.resolve, TEST_HOST, 3.0)
        results = [{"server": s, "answered": bool(f.result().get("answered"))} for s, f in zip(asked, f_conf)]
        public_answers = any(f.result().get("answered") for f in f_pub)
        resolves = bool(f_res.result())
    configured_answers = any(r["answered"] for r in results)
    adapter = (cfg or {}).get("adapter") or (prim or {}).get("name")
    if not servers:
        s1 = f"No DNS server is set on '{adapter}'."
    else:
        s1 = f"DNS servers on '{adapter}': {', '.join(servers)} ({'set manually' if manual else 'automatic from the router'})."
        s1 += " They answer." if configured_answers else " None of them answer."
    s2 = " A public DNS server (1.1.1.1) " + ("answers when asked directly." if public_answers else "does not answer either.")
    s3 = " Windows " + ("can" if resolves else "CANNOT") + " turn website names into addresses."
    return s1 + s2 + s3, {
        "adapter": adapter,
        "servers": servers,
        "manual": manual,
        "server_results": results,
        "configured_answers": configured_answers,
        "public_answers": public_answers,
        "system_resolves": resolves,
    }


def check_proxy(system: System, ctx: dict):
    p = system.proxy_get()
    direct = system.http_get(TEST_URL, use_system_proxy=False, timeout=4.0)
    via = system.http_get(TEST_URL, use_system_proxy=True, timeout=4.0) if p["enabled"] else None
    if p["enabled"]:
        summary = f"A proxy is turned ON ({p['server'] or 'no address'}). Through the proxy a test page " + (
            "loads." if via and via["ok"] else "FAILS."
        )
        summary += " Without the proxy the same page " + ("loads fine." if direct["ok"] else "also fails.")
    else:
        summary = "No proxy is set. A test page " + ("loads." if direct["ok"] else f"fails to load ({direct['error'] or 'no answer'}).")
    return summary, {
        "enabled": p["enabled"],
        "server": p["server"],
        "auto_config_url": p.get("auto_config_url", ""),
        "direct_ok": direct["ok"],
        "via_proxy_ok": (via or {}).get("ok") if via else None,
    }


def check_hosts(system: System, ctx: dict):
    entries = system.hosts_entries()
    blocked = sorted({n for h in entries if h["ip"].startswith(BLOCK_IPS) for n in h["names"]})
    target = ctx.get("target")
    blocks_target = bool(target and any(target == n or target == "www." + n or "www." + target == n for n in blocked))
    if not entries:
        summary = "The hosts file has no extra entries. No website is blocked there."
    elif blocked:
        summary = "The hosts file blocks these names by pointing them at this PC itself: " + ", ".join(blocked) + "."
        if target:
            summary += f" That {'includes' if blocks_target else 'does not include'} {target}."
    else:
        summary = f"The hosts file has {len(entries)} extra entries but none of them block a website."
    return summary, {"entries": entries, "blocked_names": blocked, "blocks_target": blocks_target}


def test_website(system: System, ctx: dict):
    target = ctx.get("target")
    url = f"http://{target}" if target else TEST_URL
    r = system.http_get(url, use_system_proxy=True, timeout=5.0)
    summary = f"Opening {url}: " + ("it loads." if r["ok"] else f"it FAILS ({r['error'] or 'no answer'}).")
    return summary, {"url": url, "ok": r["ok"], "why": r["error"]}


# ----------------------------------------------------------------- slow-PC checks
def check_disk(system: System, ctx: dict):
    disks = system.disks()
    low = [d for d in disks if d["total_gb"] and (d["free_gb"] / d["total_gb"] < 0.10 or d["free_gb"] < 10)]
    text = "; ".join(f"{d['drive']} {d['free_gb']} GB free of {d['total_gb']} GB" for d in disks) or "no drives found"
    return f"Storage: {text}." + (" Space is running low." if low else " Space is fine."), {"disks": disks, "low": [d["drive"] for d in low]}


def check_memory(system: System, ctx: dict):
    m = system.memory()
    top = system.top_processes(5)
    low = bool(m["total_gb"] and m["free_gb"] / m["total_gb"] < 0.15)
    hogs = ", ".join(f"{p['name']} {p['mem_mb']} MB" for p in top[:3])
    return (
        f"Memory: {m['free_gb']} GB free of {m['total_gb']} GB." + (" Memory is nearly full." if low else " Memory is fine.") + f" Biggest users: {hogs}.",
        {"memory": m, "top": top, "low": low},
    )


def check_startup(system: System, ctx: dict):
    items = system.startup_items()
    names = ", ".join(str(i["name"]) for i in items[:8])
    many = len(items) >= 8
    return (
        f"{len(items)} apps start automatically with Windows" + (f": {names}." if names else ".") + (" That is a lot and slows start-up." if many else ""),
        {"count": len(items), "items": items, "many": many},
    )


CHECKS = {
    "compare_with_normal": ("Compare current network settings with the last time the internet worked on this PC.", compare_with_normal),
    "check_adapters": ("See whether the Wi-Fi or cable adapter is on and connected.", check_adapters),
    "check_ip_and_router": ("See whether the PC has an address and whether the router answers.", check_ip_and_router),
    "check_internet_reach": ("Try to reach internet addresses by number, which works even when DNS is broken.", check_internet_reach),
    "check_dns": ("See which DNS servers are set and whether they answer. DNS turns website names into addresses.", check_dns),
    "check_proxy": ("See whether a proxy is set and whether pages load with and without it.", check_proxy),
    "check_hosts": ("See whether the hosts file blocks any website names.", check_hosts),
    "test_website": ("Try to open a web page the way a browser would.", test_website),
    "check_disk": ("See how much storage space is free.", check_disk),
    "check_memory": ("See how much memory is free and which apps use the most.", check_memory),
    "check_startup": ("List the apps that start automatically with Windows.", check_startup),
}
NETWORK_CHECKS = [
    "compare_with_normal",
    "check_adapters",
    "check_ip_and_router",
    "check_internet_reach",
    "check_dns",
    "check_proxy",
    "check_hosts",
    "test_website",
]
PC_CHECKS = ["check_disk", "check_memory", "check_startup"]

TITLES = {
    "compare_with_normal": "Comparing with the last time it worked",
    "check_adapters": "Checking the network adapter",
    "check_ip_and_router": "Checking the address and the router",
    "check_internet_reach": "Checking the line to the internet",
    "check_dns": "Checking DNS",
    "check_proxy": "Checking the proxy setting",
    "check_hosts": "Checking the hosts file",
    "test_website": "Opening a test page",
    "check_disk": "Checking storage space",
    "check_memory": "Checking memory",
    "check_startup": "Checking start-up apps",
}


def run_check(name: str, system: System, ctx: dict):
    if name not in CHECKS:
        raise KeyError(name)
    try:
        return CHECKS[name][1](system, ctx)
    except Exception as e:
        return f"This check could not run ({type(e).__name__}: {str(e)[:120]}).", {"error": str(e)[:200]}


def verdict(name: str, data: dict) -> str:
    """'ok', 'bad' or 'info' for one check result. Only used to colour the interface."""
    if "error" in data:
        return "info"
    if name == "compare_with_normal":
        return "bad" if data.get("changes") else ("ok" if data.get("has_baseline") else "info")
    if name == "check_adapters":
        return "ok" if data.get("up") else "bad"
    if name == "check_ip_and_router":
        return "ok" if data.get("ipv4") and not data.get("apipa") and data.get("router_ok") else "bad"
    if name == "check_internet_reach":
        return "ok" if data.get("reachable") else "bad"
    if name == "check_dns":
        return "ok" if data.get("configured_answers") and data.get("system_resolves") else "bad"
    if name == "check_proxy":
        if data.get("enabled"):
            return "ok" if data.get("via_proxy_ok") else "bad"
        return "ok" if data.get("direct_ok") else "info"
    if name == "check_hosts":
        return "bad" if data.get("blocked_names") else "ok"
    if name == "test_website":
        return "ok" if data.get("ok") else "bad"
    if name == "check_disk":
        return "bad" if data.get("low") else "ok"
    if name == "check_memory":
        return "bad" if data.get("low") else "ok"
    if name == "check_startup":
        return "bad" if data.get("many") else "ok"
    return "info"
