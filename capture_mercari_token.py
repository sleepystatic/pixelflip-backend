"""
Mint Mercari's search bearer and store it in the database. Run it ANYWHERE.

    python capture_mercari_token.py          # capture only if the stored one is stale
    python capture_mercari_token.py --force  # capture regardless
    python capture_mercari_token.py --check  # report status, capture nothing

WHY THIS IS A SEPARATE SCRIPT
Minting the token is the single most expensive thing the scraper does: it has to
load a real Mercari search page so the app's JavaScript issues the request whose
`authorization` header we need. Measured 2026-08-13 at 1,071MB peak across 11
Chrome processes — which does not fit in a 512MB web instance, and takes the
Flask process down with it when it tries.

Replaying that token, by contrast, needs no browser at all: only a client that
reproduces Chrome's TLS handshake (curl_cffi). Measured on the same day, the
replay costs 4.2MB and zero browser processes.

So the split is: mint here, on a machine with memory, on a schedule. Scrape
there, on the small box, all week. Three facts make that safe, all measured:

  * the token is a JWT with a 10,080-minute (7 day) `exp`
  * the replay needs NO cookies — not cf_clearance, not __cf_bm, nothing
  * it works from an IP different to the one that captured it

Good places to run this: your laptop via Task Scheduler/cron, or a scheduled CI
job (a GitHub Actions runner has ~7GB of RAM and costs nothing on a weekly cron).
It needs DATABASE_URL and, if you use one, MERCARI_PROXY_URL.

Exits non-zero when a capture was needed and failed, so a scheduler can alert.
"""
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import scraper_multi_user as s

FORCE = '--force' in sys.argv
CHECK_ONLY = '--check' in sys.argv


def describe(ctx, stamp_age=None):
    exp = s._mercari_token_expiry(ctx)
    if not exp:
        return 'stored token present, but its expiry is unreadable'
    left = exp - time.time()
    when = datetime.datetime.fromtimestamp(exp, datetime.timezone.utc)
    return (f'expires {when:%Y-%m-%d %H:%M} UTC '
            f'({left / 86400:.2f} days / {left / 3600:.1f} hours from now)')


def main():
    if not os.getenv('DATABASE_URL'):
        print('DATABASE_URL is not set — nothing to store the token in.')
        return 2

    existing = s._load_persisted_mercari_ctx()
    if existing:
        print(f'stored context is usable: {describe(existing)}')
        if CHECK_ONLY:
            return 0
        if not FORCE:
            print('nothing to do. Use --force to capture anyway.')
            return 0
    else:
        print('no usable stored context (absent, expired, or unreadable).')
        if CHECK_ONLY:
            return 1

    print('\nlaunching a browser to mint a fresh token...')
    t0 = time.time()
    # Go through the normal session so the capture uses the same launch flags,
    # clean User-Agent and proxy the scraper itself would use. Asking for a term
    # we do not care about keeps this honest: we want the header, not the rows.
    items, err = s._mercari_fetch_via_api(['ds lite'], 5)
    elapsed = time.time() - t0

    ctx = s._MERCARI_API_CTX
    if err or not ctx:
        print(f'FAILED after {elapsed:.1f}s: {err!r}')
        print('The stored token (if any) is untouched, so scraping continues on it.')
        return 1

    # _mercari_fetch_via_api persists on capture; make it explicit and verify by
    # reading it back rather than trusting the write.
    s._persist_mercari_ctx(ctx)
    stored = s._load_persisted_mercari_ctx()
    if not stored:
        print('captured, but the token did not read back from the database.')
        return 1

    n = sum(len(v) for v in (items or {}).values())
    print(f'captured in {elapsed:.1f}s ({n} listings came back as a side effect)')
    print(f'stored to scraper_state[{s._mercari_ctx_state_key()!r}]')
    print(f'  {describe(stored)}')
    print('\nEvery instance will pick this up on its next Mercari fetch, with no '
          'browser of its own.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
