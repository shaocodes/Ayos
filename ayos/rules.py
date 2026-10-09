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
    "pc_looks_healthy": (
        "No obvious cause found",
        "Storage, memory and start-up apps all look fine.",
        None,
        "If it is slow only sometimes, note what is open when it happens and ask again then.",
    ),
}

NETWORK_CAUSES = [
    "adapter_disabled",
    "wifi_not_connected",
    "no_ip_address",
    "router_unreachable",
    "isp_outage",
    "dns_misconfigured",
    "dns_server_down",
    "proxy_blocking",
    "hosts_block",
    "no_fault_found",
]
PC_CAUSES = ["disk_full", "low_memory", "many_startup_apps", "pc_looks_healthy"]


def _ok(obs: dict, name: str):
    d = obs.get(name)
    return d if d and "error" not in d else None


def supports(cause: str, obs: dict):
    """Does the evidence back this cause? Returns (True, None) or (False, name_of_check_to_run_or_None)."""
    ad, ip, reach = _ok(obs, "check_adapters"), _ok(obs, "check_ip_and_router"), _ok(obs, "check_internet_reach")
    dns, proxy, hosts, web = _ok(obs, "check_dns"), _ok(obs, "check_proxy"), _ok(obs, "check_hosts"), _ok(obs, "test_website")

    if cause == "adapter_disabled":
        return (bool(ad and not ad["up"] and ad["disabled"]), None if ad else "check_adapters")
    if cause == "wifi_not_connected":
        return (bool(ad and not ad["up"] and not ad["disabled"]), None if ad else "check_adapters")
    if cause == "no_ip_address":
        return (bool(ip and (ip["apipa"] or not ip["ipv4"]) and (not ad or ad["up"])), None if ip else "check_ip_and_router")
    if cause == "router_unreachable":
        return (bool(ip and ip["ipv4"] and not ip["apipa"] and ip["router_ok"] is False), None if ip else "check_ip_and_router")
    if cause == "isp_outage":
        if not reach:
            return False, "check_internet_reach"
        if not ip:
            return False, "check_ip_and_router"
        return (not reach["reachable"] and ip["gateway_ping"] is True, None)  # the router answers, but nothing beyond it does
    if cause == "dns_misconfigured":
        if not dns:
            return False, "check_dns"
        return (bool(dns["servers"]) and not dns["configured_answers"] and dns["manual"] and (dns["public_answers"] or bool(reach and reach["reachable"])), None)
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
        return (bool(hosts["blocked_names"]), None)
    if cause == "no_fault_found":
        for need in ("check_adapters", "check_dns", "check_proxy", "check_hosts", "test_website"):
            if not _ok(obs, need):
                return False, need
        return (bool(web["ok"] and not hosts["blocked_names"] and not proxy["enabled"]), None)

    disk, mem, start = _ok(obs, "check_disk"), _ok(obs, "check_memory"), _ok(obs, "check_startup")
    if cause == "disk_full":
        return (bool(disk and disk["low"]), None if disk else "check_disk")
    if cause == "low_memory":
        return (bool(mem and mem["low"]), None if mem else "check_memory")
    if cause == "many_startup_apps":
        return (bool(start and start["many"]), None if start else "check_startup")
    if cause == "pc_looks_healthy":
        for need in ("check_disk", "check_memory", "check_startup"):
            if not _ok(obs, need):
                return False, need
        return (not disk["low"] and not mem["low"] and not start["many"], None)
    return False, None


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
}


def next_check(obs: dict, route: str = "network", has_baseline: bool = False, prefer=()):
    """Fallback order of checks when no language model is choosing.

    `prefer` lists checks that found the cause on this PC before; they are tried first.
    """
    from .tools import NETWORK_CHECKS, PC_CHECKS

    order = list(PC_CHECKS if route == "pc" else NETWORK_CHECKS)
    if route != "pc" and not has_baseline:
        order.remove("compare_with_normal")
    changes = " ".join((obs.get("compare_with_normal") or {}).get("changes", [])).lower()
    hinted = []
    if route != "pc" and has_baseline and "compare_with_normal" not in obs:
        hinted.append("compare_with_normal")
    if "dns" in changes:
        hinted.append("check_dns")
    if "proxy" in changes:
        hinted.append("check_proxy")
    if "hosts" in changes:
        hinted.append("check_hosts")
    if "adapter" in changes:
        hinted.append("check_adapters")
    allowed = set(order)
    for name in hinted + [p for p in prefer if p in allowed] + order:
        if name not in obs:
            return name
    return None


def fix_args(cause: str, obs: dict, baseline: dict | None = None) -> dict:
    """Arguments the fix needs, taken from evidence and memory, never from the model's text."""
    if cause == "adapter_disabled":
        return {"adapter": obs["check_adapters"]["disabled"][0]}
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
    if cause == "hosts_block":
        h = obs["check_hosts"]
        return {"entries": [e for e in h["entries"] if is_block_ip(e["ip"])]}
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
        "disk_full": ["check_disk"],
        "low_memory": ["check_memory"],
        "many_startup_apps": ["check_startup"],
        "pc_looks_healthy": ["check_disk", "check_memory", "check_startup"],
    }.get(cause, [])
    if "compare_with_normal" in summaries and (obs.get("compare_with_normal") or {}).get("changes"):
        relevant = ["compare_with_normal"] + relevant
    return [summaries[n] for n in relevant if n in summaries]
