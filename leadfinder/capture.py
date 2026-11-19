"""Visible-browser capture for pages a script can't open on its own: bot checks, logins, event apps,
infinite-scroll exhibitor/attendee directories.

YOU drive the browser: log in, solve any 'verify you are human' check, open the list, scroll through it.
The tool snapshots the visible text of every open tab every few seconds and keeps all unique lines.
Close the browser window (or wait for --minutes) to finish. The login is remembered in data/browser-profile/.

No automation-hiding tricks are used: this is your own browsing session, recorded.
"""
import time
from datetime import datetime

from . import config

CHROMIUM_CANDIDATES = ["/usr/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]


def _chromium():
    import os
    for p in CHROMIUM_CANDIDATES:
        if os.path.exists(p):
            return p
    return None  # fall back to Playwright's own browser (needs `playwright install chromium`)


def capture(url, minutes=20, interval=4, label=None):
    from playwright.sync_api import sync_playwright, Error as PWError

    seen, ordered = set(), []
    out = config.RAW / f"capture-{(label or 'page')}-{datetime.now():%Y%m%d-%H%M%S}.md"
    deadline = time.time() + minutes * 60
    urls_seen = []

    def snapshot(ctx):
        added = 0
        for page in ctx.pages:
            try:
                text = page.inner_text("body", timeout=3000)
                if page.url not in urls_seen:
                    urls_seen.append(page.url)
            except PWError:
                continue
            for line in text.splitlines():
                line = " ".join(line.split())
                if line and line not in seen:
                    seen.add(line)
                    ordered.append(line)
                    added += 1
        return added

    def write():
        out.write_text(f"SOURCE: {url}\nVISITED: {' | '.join(urls_seen)}\nCAPTURED: {datetime.now():%Y-%m-%d %H:%M}"
                       f" via visible browser\n\n" + "\n".join(ordered))

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(config.BROWSER_PROFILE), headless=False,
                                                   executable_path=_chromium(), viewport=None)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, timeout=90000)
        print(f"Browser open. Log in / scroll through the list. Close the window when done "
              f"(auto-stops in {minutes} min). Saving to {out}", flush=True)
        while time.time() < deadline:
            try:
                if not ctx.pages:
                    break
                if snapshot(ctx):
                    write()
                time.sleep(interval)
            except PWError:
                break  # window closed
        try:
            ctx.close()
        except PWError:
            pass
    write()
    return {"path": str(out), "lines": len(ordered), "pages_visited": urls_seen}
