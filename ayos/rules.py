"""Causes Ayos can name, and the safety rules that check a conclusion against the evidence.

The language model proposes a cause. Before any fix is offered, `supports()` confirms that
the checks already run back that cause up. If not, the model is told what is missing.
`infer()` is the same logic run forwards; it drives the built-in fallback when no model is running.
"""
from __future__ import annotations

from .system import is_block_ip

CAUSES = {
    # id: (title, plain explanation, fix id or None, advice when there is no automatic fix)
    "adapter_disabled": (
        "The network adapter is turned off",
        "The Wi-Fi or cable adapter is switched off inside Windows, so this PC is not on any network.",
        "enable_adapter",
        "",
    ),
    "wifi_not_connected": (
        "Not connected to any network",
        "The adapter is on, but it is not joined to a Wi-Fi network or the cable is unplugged.",
        "open_wifi_settings",
        "Pick your Wi-Fi network and connect, or plug the cable back in.",
    ),
    "no_ip_address": (
        "The router did not give this PC an address",
        "The PC is connected but never received an address from the router, so it cannot talk to anything.",
        "renew_ip",
        "",
    ),
    "router_unreachable": (
        "The router is not answering",
        "This PC has an address, but the router does not answer. The problem is between this PC and the router.",
        None,
        "Restart the router: unplug it for 10 seconds, plug it back in, and wait two minutes.",
    ),
    "isp_outage": (
        "The internet line itself is down",
        "This PC and the router are fine, but nothing beyond the router can be reached. The outage is on the provider's side.",
        None,
        "Restart the modem once. If it is still down, it is an outage with your internet provider; nothing on this PC needs fixing.",
    ),
    "dns_misconfigured": (
        "DNS is set to a server that does not answer",
        "Someone or something set the DNS server by hand to an address that does not respond. "
        "The internet line works, but the PC cannot turn website names into addresses, so pages do not open.",
        "reset_dns",
        "",
    ),
    "dns_server_down": (
        "The router's DNS is not answering",
        "DNS is on automatic, but the server the router gave out is not responding. Other DNS servers do respond.",
        "use_public_dns",
        "",
    ),
    "proxy_blocking": (
        "A proxy setting is blocking the browser",
        "Windows is set to send web traffic through a proxy that is not there. Pages load fine without it.",
        "disable_proxy",
        "",
    ),
    "hosts_block": (
        "The hosts file is blocking a website",
        "A line in the Windows hosts file points the website's name at a dead address, so the browser never reaches the real site.",
        "remove_hosts_entry",
        "",
    ),
    "captive_portal": (
        "This Wi-Fi needs you to sign in first",
        "The network is holding every page back until you sign in or accept its terms on its own web page. "
        "The PC is fine; the Wi-Fi is waiting for you.",
        "open_login_page",
        "Open any plain website in your browser and the sign-in page should appear.",
    ),
    "clock_wrong": (
        "The PC's date and time are wrong",
        "Secure websites prove who they are with a certificate that has dates on it. With the wrong date on this PC, "
        "every certificate looks expired or not yet valid, so the browser warns that the connection is not private.",
        "set_clock",
        "",
    ),
    "no_fault_found": (
        "No fault found",
        "Every check passed and a test page loads. The connection is working right now.",
        None,
        "If one website still fails, the problem is probably on that website's side.",
    ),
    "disk_full": (
        "Storage is almost full",
        "The drive is nearly full. Windows needs free space to run smoothly, so the whole PC slows down.",
        "open_storage_settings",
        "Remove files or apps you no longer need, starting with Downloads and the Recycle Bin.",
    ),
    "low_memory": (
        "Memory is nearly full",
        "Open apps are using almost all the memory, so Windows keeps swapping to disk and everything feels slow.",
        None,
        "Close the apps using the most memory, or the browser tabs you are not using.",
    ),
    "many_startup_apps": (
        "Too many apps start with Windows",
        "A lot of apps launch every time the PC starts, which slows start-up and keeps memory busy.",
        "open_startup_settings",
        "Turn off the ones you do not need at start-up.",
    ),
    "battery_saver": (
        "Battery saver is slowing the PC",
        "The charger is unplugged and battery saver is on. Windows holds the processor back on purpose to make the battery last.",
        "open_battery_settings",
        "Plug the charger in, or turn battery saver off when you need speed.",
    ),
    "needs_restart": (
        "Windows needs a restart",
        "This PC has gone a long time without a real restart, or an update is waiting for one. Leftover programs and "
        "half-installed updates slow it down.",
        None,
        "Save your work, then choose Restart from the Start menu. Restart, not Shut down: Shut down keeps part of Windows asleep.",
    ),
    "pc_looks_healthy": (
        "No obvious cause found",
        "Storage, memory, start-up apps, power settings and uptime all look fine.",
        None,
        "If it is slow only sometimes, note what is open when it happens and ask again then.",
    ),
}

