"""The local web server behind the Ayos window.

It listens on 127.0.0.1 only, so nothing outside this computer can reach it. Because Ayos
can change Windows settings, every request that does anything must carry a secret token that
only the Ayos page itself knows, and the Host header must be this computer. A random website
open in another tab cannot drive Ayos.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, fixes, sim
from .agent import Session
from .brain import DEFAULT_MODEL, DEFAULT_URL, LocalModelBrain, RuleBrain
from .memory import Memory
from .system import System
from .tools import TEST_URL, snapshot

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

# Small models that follow instructions well, best first. Used when the chosen model is not installed.
PREFERRED = ["gemma3:4b", "qwen2.5:3b", "llama3.2:3b", "phi4-mini", "gemma3n:e2b", "qwen3:4b", "gemma3:1b", "llama3.2:1b"]
NOT_CHAT = ("embed", "bge", "minilm", "nomic", "clip", "whisper")


class App:
    def __init__(self, system: System, memory: Memory, model: str = "auto", url: str = DEFAULT_URL, api: str = "ollama", monitor: bool = True):
        self.system = system
        self.memory = memory
        self.token = secrets.token_urlsafe(24)
        self.wanted_model = model
        self.model_brain = LocalModelBrain(DEFAULT_MODEL if model == "auto" else model, url, api)
        self.rule_brain = RuleBrain()
        self.sessions = {}
        self.current = None
        self.lock = threading.Lock()
        self.internet = None
        self.model_state = {"server_up": False, "installed": False, "models": [], "warm": "no", "warm_seconds": None, "warm_error": None}
        self.force_rules = False
        self.log = []
        self._stop = False
        if monitor:
            threading.Thread(target=self._monitor, daemon=True).start()

    # ------------------------------------------------------------ background
    def refresh_model(self):
        b = self.model_brain
        models = b.list_models()
        chat = [m for m in models if not any(x in m.lower() for x in NOT_CHAT)]
        up = bool(models) or b.server_up()
        installed = any(m == b.model or m.split(":")[0] == b.model for m in models)
        if up and not installed and self.wanted_model == "auto" and chat:
            pick = next((p for p in PREFERRED if p in chat), chat[0])
            b.model = pick
            installed = True
            self.model_state["warm"] = "no"
        self.model_state.update({"server_up": up, "installed": installed, "models": chat})
        if not up or not installed:
            self.model_state["warm"] = "no"
        elif self.model_state["warm"] == "no":
            self.model_state["warm"] = "loading"
            threading.Thread(target=self._warm, daemon=True).start()

    def _warm(self):
        res = self.model_brain.warm_up()
        self.model_state.update(
            {"warm": "yes" if res["ok"] else "failed", "warm_seconds": res["seconds"], "warm_error": res.get("error")}
        )

    def refresh_internet(self):
        try:
            self.internet = bool(self.system.http_get(TEST_URL, use_system_proxy=True, timeout=4.0)["ok"])
        except Exception:
            self.internet = False
        if self.internet and not self.memory.baseline and not self.busy():
            self.save_baseline()

    def save_baseline(self) -> bool:
        """Remember the current settings as 'normal'. Only when nothing looks wrong."""
        try:
            snap = snapshot(self.system)
            demo_block = any(h.get("demo") for h in self.system.hosts_entries())
            if "error" in snap or snap["proxy_server"] == fixes.DEMO_PROXY and snap["proxy_enabled"] or demo_block:
                return False
            self.memory.set_baseline(snap)
            return True
        except Exception:
            return False

    def _monitor(self):
        n = 0
        while not self._stop:
            try:
                if n % 3 == 0:
                    self.refresh_model()
                self.refresh_internet()
            except Exception:
                pass
            n += 1
            time.sleep(4)

    def kick(self):
        """A setting just changed: re-check the internet light now instead of waiting for the next round."""
        self.internet = None
        threading.Thread(target=self.refresh_internet, daemon=True).start()

    def busy(self) -> bool:
        return bool(self.current and self.current.state in ("running", "fixing"))

    # ------------------------------------------------------------ actions
    def pick_brain(self):
        if self.force_rules:
            return self.rule_brain
        if not (self.model_state["server_up"] and self.model_state["installed"]):
            self.refresh_model()
        if self.model_state["server_up"] and self.model_state["installed"]:
            return self.model_brain
        return self.rule_brain

    def ask(self, question: str) -> Session:
        with self.lock:
            if self.current and self.current.state == "running":
                self.current.cancelled = True
            sid = secrets.token_hex(6)
            s = Session(sid, question, self.system, self.pick_brain(), self.memory, on_change=self.kick)
            self.sessions[sid] = s
            self.current = s
            for old in list(self.sessions)[:-20]:
                self.sessions.pop(old, None)
        return s.start()

    def break_it(self, fault_id: str) -> str:
        if self.busy():
            raise RuntimeError("Ayos is in the middle of a check. Wait for it to finish.")
        if not self.memory.baseline:
            self.save_baseline()  # remember normal before we break anything
        msg = sim.apply(self.system, fault_id)
        self.kick()
        return msg

    def restore(self) -> list:
        if self.busy():
            raise RuntimeError("Ayos is in the middle of a check. Wait for it to finish.")
        if getattr(self.system, "simulated", False):
            sim.reset(self.system)
            done = ["Simulated PC put back to healthy."]
        else:
            done = fixes.restore_all(self.system)
        self.kick()
        return done

    def set_model(self, model: str):
        if model == "rules":
            self.force_rules = True
            return
        self.force_rules = False
        self.wanted_model = model
        self.model_brain.model = model
        self.model_brain.last_speed = None
        self.model_state.update({"warm": "no", "installed": False})
        self.refresh_model()

    def status(self) -> dict:
        b = self.model_brain
        ms = self.model_state
        using_model = ms["server_up"] and ms["installed"] and not self.force_rules
        return {
            "version": __version__,
            "simulated": bool(getattr(self.system, "simulated", False)),
            "system_name": self.system.name,
            "admin": bool(self.system.is_admin()),
            "internet": self.internet,
            "model": {
                "name": b.model,
                "api": b.api,
                "url": b.url,
                "server_up": ms["server_up"],
                "installed": ms["installed"],
                "models": ms["models"],
                "warm": ms["warm"],
                "warm_seconds": ms["warm_seconds"],
                "warm_error": ms["warm_error"],
                "speed": b.last_speed,
                "using_model": using_model,
                "forced_rules": self.force_rules,
            },
            "memory": self.memory.public(),
            "faults": sim.fault_list(bool(getattr(self.system, "simulated", False))),
            "session": self.current.public() if self.current else None,
        }


class Handler(BaseHTTPRequestHandler):
    app: App = None  # set by make_server
    server_version = "Ayos"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # ---- helpers
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Ayos-Token") or "", self.app.token)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 20000:
                return {}
            obj = json.loads(self.rfile.read(n).decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except (ValueError, OSError):
            return {}

    # ---- GET
    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        app = self.app
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as f:
                    html = f.read().replace("__AYOS_TOKEN__", app.token)
            except OSError:
                return self._send(500, b"web/index.html is missing", "text/plain")
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self._json(app.status())
        if u.path == "/api/events":
            if not self._token_ok():
                return self._json({"error": "forbidden"}, 403)
            q = parse_qs(u.query)
            s = app.sessions.get((q.get("id") or [""])[0])
            if not s:
                return self._json({"error": "no such session"}, 404)
            try:
                after = max(0, int((q.get("after") or ["0"])[0]))
            except ValueError:
                after = 0
            return self._json({"events": s.since(after), "state": s.state, "pending": s.pending})
        return self._json({"error": "not found"}, 404)

    # ---- POST
    def do_POST(self):
        if not self._host_ok() or not self._token_ok():
            return self._json({"error": "forbidden"}, 403)
        app = self.app
        path = urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/ask":
                q = str(body.get("question") or "").strip()[:500]
                if not q:
                    return self._json({"error": "Type what is wrong first."}, 400)
                s = app.ask(q)
                return self._json({"id": s.id})
            if path in ("/api/approve", "/api/decline", "/api/undo"):
                s = app.sessions.get(str(body.get("id") or ""))
                if not s:
                    return self._json({"error": "no such session"}, 404)
                ok = {"/api/approve": s.approve, "/api/decline": s.decline, "/api/undo": s.undo}[path]()
                return self._json({"ok": bool(ok)})
            if path == "/api/followup":
                s = app.sessions.get(str(body.get("id") or ""))
                if not s:
                    return self._json({"error": "no such session"}, 404)
                return self._json({"ok": bool(s.follow_up(str(body.get("question") or "")))})
            if path == "/api/break":
                return self._json({"ok": True, "message": app.break_it(str(body.get("fault") or ""))})
            if path == "/api/restore":
                return self._json({"ok": True, "done": app.restore()})
            if path == "/api/model":
                app.set_model(str(body.get("model") or "")[:80])
                return self._json({"ok": True})
            if path == "/api/memory/clear":
                app.memory.clear()
                return self._json({"ok": True})
            if path == "/api/memory/baseline":
                return self._json({"ok": app.save_baseline()})
        except PermissionError as e:
            return self._json({"error": str(e)}, 403)
        except KeyError:
            return self._json({"error": "Unknown option."}, 400)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 500)
        return self._json({"error": "not found"}, 404)


def make_server(app: App, port: int = 8020) -> ThreadingHTTPServer:
    handler = type("AyosHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    return srv
