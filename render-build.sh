#!/usr/bin/env bash
set -e

# ---------------------------------------------------------------------------
# Render build. Set the Build Command to:  bash render-build.sh
#
# Two things previously left the deployed service with no browsers at all,
# which showed up as every scraper except Craigslist returning 0 rows (only
# Craigslist needs nothing but `requests`):
#
#   1. `--with-deps` shells out to apt-get, which needs root. Render's NATIVE
#      Python runtime has no root, so it fails — and with `set -e` that aborts
#      the whole build. Playwright's own Ubuntu image already carries those
#      system libraries, so the flag is unnecessary here.
#
#   2. Browsers default to $HOME/.cache/ms-playwright. Install them under the
#      repo instead, via PLAYWRIGHT_BROWSERS_PATH, so build and runtime agree
#      on the location. Set the SAME value in Render's Environment tab or the
#      runtime will look somewhere the build never wrote to.
# ---------------------------------------------------------------------------

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/render/project/src/.playwright}"
echo "📁 Browser install path: $PLAYWRIGHT_BROWSERS_PATH"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"

echo "📦 Installing Python dependencies..."
pip install -r requirements.txt

echo "🎭 Installing Playwright Chromium (Facebook, OfferUp, Mercari fallback)..."
playwright install chromium

# Fail the BUILD rather than the first scrape. A deploy that starts with no
# browser looks identical to the marketplaces blocking us, and costs an hour
# of debugging proxies and selectors before anyone checks the build log.
echo "🔎 Verifying the browser binary actually landed..."
if ! find "$PLAYWRIGHT_BROWSERS_PATH" -type f \
        \( -name 'headless_shell' -o -name 'chrome-headless-shell' -o -name 'chrome' \) \
        -print -quit | grep -q .; then
    echo "❌ No Chromium binary under $PLAYWRIGHT_BROWSERS_PATH after install."
    echo "   Every browser scraper would silently return 0 rows. Failing the build."
    ls -la "$PLAYWRIGHT_BROWSERS_PATH" || true
    exit 1
fi
echo "   ✅ Chromium present"

# Mercari needs patchright driving REAL Google Chrome. Bundled Chromium clears
# Cloudflare but then renders 0 listings, so it is NOT a substitute — see the
# note printed below if this step fails.
echo "🕵️  Installing patchright + Google Chrome (Mercari)..."
if patchright install chrome; then
    echo "   ✅ Chrome installed — Mercari fully enabled"
else
    echo "   ⚠️  Chrome install failed (expected on Render's native runtime: it"
    echo "   ⚠️  needs apt/root). Mercari will return 0 results until this service"
    echo "   ⚠️  runs on a Docker image with Chrome preinstalled."
    echo "   ⚠️  Set MERCARI_CHROME_CHANNEL=chromium to stop it retrying."
    patchright install chromium || true
fi

echo "✅ Build complete!"