NETWORK_CAUSES = [
    "adapter_disabled",
    "wifi_not_connected",
    "no_ip_address",
    "router_unreachable",
    "captive_portal",
    "isp_outage",
    "dns_misconfigured",
    "dns_server_down",
    "proxy_blocking",
    "hosts_block",
    "clock_wrong",
    "no_fault_found",
]
PC_CAUSES = ["disk_full", "low_memory", "many_startup_apps", "battery_saver", "needs_restart", "pc_looks_healthy"]


def _ok(obs: dict, name: str):
    d = obs.get(name)
    return d if d and "error" not in d else None


def supports(cause: str, obs: dict):
    """Does the evidence back this cause? Returns (True, None) or (False, name_of_check_to_run_or_None)."""
    ad, ip, reach = _ok(obs, "check_adapters"), _ok(obs, "check_ip_and_router"), _ok(obs, "check_internet_reach")
    dns, proxy, hosts, web = _ok(obs, "check_dns"), _ok(obs, "check_proxy"), _ok(obs, "check_hosts"), _ok(obs, "test_website")

    # An adapter only counts as working if it has a real address. A second adapter that is switched on
    # but leads nowhere must not hide the fact that the real one is off.
    # "Working" means it has a router. With memory, "switched off" means switched off since the internet last worked.
    routed = bool(ad and ad["routed"])
    off_now = (ad["disabled"] if ad.get("disabled_new") is None else ad["disabled_new"]) if ad else []
    adapter_off = bool(ad and off_now and not routed)
    not_joined = bool(ad and not adapter_off and ad["disconnected"] and not routed and not ad["self_addressed"])
    if cause == "adapter_disabled":
        return (adapter_off, None if ad else "check_adapters")
    if cause == "wifi_not_connected":
        return (not_joined, None if ad else "check_adapters")
    if cause == "no_ip_address":
        if not ip:
            return False, "check_ip_and_router"
        if not ad:
            return False, "check_adapters"  # no address is also what a switched-off adapter looks like; rule that out first
        return (bool((ip["apipa"] or not ip["ipv4"]) and not adapter_off and not not_joined and ad["up"]), None)
    if cause == "router_unreachable":
        return (bool(ip and ip["ipv4"] and not ip["apipa"] and ip["router_ok"] is False), None if ip else "check_ip_and_router")
    login, clock = _ok(obs, "check_login_page"), _ok(obs, "check_clock")
    if cause == "captive_portal":
        return (bool(login and login["portal"]), None if login else "check_login_page")
    if cause == "clock_wrong":
        return (bool(clock and clock["wrong"]), None if clock else "check_clock")
    if cause == "isp_outage":
        if not reach:
            return False, "check_internet_reach"
        if not ip:
            return False, "check_ip_and_router"
        if not login:
            return False, "check_login_page"  # a Wi-Fi sign-in page blocks everything too, and it is not an outage
        if login["portal"]:
            return False, None
        return (not reach["reachable"] and ip["router_ok"] is True, None)  # the router is alive, but nothing beyond it answers
    if cause == "dns_misconfigured":
        if not dns:
            return False, "check_dns"
        dead = bool(dns["servers"]) and not dns["configured_answers"] and dns["manual"]
        if dead and not dns["public_answers"] and not reach:
            return False, "check_internet_reach"  # some networks block outside DNS; the line itself may still be fine
        return (dead and (dns["public_answers"] or bool(reach and reach["reachable"])), None)
    if cause == "dns_server_down":
        if not dns:
            return False, "check_dns"
        return (not dns["configured_answers"] and not dns["manual"] and dns["public_answers"], None)
    if cause == "proxy_blocking":
        if not proxy:
            return False, "check_proxy"
        return (bool(proxy["enabled"] and proxy["via_proxy_ok"] is False and proxy["direct_ok"]), None)
    if cause == "hosts_block":
        if not hosts:
            return False, "check_hosts"
        return (bool(hosts["relevant"]), None)
    if cause == "no_fault_found":
        for need in ("check_adapters", "check_dns", "check_proxy", "check_hosts", "check_login_page", "check_clock", "test_website"):
            if not _ok(obs, need):
                return False, need
        return (bool(web["ok"] and not hosts["relevant"] and not proxy["enabled"] and not login["portal"] and not clock["wrong"]), None)

    disk, mem, start = _ok(obs, "check_disk"), _ok(obs, "check_memory"), _ok(obs, "check_startup")
    if cause == "disk_full":
        return (bool(disk and disk["low"]), None if disk else "check_disk")
    if cause == "low_memory":
        return (bool(mem and mem["low"]), None if mem else "check_memory")
    if cause == "many_startup_apps":
        return (bool(start and start["many"]), None if start else "check_startup")
    batt, up = _ok(obs, "check_battery"), _ok(obs, "check_uptime")
    if cause == "battery_saver":
        return (bool(batt and batt["slowing"]), None if batt else "check_battery")
    if cause == "needs_restart":
        return (bool(up and up["needs"]), None if up else "check_uptime")
    if cause == "pc_looks_healthy":
        for need in ("check_disk", "check_memory", "check_startup", "check_battery", "check_uptime"):
            if not _ok(obs, need):
                return False, need
        return (not disk["low"] and not mem["low"] and not start["many"] and not batt["slowing"] and not up["needs"], None)
    return False, None


