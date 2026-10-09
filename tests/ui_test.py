"""Browser test of the whole interface on the simulated PC.

Needs Playwright (pip install playwright) and a Chromium. Not part of the normal test run.
    python tests/ui_test.py [folder-for-screenshots]
A stand-in model server plays the language model with scripted replies, so the test is repeatable.
"""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright  # noqa: E402
from test_server import FakeModelServer, J  # noqa: E402

from ayos.memory import Memory  # noqa: E402
from ayos.server import App, make_server  # noqa: E402
from ayos.system import FakeSystem  # noqa: E402

SHOTS = sys.argv[1] if len(sys.argv) > 1 else None
failures = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


def start(replies=None, admin=True):
    pc = FakeSystem(admin=admin)
    pc.pace = 0.15
    ms = FakeModelServer(replies or [], delay=0.25) if replies is not None else None
    app = App(pc, Memory(None), url=ms.url if ms else "http://127.0.0.1:9", monitor=False)
    app.refresh_model()
    app.refresh_internet()
    srv = make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stop = threading.Event()

    def monitor():
        while not stop.wait(0.4):
            app.refresh_internet()

    threading.Thread(target=monitor, daemon=True).start()
    return pc, app, ms, srv, f"http://127.0.0.1:{srv.server_address[1]}/", stop


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=True)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        errors = []

        def new_page(url):
            page = browser.new_page(viewport={"width": 1366, "height": 768})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "favicon" not in m.text else None)
            page.goto(url)
            page.wait_for_selector("#benchBtns button")
            return page

        print("1. A model drives the investigation, the user approves, undoes, and asks a follow-up")
        pc, app, ms, srv, url, stop = start(
            [
                J("check_dns", thought="The DNS setting changed, so I will check DNS."),
                J("conclude", "dns_misconfigured", "Your PC was told to ask a DNS server that does not exist. DNS is what turns a website name into an address, so no page can open."),
                json.dumps({"message": "Usually an app, a VPN, or someone changing network settings by hand."}),
            ]
        )
        page = new_page(url)
        check("model shown as running on this PC", "On this PC" in page.inner_text("#pcBody"))
        page.click("#benchBtns button:has-text('Wrong DNS')")
        page.wait_for_selector(".lamp.off")
        check("complaint was pre-filled", "internet" in page.input_value("#q").lower())
        page.click("#askBtn")
        page.wait_for_selector("#approveBtn")
        text = page.inner_text(".ticket")
        check("first step came from memory", "from memory" in text)
        check("model's choice is labelled", "chosen by the AI model" in text)
        check("model's reason is shown", "The DNS setting changed" in text)
        check("model's explanation is shown", "does not exist" in text)
        check("safety check line shown", "Safety check passed" in text)
        check("path marks DNS as the break", page.locator("#pathNodes li.bad").inner_text().strip().endswith("DNS"))
        check("nothing changed before approval", pc.manual_dns is not None)
        shot(page, "1a_model_diagnosis")
        page.click("#approveBtn")
        page.wait_for_selector("#undoBtn")
        check("fixed stamp", page.inner_text(".stamp") == "Fixed")
        check("setting really changed", pc.manual_dns is None)
        page.wait_for_selector(".lamp.on")
        check("memory panel lists the incident", "fixed" in page.inner_text("#memBody"))
        page.fill("#fq", "why did this happen?")
        page.click("#followBtn")
        page.wait_for_selector("#thread .a:not(.muted)")
        check("follow-up answered", "VPN" in page.inner_text("#thread"))
        shot(page, "1b_fixed_followup")
        page.click("#undoBtn")
        page.wait_for_function("document.querySelector('.stamp') && document.querySelector('.stamp').textContent === 'Undone'")
        check("undo put the fault back", pc.manual_dns is not None)
        page.click("#restoreBtn")
        page.wait_for_selector(".lamp.on")
        check("restore cleared it", pc.manual_dns is None)
        page.close(); stop.set(); srv.shutdown(); ms.close()

        print("2. The safety check refuses a wrong conclusion")
        pc, app, ms, srv, url, stop = start([J("conclude", "isp_outage", "Your provider is down.", thought="Probably the provider.")] * 4)
        page = new_page(url)
        page.click("#benchBtns button:has-text('Fake proxy')")
        page.wait_for_selector(".lamp.off")
        page.click("#askBtn")
        page.wait_for_selector("#approveBtn")
        text = page.inner_text(".ticket")
        check("refusal row is shown", "Safety check refused a conclusion" in text)
        check("final cause is the proxy", "proxy" in page.inner_text("#outcome h2").lower())
        check("outcome says the model was corrected", "first answer was refused" in text)
        shot(page, "2_guard")
        page.close(); stop.set(); srv.shutdown(); ms.close()

        print("3. No model running: built-in rules, clearly labelled")
        pc, app, ms, srv, url, stop = start(None)
        page = new_page(url)
        check("says no model is running", "No local language model is running" in page.inner_text("#pcBody"))
        page.click("#benchBtns button:has-text('Block example.com')")
        page.fill("#q", "I can't open example.com")
        page.click("#askBtn")
        page.wait_for_selector("#approveBtn")
        check("rules labelled", "built-in rules" in page.inner_text(".ticket"))
        check("no follow-up box without a model", page.is_hidden("#follow"))
        page.click("button:has-text('Leave it')")
        page.wait_for_function("document.querySelector('#ledger').innerText.includes('No change was made')")
        check("declining changes nothing", len(pc.hosts) == 4)
        page.fill("#q", "My laptop is very slow")
        page.click("#askBtn")
        page.wait_for_selector(".stamp")
        check("healthy PC gets an all-clear", page.inner_text(".stamp") == "All clear")
        check("PC path is shown for a slow-PC question", "Storage" in page.inner_text("#pathNodes"))
        shot(page, "3_rules_pc")
        page.close(); stop.set(); srv.shutdown()

        print("4. General question, and a PC without administrator rights")
        pc, app, ms, srv, url, stop = start([J("answer", message="DNS is like a phone book. It turns a name such as google.com into the number computers use.")], admin=False)
        page = new_page(url)
        check("admin warning shown", "start_ayos.bat" in page.inner_text("#pcBody"))
        check("admin-only faults are disabled", page.is_disabled("#benchBtns button:has-text('Wrong DNS')"))
        page.click("#picks button:has-text('What is DNS?')")
        page.wait_for_selector("#outcome h2")
        check("answer shown", "phone book" in page.inner_text("#outcome"))
        shot(page, "4_answer_noadmin")
        page.set_viewport_size({"width": 400, "height": 800})
        check("no sideways scroll on a phone-width window", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
        shot(page, "4b_narrow")
        page.close(); stop.set(); srv.shutdown(); ms.close()

        check("no JavaScript errors", not errors)
        if errors:
            print(errors)
        browser.close()
    print("\nRESULT:", "all passed" if not failures else f"{len(failures)} failed: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
