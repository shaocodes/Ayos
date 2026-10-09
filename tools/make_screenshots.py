"""Take the README screenshots from the replay page (docs/index.html). Needs Playwright.

    python tools/make_screenshots.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "img")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1.5)
        page.goto("file://" + os.path.join(ROOT, "docs", "index.html"))
        page.wait_for_selector("#benchBtns button")
        page.add_style_tag(content=".replay-note { display: none; }")
        page.click("#benchBtns button:has-text('Wrong DNS')")
        page.wait_for_selector(".lamp.off")
        page.click("#askBtn")
        page.wait_for_selector("#approveBtn", timeout=120000)
        page.wait_for_timeout(600)
        page.locator(".ticket").screenshot(path=os.path.join(OUT, "diagnosis.png"))
        page.click("#approveBtn")
        page.wait_for_selector("#undoBtn", timeout=60000)
        page.wait_for_selector(".lamp.on")
        page.wait_for_timeout(600)
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=os.path.join(OUT, "fixed.png"), full_page=False)
        browser.close()
    print("saved", os.listdir(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