# Why a cause the model suggested does not fit, in words the model can use. Only facts already in the results.
def why_not(cause: str, obs: dict) -> str:
    ad, ip, reach = _ok(obs, "check_adapters"), _ok(obs, "check_ip_and_router"), _ok(obs, "check_internet_reach")
    dns, proxy, hosts, web = _ok(obs, "check_dns"), _ok(obs, "check_proxy"), _ok(obs, "check_hosts"), _ok(obs, "test_website")
    if cause in ("adapter_disabled", "wifi_not_connected") and ad:
        return "an adapter is connected and has a router" if ad["routed"] else "the adapter results say something else"
    if cause == "no_ip_address" and ip and ip["ipv4"] and not ip["apipa"]:
        return f"the PC does have an address ({ip['ipv4']})"
    if cause == "router_unreachable" and ip and ip.get("router_ok"):
        return "the router is working"
    if cause == "isp_outage" and reach and reach["reachable"]:
        return "internet addresses can be reached, so the line is up"
    if cause in ("dns_misconfigured", "dns_server_down") and dns:
        if dns["configured_answers"]:
            return "the DNS servers answer"
        return "the DNS servers were set by hand" if cause == "dns_server_down" and dns["manual"] else "the DNS servers are on automatic, not set by hand" if not dns["manual"] else "no DNS server answers at all, not even a public one"
    if cause == "proxy_blocking" and proxy:
        return "no proxy is turned on" if not proxy["enabled"] else "pages fail even without the proxy"
    if cause == "hosts_block" and hosts and not hosts["relevant"]:
        if not hosts["blocked_names"]:
            return "the hosts file blocks nothing"
        return "the hosts file does not block that website" if hosts.get("target") else "the blocked names were already there when the internet worked"
    login, clock = _ok(obs, "check_login_page"), _ok(obs, "check_clock")
    if cause == "captive_portal" and login and not login["portal"]:
        return "no sign-in page is in the way"
    if cause == "clock_wrong" and clock and not clock["wrong"]:
        return "the date and time are right" if clock["skew"] is not None else "the clock could not be compared"
    if cause == "isp_outage" and login and login["portal"]:
        return "the Wi-Fi is showing a sign-in page, which is not an outage"
    if cause == "no_fault_found":
        if login and login["portal"]:
            return "the Wi-Fi is showing a sign-in page"
        if clock and clock["wrong"]:
            return "the PC's date and time are wrong"
        if web and not web["ok"]:
            return "a test page still fails to load"
        if hosts and hosts["relevant"]:
            return "the hosts file blocks a website"
        if proxy and proxy["enabled"]:
            return "a proxy is turned on"
    disk, mem, start = _ok(obs, "check_disk"), _ok(obs, "check_memory"), _ok(obs, "check_startup")
    if cause == "disk_full" and disk and not disk["low"]:
        return "there is enough free storage"
    if cause == "low_memory" and mem and not mem["low"]:
        return "there is enough free memory"
    if cause == "many_startup_apps" and start and not start["many"]:
        return "only a few apps start with Windows"
    batt, up = _ok(obs, "check_battery"), _ok(obs, "check_uptime")
    if cause == "battery_saver" and batt and not batt["slowing"]:
        return "battery saver is not slowing the PC"
    if cause == "needs_restart" and up and not up["needs"]:
        return "Windows was restarted recently"
    if cause == "pc_looks_healthy" and ((disk and disk["low"]) or (mem and mem["low"]) or (start and start["many"]) or (batt and batt["slowing"]) or (up and up["needs"])):
        return "one of the checks did find a problem"
    if cause in PC_CAUSES and any(k in obs for k in ("check_adapters", "check_dns", "check_proxy", "check_hosts", "test_website", "check_internet_reach", "check_ip_and_router")) and not any(k in obs for k in ("check_disk", "check_memory", "check_startup", "check_battery", "check_uptime")):
        return "that cause is about a slow PC, and this is an internet problem"
    if cause in NETWORK_CAUSES and any(k in obs for k in ("check_disk", "check_memory", "check_startup", "check_battery", "check_uptime")) and not any(k in obs for k in ("check_adapters", "check_dns", "check_proxy", "check_hosts", "test_website")):
        return "that cause is about the internet, and this is a slow-PC problem"
    return "the check results do not show it"


