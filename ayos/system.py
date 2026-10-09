"""Everything Ayos knows about the computer goes through a System object.

WindowsSystem talks to the real PC. FakeSystem is a simulated PC used for tests,
for rehearsing the interface on any computer, and for checking how well a language
model diagnoses faults without breaking a real machine.
"""
from __future__ import annotations

import atexit
import base64
import concurrent.futures
import json
import os
import queue
import random
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

PUBLIC_DNS = ["1.1.1.1", "8.8.8.8"]
BOGUS_DNS = ["192.0.2.53", "2001:db8::53"]  # documentation-only addresses, never answer
DEMO_TAG = "# ayos-demo"


# ----------------------------------------------------------------- DNS over UDP
def build_dns_query(name: str, qid: int | None = None) -> bytes:
    qid = random.randint(0, 0xFFFF) if qid is None else qid
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.strip(".").split(".")) + b"\x00"
    return header + q + struct.pack(">HH", 1, 1)  # type A, class IN


def _skip_name(buf: bytes, pos: int) -> int:
    while pos < len(buf):
        length = buf[pos]
        if length == 0:
            return pos + 1
        if length & 0xC0 == 0xC0:
            return pos + 2
        pos += 1 + length
    return pos


def parse_dns_response(buf: bytes) -> dict:
    """Return {'answered': bool, 'ip': str | None, 'rcode': int}."""
    if len(buf) < 12:
        return {"answered": False, "ip": None, "rcode": -1}
    _id, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", buf[:12])
    rcode = flags & 0x000F
    pos = 12
    for _ in range(qd):
        pos = _skip_name(buf, pos) + 4
    ip = None
    for _ in range(an):
        pos = _skip_name(buf, pos)
        if pos + 10 > len(buf):
            break
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", buf[pos : pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4 and ip is None:
            ip = ".".join(str(b) for b in buf[pos : pos + 4])
        pos += rdlen
    return {"answered": True, "ip": ip, "rcode": rcode}


def udp_dns_query(name: str, server: str, timeout: float = 2.5, port: int = 53) -> dict:
    """Ask one DNS server directly, bypassing Windows settings and the DNS cache."""
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    started = time.time()
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(build_dns_query(name), (server, port))
            data, _ = s.recvfrom(2048)
        out = parse_dns_response(data)
    except OSError as e:
        out = {"answered": False, "ip": None, "rcode": -1, "error": type(e).__name__}
    out["ms"] = int((time.time() - started) * 1000)
    return out


def _with_timeout(fn, timeout, default=None):
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except Exception:
        return default
    finally:
        pool.shutdown(wait=False)


# ----------------------------------------------------------------- base class
class System:
    simulated = False
    name = "PC"

    def is_admin(self) -> bool:
        raise NotImplementedError

    # network: read
    def adapters(self) -> list:
        raise NotImplementedError

    def ip_config(self) -> list:
        raise NotImplementedError

    def dns_config(self) -> list:
        raise NotImplementedError

    def ping(self, host: str, timeout: float = 1.5) -> bool:
        raise NotImplementedError

    def tcp_reach(self, host: str, port: int, timeout: float = 2.5) -> bool:
        raise NotImplementedError

    def dns_query(self, name: str, server: str, timeout: float = 2.5) -> dict:
        raise NotImplementedError

    def resolve(self, name: str, timeout: float = 5.0):
        raise NotImplementedError

    def proxy_get(self) -> dict:
        raise NotImplementedError

    def hosts_entries(self) -> list:
        raise NotImplementedError

    def http_get(self, url: str, use_system_proxy: bool = True, timeout: float = 6.0) -> dict:
        raise NotImplementedError

    # network: change
    def set_dns(self, adapter: str, servers) -> None:
        raise NotImplementedError

    def enable_adapter(self, adapter: str) -> None:
        raise NotImplementedError

    def disable_adapter(self, adapter: str) -> None:
        raise NotImplementedError

    def flush_dns(self) -> None:
        raise NotImplementedError

    def renew_ip(self) -> None:
        raise NotImplementedError

    def proxy_set(self, enabled: bool, server: str | None = None) -> None:
        raise NotImplementedError

    def hosts_add(self, ip: str, name: str, demo: bool = True) -> None:
        raise NotImplementedError

    def hosts_remove(self, name: str) -> int:
        raise NotImplementedError

    # computer health: read
    def disks(self) -> list:
        raise NotImplementedError

    def memory(self) -> dict:
        raise NotImplementedError

    def top_processes(self, n: int = 5) -> list:
        raise NotImplementedError

    def startup_items(self) -> list:
        raise NotImplementedError

    def open_settings(self, page: str) -> None:
        raise NotImplementedError


