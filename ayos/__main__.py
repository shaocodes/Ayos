"""Start Ayos:  python -m ayos            real PC (Windows)
               python -m ayos --sim      simulated PC, safe on any computer
               python -m ayos --selftest run every read-only check once and print the results
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

from .brain import DEFAULT_URL
from .memory import Memory
from .server import App, make_server
from .system import FakeSystem, WindowsSystem

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ayos", description="Ayos: a local AI agent that finds and fixes PC and internet problems, offline.")
    p.add_argument("--sim", action="store_true", help="use a simulated PC instead of this one (changes nothing on this computer)")
    p.add_argument("--port", type=int, default=8020)
    p.add_argument("--model", default="auto", help="model name as the local server knows it, e.g. gemma3:4b (default: pick the best one installed)")
    p.add_argument("--url", default=DEFAULT_URL, help="address of the local model server (default: Ollama on this computer)")
    p.add_argument("--api", choices=["ollama", "openai"], default="ollama", help="'openai' for LM Studio or another OpenAI-compatible local server")
    p.add_argument("--no-open", action="store_true", help="do not open the browser")
    p.add_argument("--selftest", action="store_true", help="run every read-only check once, print the results, and exit")
    p.add_argument("--data", default=None, help="folder for Ayos' memory file")
    args = p.parse_args(argv)

    sim = args.sim
    if not sim and os.name != "nt":
        print("This is not Windows, so Ayos is starting with the simulated PC.")
        sim = True

    if args.selftest:
        from .selftest import run_selftest

        return run_selftest(FakeSystem() if sim else WindowsSystem(), args.model, args.url, args.api)

    data_dir = args.data or os.path.join(ROOT, "data")
    system = FakeSystem() if sim else WindowsSystem()
    memory = Memory(os.path.join(data_dir, "memory_sim.json" if sim else "memory.json"))
    app = App(system, memory, args.model, args.url, args.api)
    try:
        srv = make_server(app, args.port)
    except OSError as e:
        print(f"Could not start on port {args.port} ({e}). Is Ayos already running? Try: python -m ayos --port 8021")
        return 1
    url = f"http://127.0.0.1:{args.port}/"
    print("")
    print("  Ayos is running.")
    print(f"  Open:   {url}")
    print(f"  PC:     {'SIMULATED PC (nothing on this computer is changed)' if sim else 'this computer'}")
    if not sim:
        print(f"  Admin:  {'yes' if system.is_admin() else 'NO - fixes that change network settings will be refused. Use start_ayos.bat.'}")
    print(f"  Model:  local server at {args.url}")
    print("  Stop:   press Ctrl+C in this window")
    print("")
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nAyos stopped.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