def infer(obs: dict, route: str = "network"):
    """Best supported cause so far, or None if more checks are needed."""
    order = PC_CAUSES if route == "pc" else NETWORK_CAUSES
    for cause in order:
        ok, _ = supports(cause, obs)
        if ok:
            # A dead adapter or router explains everything else, so those win as soon as they are seen.
            return cause
    return None


KEY_CHECK = {
    "adapter_disabled": "check_adapters",
    "wifi_not_connected": "check_adapters",
    "no_ip_address": "check_ip_and_router",
    "router_unreachable": "check_ip_and_router",
    "isp_outage": "check_internet_reach",
    "dns_misconfigured": "check_dns",
    "dns_server_down": "check_dns",
    "proxy_blocking": "check_proxy",
    "hosts_block": "check_hosts",
    "disk_full": "check_disk",
    "low_memory": "check_memory",
    "many_startup_apps": "check_startup",
    "captive_portal": "check_login_page",
    "clock_wrong": "check_clock",
    "battery_saver": "check_battery",
    "needs_restart": "check_uptime",
}


def changed_areas(obs: dict) -> list:
    """What the comparison with normal points at, most basic first: [(check_name, what_changed)].

    A switched-off adapter also makes the DNS setting look different, so the adapter comes first.
    """
    changes = " ".join((obs.get("compare_with_normal") or {}).get("changes", [])).lower()
    out = []
    if "adapter" in changes:
        out.append(("check_adapters", "the network adapter"))
    if "dns" in changes:
        out.append(("check_dns", "the DNS setting"))
    if "proxy" in changes:
        out.append(("check_proxy", "the proxy setting"))
    if "hosts" in changes:
        out.append(("check_hosts", "the hosts file"))
    return out