# ----------------------------------------------------------------- one long-lived PowerShell
class ShellDown(Exception):
    """The long-lived PowerShell is not usable. The caller falls back to starting one per command."""


class PsShell:
    """Keeps a single PowerShell process open and feeds it commands.

    Starting PowerShell and loading its networking modules costs one to three seconds every time.
    Paying that once instead of once per check is the difference between a diagnosis that takes
    three seconds and one that takes twenty.

    Each command is sent as one ASCII line (the script itself travels as Base64, so names in any
    language survive) and is answered by a marker line, so we always know where the output ends.
    """

    ARGS = ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-InputFormat", "Text", "-Command", "-"]

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.lines = queue.Queue()
        self.n = 0

    def _start(self):
        self.close()
        self.lines = queue.Queue()
        try:
            self.proc = subprocess.Popen(
                self.ARGS,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            self.proc = None
            raise ShellDown(str(e))
        threading.Thread(target=self._pump, args=(self.proc, self.lines), daemon=True).start()

    @staticmethod
    def _pump(proc, lines):
        try:
            for raw in iter(proc.stdout.readline, b""):
                lines.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
        except Exception:
            pass
        lines.put(None)  # the process ended

    @staticmethod
    def wrap(script: str, n: int) -> str:
        b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return (
            "try { $ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
            f"Invoke-Expression ([System.Text.Encoding]::Unicode.GetString([System.Convert]::FromBase64String('{b64}'))); "
            f"'<<<AYOS-OK {n}>>>' }} catch {{ '<<<AYOS-ERR {n}>>>' + "
            "[System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes([string]$_)) }"
        )

    def run(self, script: str, timeout: float = 20.0) -> str:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            self.n += 1
            n = self.n
            try:
                self.proc.stdin.write((self.wrap(script, n) + "\n").encode("ascii"))
                self.proc.stdin.flush()
            except (OSError, ValueError) as e:
                self.close()
                raise ShellDown(str(e))
            out, end = [], time.time() + timeout
            ok_mark, err_mark = f"<<<AYOS-OK {n}>>>", f"<<<AYOS-ERR {n}>>>"
            while True:
                try:
                    line = self.lines.get(timeout=max(0.05, end - time.time()))
                except queue.Empty:
                    self.close()
                    raise ShellDown("PowerShell did not answer in time")
                if line is None:
                    self.close()
                    raise ShellDown("PowerShell stopped")
                if line.startswith(ok_mark):
                    return "\n".join(out).strip()
                if line.startswith(err_mark):
                    try:
                        msg = base64.b64decode(line[len(err_mark):]).decode("utf-8", "replace")
                    except Exception:
                        msg = "PowerShell reported an error"
                    raise RuntimeError(clean_ps_error(msg))
                out.append(line)

    def close(self):
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
            try:
                proc.stdout.close()
            except Exception:
                pass


# ----------------------------------------------------------------- real Windows
class WindowsSystem(System):
    name = "Windows PC"
    HOSTS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers", "etc", "hosts")
    INET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

    # Virtual machines sometimes report no "physical" adapter at all; then every visible adapter is used.
    PS_AD = "$ad=@(Get-NetAdapter -Physical -ErrorAction SilentlyContinue); if ($ad.Count -eq 0) { $ad=@(Get-NetAdapter -ErrorAction SilentlyContinue) }; "
    PS_PREFIX = "$ProgressPreference='SilentlyContinue'; try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}; "
    CACHE_SECONDS = 2.5  # several checks ask for the same lists back to back

    def __init__(self, persistent: bool = True):
        self._cache = {}
        self._shell = PsShell() if persistent else None
        self._shell_failures = 0
        if self._shell:
            atexit.register(self._shell.close)

    def ps_mode(self) -> str:
        return "one PowerShell kept open" if self._shell else "a new PowerShell per command"

    def warm(self) -> None:
        """Start PowerShell and load its networking modules now, so the first real check is fast."""
        try:
            self.adapters()
            self.dns_config()
            self.ip_config()
        except Exception:
            pass

    def _cached(self, key: str, fn):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.CACHE_SECONDS:
            return [dict(x) for x in hit[1]]
        val = fn()
        self._cache[key] = (time.time(), val)
        return [dict(x) for x in val]

    def _changed(self):
        self._cache.clear()

    def _ps(self, script: str, timeout: float = 20.0) -> str:
        if self._shell is not None:
            try:
                return self._shell.run(script, timeout)
            except ShellDown:
                self._shell_failures += 1
                if self._shell_failures >= 2:
                    self._shell = None  # give up on the shared shell for good; the slow way always works
        return self._ps_once(script, timeout)

    def _ps_once(self, script: str, timeout: float = 20.0) -> str:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", self.PS_PREFIX + script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=flags,
        )
        if proc.returncode != 0 and (proc.stderr or "").strip():
            raise RuntimeError(clean_ps_error(proc.stderr))
        return (proc.stdout or "").strip().lstrip("\ufeff")

    def _ps_json(self, script: str, timeout: float = 20.0) -> list:
        # The JSON comes back as Base64 text, so adapter names in any language survive the console's code page.
        wrapped = "$j = & { " + script + " }; if ($j) { [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes([string]$j)) }"
        return decode_ps_json(self._ps(wrapped, timeout))

    @staticmethod
    def _q(s: str) -> str:
        return "'" + str(s).replace("'", "''") + "'"

    def is_admin(self) -> bool:
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def adapters(self) -> list:
        return self._cached('adapters', self._read_adapters)

    def _read_adapters(self) -> list:
        rows = self._ps_json(
            self.PS_AD + "$ad | Select-Object Name,InterfaceDescription,Status,PhysicalMediaType | ConvertTo-Json -Compress"
        )
        out = []
        for r in rows:
            media = str(r.get("PhysicalMediaType") or "")
            name = str(r.get("Name") or "")
            desc = str(r.get("InterfaceDescription") or "")
            if "bluetooth" in (name + " " + desc + " " + media).lower():
                continue  # Bluetooth tethering shows up as a network adapter; it is never the internet line here
            out.append(
                {
                    "name": name,
                    "description": str(r.get("InterfaceDescription") or ""),
                    "status": str(r.get("Status") or "Unknown"),
                    "wifi": "802.11" in media or "wi-fi" in name.lower() or "wireless" in name.lower(),
                }
            )
        return out

    def ip_config(self) -> list:
        return self._cached('ip_config', self._read_ip_config)

    def _read_ip_config(self) -> list:
        try:
            rows = self._ps_json(
                self.PS_AD + "$phys=@($ad | ForEach-Object { $_.Name }); "
                "Get-NetIPConfiguration -ErrorAction SilentlyContinue | Where-Object { $phys -contains $_.InterfaceAlias } | "
                "ForEach-Object { [pscustomobject]@{ adapter=$_.InterfaceAlias; "
                "ipv4=($_.IPv4Address | Select-Object -First 1).IPAddress; "
                "gateway=($_.IPv4DefaultGateway | Select-Object -First 1).NextHop } } | ConvertTo-Json -Compress"
            )
        except RuntimeError:
            rows = []  # while an adapter is going down or coming up, Windows may have nothing to report yet
        return [
            {"adapter": r.get("adapter"), "ipv4": r.get("ipv4") or None, "gateway": r.get("gateway") or None}
            for r in rows
        ]

    def dns_config(self) -> list:
        return self._cached('dns_config', self._read_dns_config)

    def _read_dns_config(self) -> list:
        rows = self._ps_json(
            self.PS_AD + "$ad | ForEach-Object { $a=$_; "
            "$k='\\Parameters\\Interfaces\\' + $a.InterfaceGuid; "
            "$r4=Get-ItemProperty -Path ('HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip' + $k) -ErrorAction SilentlyContinue; "
            "$r6=Get-ItemProperty -Path ('HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip6' + $k) -ErrorAction SilentlyContinue; "
            "$s=@(Get-DnsClientServerAddress -InterfaceAlias $a.Name -ErrorAction SilentlyContinue | "
            "ForEach-Object { $_.ServerAddresses }); "
            "[pscustomobject]@{ adapter=$a.Name; servers=$s; manual4=[string]$r4.NameServer; manual6=[string]$r6.NameServer } } | "
            "ConvertTo-Json -Compress -Depth 4"
        )
        out = []
        for r in rows:
            if "bluetooth" in str(r.get("adapter") or "").lower():
                continue
            servers = r.get("servers") or []
            if isinstance(servers, dict):  # a PowerShell 5 quirk can wrap arrays as {"value": [...], "Count": n}
                servers = servers.get("value") or []
            if isinstance(servers, str):
                servers = [servers]
            servers = [str(x) for x in servers if x and not str(x).lower().startswith("fec0:")]
            manual = bool(str(r.get("manual4") or "").strip()) or bool(str(r.get("manual6") or "").strip())
            out.append({"adapter": r.get("adapter"), "servers": servers, "manual": manual})
        return out

    def ping(self, host: str, timeout: float = 1.5) -> bool:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host],
                capture_output=True,
                text=True,
                timeout=timeout + 3,
                creationflags=flags,
            )
            return "TTL=" in proc.stdout.upper()
        except Exception:
            return False

    def tcp_reach(self, host: str, port: int, timeout: float = 2.5) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def dns_query(self, name: str, server: str, timeout: float = 2.5) -> dict:
        return udp_dns_query(name, server, timeout)

    def resolve(self, name: str, timeout: float = 5.0):
        def go():
            infos = socket.getaddrinfo(name, 80, proto=socket.IPPROTO_TCP)
            return infos[0][4][0] if infos else None

        return _with_timeout(go, timeout)

    def proxy_get(self) -> dict:
        import winreg

        out = {"enabled": False, "server": "", "auto_config_url": ""}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.INET_KEY) as k:
                for field, key in (("enabled", "ProxyEnable"), ("server", "ProxyServer"), ("auto_config_url", "AutoConfigURL")):
                    try:
                        val = winreg.QueryValueEx(k, key)[0]
                        out[field] = bool(val) if field == "enabled" else str(val)
                    except OSError:
                        pass
        except OSError:
            pass
        return out

    def _proxy_refresh(self) -> None:
        try:
            import ctypes

            wininet = ctypes.windll.wininet
            wininet.InternetSetOptionW(0, 39, 0, 0)  # settings changed
            wininet.InternetSetOptionW(0, 37, 0, 0)  # refresh
        except Exception:
            pass

    def proxy_set(self, enabled: bool, server: str | None = None) -> None:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.INET_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enabled else 0)
            if server is not None:
                winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server)
        self._proxy_refresh()

    def hosts_entries(self) -> list:
        return parse_hosts(_read_text(self.HOSTS))

    def hosts_add(self, ip: str, name: str, demo: bool = True) -> None:
        """Add one line. demo=True marks it as a practice line, which "put everything back" removes."""
        self._changed()
        text = _read_text(self.HOSTS)
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"{ip} {name} {DEMO_TAG}\n" if demo else f"{ip} {name}\n"
        with open(self.HOSTS, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(text)
        self.flush_dns()

    def hosts_remove(self, name: str) -> int:
        kept, removed = remove_hosts_name(_read_text(self.HOSTS), name)
        if removed:
            with open(self.HOSTS, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(kept)
            self.flush_dns()
        return removed

    def http_get(self, url: str, use_system_proxy: bool = True, timeout: float = 6.0) -> dict:
        # Name lookups ignore the socket timeout on Windows and can hang for a long time when DNS is dead.
        started = time.time()
        out = _with_timeout(lambda: real_http_get(url, use_system_proxy, timeout), timeout + 1.5)
        if out is None:
            out = {"ok": False, "status": 0, "error": "timed out", "ms": int((time.time() - started) * 1000)}
        return out

    def set_dns(self, adapter: str, servers) -> None:
        self._changed()
        if servers:
            def put(lst):
                joined = ",".join(self._q(x) for x in lst)
                self._ps(f"Set-DnsClientServerAddress -InterfaceAlias {self._q(adapter)} -ServerAddresses ({joined})")

            try:
                put(servers)
            except RuntimeError as e:
                v4 = [x for x in servers if ":" not in x]
                if "administrator" in str(e) or not v4 or len(v4) == len(servers):
                    raise
                put(v4)  # IPv6 is switched off on this adapter
        else:
            self._ps(f"Set-DnsClientServerAddress -InterfaceAlias {self._q(adapter)} -ResetServerAddresses")
        self.flush_dns()

    def enable_adapter(self, adapter: str) -> None:
        self._changed()
        self._ps(f"Enable-NetAdapter -Name {self._q(adapter)} -Confirm:$false", timeout=30)

    def disable_adapter(self, adapter: str) -> None:
        self._changed()
        self._ps(f"Disable-NetAdapter -Name {self._q(adapter)} -Confirm:$false", timeout=30)

    def flush_dns(self) -> None:
        try:
            self._ps("Clear-DnsClientCache")
        except Exception:
            pass

    def renew_ip(self) -> None:
        self._changed()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(["ipconfig", "/renew"], capture_output=True, text=True, timeout=60, creationflags=flags)

    def disks(self) -> list:
        out = []
        for letter in "CDEFGH":
            root = f"{letter}:\\"
            if os.path.exists(root):
                try:
                    u = shutil.disk_usage(root)
                    out.append({"drive": f"{letter}:", "total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1)})
                except OSError:
                    pass
        return out

    def memory(self) -> dict:
        rows = self._ps_json(
            "Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize,FreePhysicalMemory | "
            "ConvertTo-Json -Compress"
        )
        r = rows[0] if rows else {}
        return {
            "total_gb": round(float(r.get("TotalVisibleMemorySize") or 0) / 1048576, 1),
            "free_gb": round(float(r.get("FreePhysicalMemory") or 0) / 1048576, 1),
        }

    def top_processes(self, n: int = 5) -> list:
        rows = self._ps_json(
            "Get-Process | Group-Object ProcessName | ForEach-Object { [pscustomobject]@{ name=$_.Name; "
            "mem_mb=[int](($_.Group | Measure-Object WorkingSet64 -Sum).Sum/1MB) } } | "
            f"Sort-Object mem_mb -Descending | Select-Object -First {int(n)} | ConvertTo-Json -Compress"
        )
        return [{"name": r.get("name"), "mem_mb": int(r.get("mem_mb") or 0)} for r in rows]

    def startup_items(self) -> list:
        rows = self._ps_json(
            "Get-CimInstance Win32_StartupCommand | Select-Object Name,Command | ConvertTo-Json -Compress"
        )
        return [{"name": r.get("Name"), "command": str(r.get("Command") or "")[:120]} for r in rows]

    def open_settings(self, page: str) -> None:
        os.startfile(f"ms-settings:{page}")  # type: ignore[attr-defined]


def decode_ps_json(out: str) -> list:
    """Decode what _ps_json's wrapper printed: Base64 of UTF-8 JSON. Always returns a list."""
    out = (out or "").strip()
    if not out:
        return []
    try:
        text = base64.b64decode(out, validate=True).decode("utf-8")
    except Exception:
        text = out  # plain JSON, just in case
    data = json.loads(text)
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


def clean_ps_error(stderr: str) -> str:
    """Turn PowerShell's error output into one readable line."""
    text = stderr or ""
    if "<Objs" in text:  # PowerShell sometimes wraps errors in XML when its output is captured
        import re as _re

        parts = _re.findall(r'<S S="Error">(.*?)</S>', text, flags=_re.S)
        text = " ".join(p.replace("_x000D__x000A_", " ") for p in parts) or text
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#< CLIXML")]
    first = lines[0] if lines else "PowerShell reported an error"
    if "access is denied" in text.lower() or "requires elevation" in text.lower() or "PermissionDenied" in text:
        return "Windows refused: administrator rights are needed. Start Ayos with start_ayos.bat. (" + first[:160] + ")"
    return first[:300]


def is_block_ip(ip: str) -> bool:
    """Addresses people put in a hosts file to block a site: this PC itself, or 'nowhere'."""
    return ip.startswith("127.") or ip in ("0.0.0.0", "::", "::1")


def _read_text(path: str) -> str:
    for enc in ("utf-8", "mbcs", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return ""
    return ""


def parse_hosts(text: str) -> list:
    out = []
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        parts = body.split()
        if len(parts) < 2:
            continue
        names = [n.lower() for n in parts[1:]]
        if all(n in ("localhost", "localhost.localdomain", "ip6-localhost") for n in names):
            continue
        out.append({"ip": parts[0], "names": names, "demo": DEMO_TAG in line})
    return out


def remove_hosts_name(text: str, name: str):
    """Remove `name` from hosts text. Returns (new_text, lines_changed)."""
    name = name.lower()
    kept, changed = [], 0
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        parts = body.split()
        if len(parts) >= 2 and name in [p.lower() for p in parts[1:]]:
            changed += 1
            rest = [p for p in parts[1:] if p.lower() != name]
            if rest:
                kept.append(parts[0] + " " + " ".join(rest))
            continue
        kept.append(line)
    return "\n".join(kept) + "\n", changed


def real_http_get(url: str, use_system_proxy: bool, timeout: float) -> dict:
    handlers = [] if use_system_proxy else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "Ayos/1.0"})
    started = time.time()
    try:
        with opener.open(req, timeout=timeout) as res:
            res.read(256)
            return {"ok": 200 <= res.status < 400, "status": res.status, "error": "", "ms": int((time.time() - started) * 1000)}
    except urllib.error.HTTPError as e:
        return {"ok": e.code < 500, "status": e.code, "error": "", "ms": int((time.time() - started) * 1000)}
    except Exception as e:
        reason = getattr(e, "reason", e)
        return {"ok": False, "status": 0, "error": str(reason)[:160], "ms": int((time.time() - started) * 1000)}


# ----------------------------------------------------------------- simulated PC
class FakeSystem(System):
    """A pretend Windows PC on a home network. Faults can be switched on and off."""

    simulated = True
    name = "Simulated PC"
    GOOD_DNS = {"192.168.1.1", "1.1.1.1", "8.8.8.8"}

    def __init__(self, admin: bool = True):
        self.admin = admin
        self.adapter = {"name": "Wi-Fi", "description": "Simulated Wireless Adapter", "status": "Up", "wifi": True}
        self.ip = "192.168.1.23"
        self.gateway = "192.168.1.1"
        self.dhcp_dns = ["192.168.1.1"]
        self.manual_dns = None  # list when set by hand
        self.proxy = {"enabled": False, "server": "", "auto_config_url": ""}
        self.hosts = []  # [{'ip','names','demo'}]
        self.router_up = True
        self.isp_up = True
        self.router_dns_up = True
        self.router_pings = True  # False: a router that works but ignores pings, like many public Wi-Fi networks
        self.disk = [{"drive": "C:", "total_gb": 237.0, "free_gb": 61.4}]
        self.mem = {"total_gb": 15.8, "free_gb": 6.2}
        self.procs = [{"name": "chrome", "mem_mb": 1840}, {"name": "Code", "mem_mb": 910}, {"name": "Discord", "mem_mb": 420}]
        self.startup = [{"name": "Discord", "command": "Discord.exe"}, {"name": "OneDrive", "command": "OneDrive.exe"}]
        self.opened = []
        self.calls = []

    def _need_admin(self):
        if not self.admin:
            raise PermissionError("Administrator rights are required for this change.")

    def _link(self) -> bool:
        return self.adapter["status"] == "Up"

    def is_admin(self) -> bool:
        return self.admin

    def adapters(self) -> list:
        return [dict(self.adapter)]

    def ip_config(self) -> list:
        if not self._link():
            return [{"adapter": self.adapter["name"], "ipv4": None, "gateway": None}]
        return [{"adapter": self.adapter["name"], "ipv4": self.ip, "gateway": self.gateway if self.ip and not self.ip.startswith("169.254.") else None}]

    def _servers(self) -> list:
        if not self._link():
            return []
        return list(self.manual_dns if self.manual_dns else self.dhcp_dns)

    def dns_config(self) -> list:
        return [{"adapter": self.adapter["name"], "servers": self._servers(), "manual": bool(self.manual_dns)}]

    def ping(self, host: str, timeout: float = 1.5) -> bool:
        if not self._link() or not self.ip or self.ip.startswith("169.254."):
            return False
        if host == self.gateway:
            return self.router_up and self.router_pings
        return self.router_up and self.isp_up and host in ("1.1.1.1", "8.8.8.8")

    def tcp_reach(self, host: str, port: int, timeout: float = 2.5) -> bool:
        if not self._link() or not self.ip or self.ip.startswith("169.254."):
            return False
        if host == self.gateway:
            return self.router_up  # the router's own web and DNS ports
        return self.router_up and self.isp_up

    def dns_query(self, name: str, server: str, timeout: float = 2.5) -> dict:
        ok = self._link() and self.router_up and server in self.GOOD_DNS
        if ok and server == self.gateway:
            ok = self.router_dns_up and self.isp_up
        elif ok:
            ok = self.isp_up
        return {"answered": bool(ok), "ip": "93.184.216.34" if ok else None, "rcode": 0 if ok else -1, "ms": 18 if ok else int(timeout * 1000)}

    def resolve(self, name: str, timeout: float = 5.0):
        name = name.lower()
        for h in self.hosts:
            if name in h["names"]:
                return h["ip"]
        for s in self._servers():
            if self.dns_query(name, s)["answered"]:
                return "93.184.216.34"
        return None

    def proxy_get(self) -> dict:
        return dict(self.proxy)

    def hosts_entries(self) -> list:
        return [dict(h) for h in self.hosts]

    def http_get(self, url: str, use_system_proxy: bool = True, timeout: float = 6.0) -> dict:
        host = (urlparse(url).hostname or "").lower()
        if use_system_proxy and self.proxy["enabled"]:
            return {"ok": False, "status": 0, "error": "proxy connection refused", "ms": 40}
        ip = self.resolve(host)
        if not ip:
            return {"ok": False, "status": 0, "error": "name could not be resolved", "ms": 900}
        if is_block_ip(ip):
            return {"ok": False, "status": 0, "error": "connection refused", "ms": 30}
        if not self.tcp_reach(ip, 80):
            return {"ok": False, "status": 0, "error": "network unreachable", "ms": 900}
        return {"ok": True, "status": 200, "error": "", "ms": 120}

    def set_dns(self, adapter: str, servers) -> None:
        self._need_admin()
        self.manual_dns = list(servers) if servers else None
        self.calls.append(("set_dns", adapter, servers))

    def enable_adapter(self, adapter: str) -> None:
        self._need_admin()
        self.adapter["status"] = "Up"

    def disable_adapter(self, adapter: str) -> None:
        self._need_admin()
        self.adapter["status"] = "Disabled"

    def flush_dns(self) -> None:
        self.calls.append(("flush_dns",))

    def renew_ip(self) -> None:
        if self._link() and self.router_up:
            self.ip = "192.168.1.23"

    def proxy_set(self, enabled: bool, server: str | None = None) -> None:
        self.proxy["enabled"] = bool(enabled)
        if server is not None:
            self.proxy["server"] = server

    def hosts_add(self, ip: str, name: str, demo: bool = True) -> None:
        self._need_admin()
        self.hosts.append({"ip": ip, "names": [name.lower()], "demo": bool(demo)})

    def hosts_remove(self, name: str) -> int:
        self._need_admin()
        before = len(self.hosts)
        self.hosts = [h for h in self.hosts if name.lower() not in h["names"]]
        return before - len(self.hosts)

    def disks(self) -> list:
        return [dict(d) for d in self.disk]

    def memory(self) -> dict:
        return dict(self.mem)

    def top_processes(self, n: int = 5) -> list:
        return [dict(p) for p in self.procs[:n]]

    def startup_items(self) -> list:
        return [dict(s) for s in self.startup]

    def open_settings(self, page: str) -> None:
        self.opened.append(page)
