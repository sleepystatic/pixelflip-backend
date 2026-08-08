# PixelFlip backend — Docker deploy for Render.
#
# WHY THIS EXISTS
# Mercari is the only scraper that needs REAL Google Chrome: patchright driving
# bundled Chromium clears Cloudflare and then renders zero listings, so Chromium
# is not a substitute. Installing Chrome needs apt and root, which Render's
# NATIVE Python runtime does not provide — hence a container.
#
# The base image is Microsoft's official Playwright image: it already contains
# Chromium, every system library those browsers need, and runs as root, so the
# `--with-deps` / `su: Authentication failure` problem cannot recur.
#
# Keep this tag's version aligned with playwright in requirements.txt (1.61.0).
# A mismatch means the browsers baked into the image are not the build the
# Python package expects, and launches fail with "Executable doesn't exist".
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

# The base image already sets PLAYWRIGHT_BROWSERS_PATH=/ms-playwright.
# IMPORTANT: do NOT set PLAYWRIGHT_BROWSERS_PATH in Render's env for this
# service — it would point the app away from the browsers baked into the image.
# Remove that variable when switching from the native runtime to Docker.

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so edits to application code do not invalidate this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium ships with the base image; Chrome does not. Install it here, where
# we are root, for Mercari. patchright is a Playwright fork with its own
# browser registry, so it needs its own install step.
RUN playwright install chromium \
 && patchright install chrome \
 && patchright install chromium

# Fail the BUILD, not the first scrape. A container that starts without Chrome
# silently returns zero Mercari listings, which is indistinguishable from
# Mercari blocking us and costs hours of proxy debugging.
RUN test -x /opt/google/chrome/chrome \
      || (echo "Google Chrome missing at /opt/google/chrome/chrome — Mercari would return 0 rows" && exit 1) \
 && echo "Chrome present at /opt/google/chrome/chrome"

COPY . .

# Render injects PORT; app.py reads it and defaults to 5000 locally.
ENV PORT=10000
EXPOSE 10000

# Not gunicorn: app.py's __main__ starts the scraper thread and runs the
# duplicate-instance and wrong-interpreter guards. Under gunicorn __main__
# never executes, and more than one worker would start one scraper loop per
# worker — the ghost-scrape failure, multiplied.
CMD ["python", "app.py"]