def next_check(obs: dict, route: str = "network", has_baseline: bool = False, prefer=()):
    """Fallback order of checks when no language model is choosing.

    `prefer` lists checks that found the cause on this PC before; they are tried first.
    """
    from .tools import NETWORK_CHECKS, PC_CHECKS

    order = list(PC_CHECKS if route == "pc" else NETWORK_CHECKS)
    if route != "pc" and not has_baseline:
        order.remove("compare_with_normal")
    hinted = []
    if route != "pc" and has_baseline and "compare_with_normal" not in obs:
        hinted.append("compare_with_normal")
    hinted += [name for name, _what in changed_areas(obs)]
    allowed = set(order)
    for name in hinted + [p for p in prefer if p in allowed] + order:
        if name not in obs:
            return name
    return None


def fix_args(cause: str, obs: dict, baseline: dict | None = None) -> dict:
    """Arguments the fix needs, taken from evidence and memory, never from the model's text."""
    if cause == "adapter_disabled":
        ad = obs["check_adapters"]
        # Turn on the one that was on when the internet last worked; failing that, Wi-Fi before cable.
        wifi = {a["name"] for a in ad["adapters"] if a.get("wifi")}
        pool = list(ad.get("disabled_new") or []) or sorted(ad["disabled"], key=lambda n: n not in wifi)
        return {"adapter": pool[0]}
    if cause in ("dns_misconfigured", "dns_server_down"):
        d = obs["check_dns"]
        args = {"adapter": d["adapter"], "previous": d["servers"] if d["manual"] else None}
        # If this PC used hand-set DNS servers back when the internet worked, go back to those.
        was = ((baseline or {}).get("snapshot") or {}).get("dns", {}).get(d["adapter"]) or {}
        if cause == "dns_misconfigured" and was.get("manual") and was.get("servers") and was["servers"] != d["servers"]:
            args["restore_to"] = list(was["servers"])
        return args
    if cause == "proxy_blocking":
        return {"previous": obs["check_proxy"]["server"]}
    if cause == "clock_wrong":
        return {"skew": obs["check_clock"]["skew"]}
    if cause == "hosts_block":
        h = obs["check_hosts"]
        wanted = set(h["relevant"])
        # Only the names that matter are removed. Other blocking lines in the file are the owner's business.
        return {
            "entries": [
                {"ip": e["ip"], "names": [n for n in e["names"] if n in wanted], "demo": bool(e.get("demo"))}
                for e in h["entries"]
                if is_block_ip(e["ip"]) and wanted & set(e["names"])
            ]
        }
    return {}


def evidence_lines(cause: str, obs: dict, summaries: dict) -> list:
    """The check results that matter for this cause, shown to the user as proof."""
    relevant = {
        "adapter_disabled": ["check_adapters"],
        "wifi_not_connected": ["check_adapters"],
        "no_ip_address": ["check_ip_and_router"],
        "router_unreachable": ["check_ip_and_router"],
        "isp_outage": ["check_ip_and_router", "check_internet_reach"],
        "dns_misconfigured": ["check_dns", "check_internet_reach"],
        "dns_server_down": ["check_dns"],
        "proxy_blocking": ["check_proxy"],
        "hosts_block": ["check_hosts"],
        "no_fault_found": ["test_website"],
        "captive_portal": ["check_login_page"],
        "clock_wrong": ["check_clock"],
        "battery_saver": ["check_battery"],
        "needs_restart": ["check_uptime"],
        "disk_full": ["check_disk"],
        "low_memory": ["check_memory"],
        "many_startup_apps": ["check_startup"],
        "pc_looks_healthy": ["check_disk", "check_memory", "check_startup", "check_battery", "check_uptime"],
    }.get(cause, [])
    if "compare_with_normal" in summaries and (obs.get("compare_with_normal") or {}).get("changes"):
        relevant = ["compare_with_normal"] + relevant
    return [summaries[n] for n in relevant if n in summaries]
