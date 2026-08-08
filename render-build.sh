#!/bin/bash
set -e

echo "📦 Installing Python dependencies..."
pip install -r requirements.txt

echo "🎭 Installing Playwright Chromium (Facebook, OfferUp)..."
playwright install chromium --with-deps

# Mercari needs patchright driving REAL Google Chrome. Bundled Chromium clears
# Cloudflare but renders 0 listings, so it is not a substitute.
# If the Chrome install fails on Render (it needs apt deps), the scraper falls
# back to MERCARI_CHROME_CHANNEL=chromium — Mercari may then return no results,
# but every other platform keeps working.
echo "🕵️  Installing patchright + Google Chrome (Mercari)..."
if patchright install chrome --with-deps; then
    echo "   ✅ Chrome installed — Mercari fully enabled"
else
    echo "   ⚠️  Chrome install failed; falling back to patched Chromium."
    echo "   ⚠️  Set MERCARI_CHROME_CHANNEL=chromium and expect Mercari to be degraded."
    patchright install chromium --with-deps || true
fi

echo "✅ Build complete!"
