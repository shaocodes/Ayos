"""Start Resolv:  python -m ayos            real PC (Windows)
               python -m ayos --sim      simulated PC, safe on any computer
               python -m ayos --selftest run every read-only check once and print the results
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

from .brain import DEFAULT_URL
from .memory import Memory
from .server import App, make_server
from .system import FakeSystem, WindowsSystem

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if getattr(sys, "frozen", False):
    ROOT = os.path.dirname(os.path.abspath(sys.executable))  # packaged as Resolv.exe: keep memory next to the program


def find_app_browser(env=None, exists=os.path.isfile, which=shutil.which):
    """A browser that can show a page as its own window, with no tabs and no address bar.

    Microsoft Edge is part of Windows 10 and 11, so this is found on nearly every PC. Returns "" if there is none.
    """
    env = os.environ if env is None else env
    folders = [env.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
    for tail in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
        for folder in folders:
            if folder and exists(os.path.join(folder, tail)):
                return os.path.join(folder, tail)
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser"):
        found = which(name)
        if found:
            return found
    return ""


def window_command(exe: str, url: str, data_dir: str) -> list:
    # Its own profile folder: a clean window with no extensions, separate from the owner's everyday browser.
    return [exe, f"--app={url}", f"--user-data-dir={os.path.join(data_dir, 'window')}", "--window-size=1240,880",
            "--no-first-run", "--no-default-browser-check"]


def open_window(url: str, data_dir: str, plain_browser: bool = False, elevated: bool = False, loaded=lambda: False,
                popen=subprocess.Popen, wait: float = 12.0) -> str:
    """Show Resolv in its own desktop window. Falls back to a normal browser tab. Returns which way worked.

    Resolv runs as administrator, and a browser started by an administrator shows a warning bar. So on Windows the window
    is first started through Explorer, which runs it as the normal user. `loaded` tells us whether the page really opened.
    """
    exe = "" if plain_browser else find_app_browser()
    if exe and elevated and os.name == "nt":
        try:
            folder = os.path.join(data_dir, "window")
            os.makedirs(folder, exist_ok=True)
            script = os.path.join(folder, "open_window.cmd")
            line = "start \"\" " + " ".join('"' + part.replace("%", "%%") + '"' for part in window_command(exe, url, data_dir))
            with open(script, "w", encoding="utf-8") as f:
                f.write("@echo off\r\n" + line + "\r\n")
            popen(["explorer.exe", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if loaded():
                    return "window"
                time.sleep(0.25)
        except OSError:
            pass
    if exe:
        try:
            popen(window_command(exe, url, data_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "window (direct)"
        except OSError:
            pass
    webbrowser.open(url)
    return "browser"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ayos", description="Resolv: a local AI agent that finds and fixes PC and internet problems, offline.")
    p.add_argument("--sim", action="store_true", help="use a simulated PC instead of this one (changes nothing on this computer)")
    p.add_argument("--port", type=int, default=8020)
    p.add_argument("--model", default="auto", help="model name as the local server knows it, e.g. gemma3:4b (default: pick the best one installed)")
    p.add_argument("--url", default=DEFAULT_URL, help="address of the local model server (default: Ollama on this computer)")
    p.add_argument("--api", choices=["ollama", "openai"], default="ollama", help="'openai' for LM Studio or another OpenAI-compatible local server")
    p.add_argument("--no-open", action="store_true", help="do not open a window")
    p.add_argument("--browser", action="store_true", help="open in a normal browser tab instead of its own window")
    p.add_argument("--selftest", action="store_true", help="run every read-only check once, print the results, and exit")
    p.add_argument("--data", default=None, help="folder for Resolv's memory file")
    args = p.parse_args(argv)

    sim = args.sim
    if not sim and os.name != "nt":
        print("This is not Windows, so Resolv is starting with the simulated PC.")
        sim = True

    if args.selftest:
        from .selftest import run_selftest

        return run_selftest(FakeSystem() if sim else WindowsSystem(), args.model, args.url, args.api)

    data_dir = args.data or os.path.join(ROOT, "data")
    system = FakeSystem() if sim else WindowsSystem()
    if sim:
        system.pace = 0.7
    memory = Memory(os.path.join(data_dir, "memory_sim.json" if sim else "memory.json"))
    app = App(system, memory, args.model, args.url, args.api)
    try:
        srv = make_server(app, args.port)
    except OSError as e:
        print(f"Could not start on port {args.port} ({e}). Is Resolv already running? Try: python -m ayos --port 8021")
        return _pause_if_packaged(1)
    url = f"http://127.0.0.1:{args.port}/"
    print("")
    print("  Resolv is running.")
    print(f"  Window: opens by itself. If it does not, open {url} in a browser.")
    print(f"  PC:     {'SIMULATED PC (nothing on this computer is changed)' if sim else 'this computer'}")
    if not sim:
        print(f"  Admin:  {'yes' if system.is_admin() else 'NO - fixes that change network settings will be refused. Use start_ayos.bat.'}")
    print(f"  Model:  local server at {args.url}")
    print("  Stop:   press Ctrl+C in this window")
    print("")
    if not args.no_open:
        def show():
            how = open_window(url, data_dir, args.browser, elevated=system.is_admin(), loaded=lambda: app.page_loads > 0)
            print(f"  Opened: {how}", flush=True)

        threading.Timer(0.8, show).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nResolv stopped.")
    finally:
        srv.server_close()
    return 0


def _pause_if_packaged(code: int) -> int:
    """Double-clicked programs close their window at once; keep it open long enough to read a message."""
    if getattr(sys, "frozen", False) and code != 0:
        try:
            input("\nPress Enter to close this window.")
        except EOFError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
