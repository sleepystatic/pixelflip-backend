"""
Local scraper test — run with:
    python test_scraper.py

Requires a .env file with at minimum:
    DATABASE_URL=...
    SELENIUM_REMOTE_URL=https://chrome.browserless.io/webdriver
    BROWSERLESS_TOKEN=...   (or embed token in SELENIUM_REMOTE_URL)

Does NOT start Flask or touch the database — just calls the scraper
functions directly and prints what they find.
"""

import os
from dotenv import load_dotenv
load_dotenv()

# Simple console log callback so scraper debug messages print to stdout.
def log(user_id, message, log_type="info"):
    tag = {"info": "   ", "error": "ERR"}.get(log_type, "   ")
    print(f"[{tag}] {message}", flush=True)

# Fake user config — edit these to match what you're testing.
USER_ID = "local_test"
ZIP_CODE = "95210"
SEARCH_RADIUS = 100
SEARCH_TERMS = {
    "gameboy advance sp": {"min": 0, "max": 120},
    "ds lite":            {"min": 0, "max": 80},
}
EXCLUSIONS = []

from scraper_multi_user import (
    scrape_mercari_for_user,
    scrape_offerup_for_user,
)

def run(platform):
    print(f"\n{'='*50}")
    print(f"  Testing: {platform.upper()}")
    print(f"{'='*50}")

    kwargs = dict(
        user_id=USER_ID,
        zip_code=ZIP_CODE,
        search_radius=SEARCH_RADIUS,
        search_terms=SEARCH_TERMS,
        exclusions=EXCLUSIONS,
        ai_enabled=False,
        ai_strictness="balanced",
        debug=True,
        log_callback=log,
    )

    if platform == "mercari":
        results = scrape_mercari_for_user(**kwargs)
    elif platform == "offerup":
        results = scrape_offerup_for_user(**kwargs)

    print(f"\n--- {len(results)} result(s) ---")
    for r in results[:5]:
        print(f"  ${r['price']:.0f}  {r['title'][:60]}")
        print(f"       {r['link']}")
    if len(results) > 5:
        print(f"  ... and {len(results) - 5} more")

if __name__ == "__main__":
    import sys
    platforms = sys.argv[1:] or ["mercari", "offerup"]
    for p in platforms:
        run(p)
