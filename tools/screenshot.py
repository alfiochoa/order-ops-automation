"""Screenshot the dashboard: python -m tools.screenshot http://127.0.0.1:8000 docs/dashboard.png"""
import sys

from playwright.sync_api import sync_playwright

url, out = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1000, "height": 720})
    pg.goto(url)
    pg.wait_for_selector("#t tr")
    pg.screenshot(path=out)
    b.close()
