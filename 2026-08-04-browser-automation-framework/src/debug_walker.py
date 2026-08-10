"""
Isolated diagnostic for the shadow-DOM walker — no LLM, no scenario file,
no retries. Just: open a page, run the exact same candidate detection the
real pipeline uses, and print/show what it actually sees.

This exists because "0 candidates found" on a page that visibly has
content (per a failure screenshot) is ambiguous from the JSON summary alone
— it could be a walker bug, a page that hasn't rendered yet, or the site
quietly serving something other than what it looks like at a glance (a
captcha/interstitial can look "blank-ish" without close inspection). This
script isolates the one step in question so there's nothing else to guess
about.

Usage:
    python debug_walker.py https://www.amazon.in/
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from shadow_walker import get_candidates


def main(url: str) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page()

        print(f"Navigating to {url} ...")
        page.goto(url, wait_until="load", timeout=30_000)
        page.wait_for_timeout(3000)  # let client-side rendering settle

        print(f"Page title : {page.title()!r}")
        print(f"Page URL   : {page.url!r}")

        candidates = get_candidates(page)
        print(f"Candidates found: {len(candidates)}")
        for c in candidates[:15]:
            print(
                f"  [{c.index}] <{c.tag}> text={c.text!r} role={c.role!r} "
                f"selector={c.selector!r} in_shadow={c.in_shadow} frame={c.frame}"
            )

        screenshot_path = "debug_walker_screenshot.png"
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"Screenshot saved to: {screenshot_path}")

        print("\nBrowser window is still open — look at it now.")
        input("Press Enter here once you've checked it, to close the browser...")
        browser.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "https://www.amazon.in/"
    main(target)
