"""
Local scraper test — run with:
    python test_scraper.py
    python test_scraper.py facebook
    python test_scraper.py mercari offerup

Requires a .env file with keys for the platforms you test (e.g.
SCRAPINGBEE_API_KEY for some flows, BRIGHTDATA_API_KEY for Facebook).

Does NOT start Flask — calls scraper functions directly and prints matches.
Facebook uses Bright Data only (no ScrapingBee). Sync /scrape can take several minutes;
optional: BRIGHTDATA_FB_SCRAPE_READ_TIMEOUT_SEC (default 900), BRIGHTDATA_FB_SCRAPE_CONNECT_TIMEOUT_SEC (default 30).
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
    "ds lite": {"min": 0, "max": 80},
}
EXCLUSIONS = []

from scraper_multi_user import (
    scrape_facebook_for_user,
    scrape_mercari_for_user,
    scrape_offerup_for_user,
)


def run(platform):
    print(f"\n{'=' * 50}")
    print(f"  Testing: {platform.upper()}")
    print(f"{'=' * 50}")

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
    elif platform == "facebook":
        results = scrape_facebook_for_user(**kwargs)
    else:
        raise ValueError(f"unsupported platform: {platform}")
    for r in results[:5]:
        print(f"  ${r['price']:.0f}  {r['title'][:60]}")
        print(f"       {r['link']}")
    if len(results) > 5:
        print(f"  ... and {len(results) - 5} more")


if __name__ == "__main__":
    import sys

    platforms = sys.argv[1:] or ["mercari", "offerup"]
    for p in platforms:
        if p not in ("mercari", "offerup", "facebook"):
            print(f"Unknown platform: {p!r} (use mercari, offerup, facebook)")
            sys.exit(1)
        run(p)