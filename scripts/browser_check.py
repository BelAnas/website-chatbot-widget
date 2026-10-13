"""Drives the widget in a real headless browser, the way a visitor would, and takes screenshots.

It starts the service on a free port with the mock answer writer and a temporary database,
then: opens the demo website, asks a question, checks that the answer streamed in and that a
source link is shown, asks for a quote, fills the contact form, and checks the lead in the
database. It also loads the widget from a page on a different origin, once allowed and once not.

Needs:  pip install -r requirements-dev.txt   and a Chrome or Chromium on the machine.
Run:    python -m scripts.browser_check
"""
from __future__ import annotations

import functools
import http.server
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch_browser(p):  # type: ignore[no-untyped-def]
    """The machine's Chrome if there is one, otherwise the Chromium that Playwright installs."""
    try:
        return p.chromium.launch(channel="chrome")
    except Exception:  # noqa: BLE001
        return p.chromium.launch()


def main() -> int:
    port, foreign_port = free_port(), free_port()
    base = f"http://127.0.0.1:{port}"
    foreign = f"http://localhost:{foreign_port}"      # another origin: different host name and port
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "DB_PATH": f"{tmp}/chat.sqlite3", "LLM_PROVIDER": "mock", "MOCK_DELAY_MS": "25",
           "ADMIN_TOKEN": "browser-check", "ALLOWED_ORIGINS": foreign, "INDEX_DIR": os.environ.get("INDEX_DIR", str(ROOT / ".index"))}
    server = subprocess.Popen([sys.executable, "-m", "chatbot.cli", "serve", "--port", str(port)], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL)
    # A second website on another origin that embeds the widget with one script tag.
    (Path(tmp) / "index.html").write_text(
        f'<!doctype html><title>Another site</title><h1>A site on another origin</h1>'
        f'<script src="{base}/widget.js" data-title="Embedded" data-open="true" defer></script>', encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp)
    handler.log_message = lambda *a: None  # type: ignore[method-assign]
    other_site = http.server.ThreadingHTTPServer(("127.0.0.1", foreign_port), handler)
    threading.Thread(target=other_site.serve_forever, daemon=True).start()

    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, ok))
        print(("PASS  " if ok else "FAIL  ") + name, flush=True)

    try:
        for _ in range(120):
            try:
                if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            print("the service did not start")
            return 1

        with sync_playwright() as p:
            browser = launch_browser(p)
            page = browser.new_page(viewport={"width": 1180, "height": 760})
            page.goto(f"{base}/demo/delivery.html")
            page.get_by_role("button", name="Open chat").click()
            check("the widget opens and shows the greeting", page.locator(".msg.bot").first.inner_text().startswith("Hi!"))
            check("suggested questions are shown", page.locator(".chips button").count() == 3)

            # --- a question, typed like a visitor ---
            page.get_by_label("Your question").fill("How much is express delivery?")
            page.get_by_label("Your question").press("Enter")
            answer = page.locator(".msg.bot").nth(1)
            answer.locator(".src a").first.wait_for(timeout=15000)
            check("the answer contains the price from the website ($79)", "$79" in answer.inner_text())
            link = answer.locator(".src a").first
            check("a source link points to the page section", "/demo/delivery.html#express-delivery" in (link.get_attribute("href") or ""))
            DOCS.mkdir(exist_ok=True)
            page.screenshot(path=str(DOCS / "widget-answer.png"))

            # --- streaming: the text must arrive in more than one piece ---
            page.get_by_label("Your question").fill("What is the warranty on desk frames and motors?")
            lengths: list[int] = []
            page.get_by_label("Your question").press("Enter")
            bubble = page.locator(".msg.bot").nth(2)
            for _ in range(200):
                lengths.append(len(bubble.locator(".text").inner_text()))
                if bubble.locator(".src").count():
                    break
                time.sleep(0.02)
            check(f"the answer grew on screen while streaming ({len(set(lengths))} different lengths seen)", len(set(lengths)) >= 4)

            # --- lead capture ---
            page.get_by_label("Your question").fill("We need 40 desks for a new office. Can I get a quote?")
            page.get_by_label("Your question").press("Enter")
            form = page.locator("form.lead")
            form.wait_for(timeout=15000)
            check("the contact form appears after a request for a quote", form.is_visible())
            form.get_by_role("button", name="Send my details").click()
            form.locator('[data-for="email"]').filter(has_text="valid email").wait_for(timeout=5000)
            check("an empty form is refused with a message per field", "tick the box" in form.inner_text())
            form.get_by_label("Name").fill("Dana Lee")
            form.get_by_label("Work email").fill("dana@northfield.example")
            form.get_by_label("Company (optional)").fill("Northfield Logistics")
            form.get_by_label("I agree to be contacted about my enquiry.").check()
            page.screenshot(path=str(DOCS / "widget-lead-form.png"))
            form.get_by_role("button", name="Send my details").click()
            page.locator(".msg.bot", has_text="Thank you, Dana").wait_for(timeout=5000)
            overview = httpx.get(f"{base}/api/admin/overview", headers={"Authorization": "Bearer browser-check"}).json()
            lead = overview["leads"][0] if overview["leads"] else {}
            check("the lead is stored with the conversation", lead.get("email") == "dana@northfield.example"
                  and lead.get("reason") == "buying_interest" and len(lead.get("transcript", [])) >= 6)

            # --- the conversation survives a page change ---
            page.goto(f"{base}/demo/warranty.html")
            page.locator(".msg.user").first.wait_for(timeout=5000)
            check("the conversation is still there on another page", page.locator(".msg.user").count() == 3)

            # --- HTML in a message is shown as text, not run ---
            page.get_by_label("Your question").fill('<img src=x onerror="window.hacked=1"> hello')
            page.get_by_label("Your question").press("Enter")
            page.locator(".msg.user", has_text="<img").wait_for(timeout=5000)
            page.wait_for_timeout(500)
            check("HTML typed by a visitor is displayed as text and does not run", page.evaluate("window.hacked") is None
                  and page.locator("#site-chat-widget img").count() == 0)

            # --- admin page ---
            admin = browser.new_page(viewport={"width": 1180, "height": 760})
            admin.goto(f"{base}/admin?token=browser-check")
            admin.locator("#leads tbody tr", has_text="dana@northfield.example").wait_for(timeout=5000)
            admin.get_by_role("button", name="Show").first.click()
            admin.screenshot(path=str(DOCS / "admin.png"))
            check("the admin page lists the lead", True)

            # --- the same widget on another website (another origin) ---
            other = browser.new_page(viewport={"width": 900, "height": 700})
            other.goto(foreign)
            other.get_by_label("Your question").fill("Do you deliver on Saturdays?")
            other.get_by_label("Your question").press("Enter")
            other.locator(".msg.bot .src a").first.wait_for(timeout=15000)
            text = other.locator(".msg.bot").nth(1).inner_text()
            check("the widget works from an allowed website on another origin", "Monday to Friday" in text)
            href = other.locator(".msg.bot .src a").first.get_attribute("href") or ""
            check("source links on another origin point back to the indexed site", href.startswith(base))

            # --- mobile width: the panel takes the whole screen ---
            phone = browser.new_page(viewport={"width": 390, "height": 780})
            phone.goto(f"{base}/demo/index.html")
            phone.get_by_role("button", name="Open chat").click()
            box = phone.locator(".panel").bounding_box() or {}
            check("on a phone the panel fills the screen", box.get("width") == 390 and box.get("height", 0) >= 760)
            browser.close()

        # A website that is not on the allow-list is refused by the service.
        r = httpx.post(f"{base}/api/chat", headers={"Origin": "https://not-allowed.example"},
                       json={"session_id": "browser-check-1", "message": "hello"})
        check("a website that is not on the allow-list gets HTTP 403", r.status_code == 403)
    finally:
        server.terminate()
        server.wait(timeout=10)
        other_site.shutdown()

    passed = sum(ok for _, ok in checks)
    print(f"\n{passed}/{len(checks)} browser checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
