# -*- coding: utf-8 -*-
"""
Mercari capture paths, rebuilt 2026-11-20 (original: 19 assertions, lost).

Two halves, no browser and no network in either:

  capture_mercari_token.py (what the GitHub Action runs): exit codes for
    --check, nothing-to-do, --force, a failed capture (stored token untouched),
    a capture that does not read back, and success.
  _mercari_fetch_via_api (what every scrape runs): browserless replay first; on
    a host with MERCARI_ALLOW_CAPTURE=0 (Render) the browser is STRUCTURALLY
    unreachable — it says why and never launches Chrome; the cooldown is
    checked without being re-armed; real Chrome falls back to bundled Chromium;
    a double failure arms the cooldown.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
sys.argv = [sys.argv[0]]                        # no --check / --force at import
import scraper_multi_user as S
import capture_mercari_token as C

t = Suite('mercari-capture')
S._emit = lambda *a, **k: None
C.print = lambda *a, **k: None                  # keep the script's chatter out of the log

CALLS = {'capture': 0, 'persist': 0, 'session': [], 'replay': 0}
GOOD = {'headers': {'authorization': 'Bearer x'}, 'variables': {'q': 1}, 'ext': 'h', 'cookies': {}}


def setup(stored=None, capture=None, readback=None):
    """stored: what the DB holds; capture: (items, err, ctx) the browser yields;
    readback: what the DB holds after persisting."""
    seq = [stored, readback]
    CALLS.update(capture=0, persist=0)

    def load():
        return seq.pop(0) if seq else readback

    def fetch(terms, n):
        CALLS['capture'] += 1
        items, err, ctx = capture
        S._MERCARI_API_CTX = ctx
        return items, err

    S._load_persisted_mercari_ctx = load
    S._mercari_fetch_via_api = fetch
    S._persist_mercari_ctx = lambda ctx: CALLS.__setitem__('persist', CALLS['persist'] + 1)


real = {k: getattr(S, k) for k in ('_load_persisted_mercari_ctx', '_mercari_fetch_via_api',
                                   '_persist_mercari_ctx', '_mercari_api_session',
                                   '_mercari_fetch_browserless', '_mercari_unavailable_reason')}

t.section('1. capture_mercari_token.py exit codes')
saved_url = os.environ.pop('DATABASE_URL', None)
t.eq(C.main(), 2, 'no DATABASE_URL -> exit 2, nothing to store into')
os.environ['DATABASE_URL'] = 'postgresql://offline.invalid/none'   # never connected to: all DB calls stubbed

C.CHECK_ONLY, C.FORCE = True, False
setup(stored=GOOD)
t.ok(C.main() == 0 and CALLS['capture'] == 0, '--check with a usable token -> 0, no browser')
setup(stored=None)
t.ok(C.main() == 1 and CALLS['capture'] == 0, '--check with no usable token -> 1 (the workflow logs it), no browser')

C.CHECK_ONLY = False
setup(stored=GOOD)
t.ok(C.main() == 0 and CALLS['capture'] == 0, 'usable token, no --force -> nothing to do')
C.FORCE = True
setup(stored=GOOD, capture=({'t': [1, 2]}, None, GOOD), readback=GOOD)
t.ok(C.main() == 0 and CALLS['capture'] == 1, '--force captures even with a usable token')
C.FORCE = False

setup(stored=None, capture=({}, 'cloudflare said no', None))
t.ok(C.main() == 1 and CALLS['persist'] == 0, 'capture FAILED -> exit 1 (job goes red, email), stored token untouched')
setup(stored=None, capture=({'t': [1]}, None, GOOD), readback=None)
t.eq(C.main(), 1, 'captured but did not read back from the database -> exit 1')
setup(stored=None, capture=({'t': [1, 2, 3]}, None, GOOD), readback=GOOD)
t.ok(C.main() == 0 and CALLS['persist'] == 1, 'captured, persisted, read back -> exit 0')
if saved_url is not None:
    os.environ['DATABASE_URL'] = saved_url
for k, v in real.items():
    setattr(S, k, v)

t.section('2. Every scrape: replay first, browser only where allowed')


def session(terms, rows, bundled=False):
    CALLS['session'].append(bundled)
    return SESSION_RESULT[bundled]


def replay(terms, rows, ctx):
    CALLS['replay'] += 1
    return REPLAY_RESULT


S._mercari_api_session = session
S._mercari_fetch_browserless = replay
S._mercari_unavailable_reason = lambda: 'access token expired 2026-10-16 (JWT exp passed)'
S._persist_mercari_ctx = lambda ctx: None
S._MERCARI_API_COOLDOWN_UNTIL = 0.0

S._MERCARI_API_CTX = None
S._load_persisted_mercari_ctx = lambda: dict(GOOD)
REPLAY_RESULT = ({'ds lite': [{'x': 1}]}, None)
os.environ['MERCARI_ALLOW_CAPTURE'] = '0'
CALLS['session'] = []
items, err = S._mercari_fetch_via_api(['ds lite'], 5)
t.ok(items and err is None and not CALLS['session'], 'stored token -> browserless replay, no browser at all')

S._MERCARI_API_CTX = None
S._load_persisted_mercari_ctx = lambda: None
items, err = S._mercari_fetch_via_api(['ds lite'], 5)
t.ok(items == {} and 'expired' in (err or '') and not CALLS['session'],
     'MERCARI_ALLOW_CAPTURE=0 and no token -> says WHY, never launches Chrome')

os.environ['MERCARI_ALLOW_CAPTURE'] = '1'
S._MERCARI_API_COOLDOWN_UNTIL = time.time() + 600
until = S._MERCARI_API_COOLDOWN_UNTIL
items, err = S._mercari_fetch_via_api(['ds lite'], 5)
t.ok('cooldown' in (err or '') and not CALLS['session'] and S._MERCARI_API_COOLDOWN_UNTIL == until,
     'in cooldown -> no browser, and the cooldown is NOT re-armed (it once extended itself forever)')

S._MERCARI_API_COOLDOWN_UNTIL = 0.0
os.environ.pop('MERCARI_BUNDLED_CHROMIUM', None)
SESSION_RESULT = {False: ({}, 'chrome failed'), True: ({'ds lite': [{'x': 1}]}, None)}
items, err = S._mercari_fetch_via_api(['ds lite'], 5)
t.ok(items and err is None and CALLS['session'] == [False, True],
     'real Chrome fails -> one retry on bundled Chromium, which works')

CALLS['session'] = []
SESSION_RESULT = {False: ({}, 'chrome failed'), True: ({}, 'chromium failed')}
os.environ['MERCARI_API_COOLDOWN_SEC'] = '900'
items, err = S._mercari_fetch_via_api(['ds lite'], 5)
t.ok('not retrying for 900s' in (err or '') and S._MERCARI_API_COOLDOWN_UNTIL > time.time() + 800,
     'both engines fail -> cooldown armed, so a broken host costs one attempt per 15 min')
t.eq(CALLS['session'], [False, True], '...after exactly one attempt on each engine')

t.done()
