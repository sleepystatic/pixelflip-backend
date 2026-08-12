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
CHROME_DIR="${CHROME_DIR:-/opt/render/project/src/.chrome}"
CHROME_BIN="$CHROME_DIR/opt/google/chrome/chrome"

if patchright install chrome; then
    echo "   ✅ Chrome installed via patchright — Mercari fully enabled"
else
    echo "   ℹ️  patchright install chrome failed (it shells out to apt, which"
    echo "   ℹ️  needs root). Falling back to a rootless unpack of the .deb."

    # `dpkg-deb -x` extracts without installing, so no root and no apt. This is
    # the only reason a full Docker migration may not be necessary: Mercari
    # needs REAL Chrome (bundled Chromium clears Cloudflare then renders zero
    # listings), and this puts a real Chrome binary on disk.
    if curl -fsSL -o /tmp/chrome.deb \
         https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
       && mkdir -p "$CHROME_DIR" \
       && dpkg-deb -x /tmp/chrome.deb "$CHROME_DIR" \
       && [ -x "$CHROME_BIN" ]; then
        echo "   ✅ Chrome unpacked to $CHROME_BIN"
        # Confirm it can actually BROWSE, not merely start.
        #
        # This used to check `--dump-dom about:blank`, which needs no network and
        # no TLS — so it passed on a Chrome that could not load a single HTTPS
        # page. dpkg-deb -x unpacks without installing dependencies, and the ones
        # that go missing (libnss3 and friends) are exactly the certificate and
        # crypto libraries. The result was a Chrome that launched, hung on every
        # navigation, and looked identical to Cloudflare blocking us.
        if "$CHROME_BIN" --headless=new --no-sandbox --disable-dev-shm-usage \
             --virtual-time-budget=15000 --dump-dom https://example.com 2>/dev/null \
             | grep -qi 'example domain'; then
            echo "   ✅ Chrome can load HTTPS. SET THIS IN RENDER:"
            echo "        MERCARI_CHROME_PATH=$CHROME_BIN"
        else
            echo "   ⚠️  Chrome unpacked but cannot load an HTTPS page."
            "$CHROME_BIN" --version 2>&1 | head -2 || true
            echo "   ⚠️  Missing shared libraries (dpkg-deb -x does not install them):"
            ldd "$CHROME_BIN" 2>/dev/null | grep -i 'not found' | head -12 \
                || echo "        (ldd unavailable)"
            echo "   ℹ️  Do NOT set MERCARI_CHROME_PATH — leave it unset."
            echo "   ℹ️  Mercari's search API works on patchright's OWN Chromium"
            echo "   ℹ️  (measured: same results as real Chrome), so set:"
            echo "        MERCARI_BUNDLED_CHROMIUM=1"
        fi
    else
        echo "   ⚠️  Rootless Chrome unpack failed. Mercari will return 0 results"
        echo "   ⚠️  until this service runs on the Docker image (see Dockerfile)."
    fi
    patchright install chromium || true
fi

echo "✅ Build complete!"
