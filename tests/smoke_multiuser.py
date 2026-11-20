# -*- coding: utf-8 -*-
"""
Multi-user safety: what has to hold before SCRAPE_MAX_WORKERS can be raised
above 1.

Two concurrent scrapes must not share anything that belongs to one user. This
suite runs two users' scrapes simultaneously in threads and checks isolation of
console output, keyword filters and known_links, plus the worker-count
arithmetic that decides how many can run at all.

Offline — the HTTP layer and the search feeds are faked.
"""
import io
import os
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, body_only_word, bootstrap, fixture_glob

bootstrap()
import scraper_multi_user as S

t = Suite('multi-user')

pages = fixture_glob('mc_item_*.html')
if not pages:
    t.skip('no mc_item_*.html fixture')

HTML_A = io.open(pages[0], encoding='utf-8').read()
ID_A = re.search(r'"ItemDetail:(m\d+)"', HTML_A).group(1)
BODY_A = S._mercari_description_from_item_html(HTML_A, ID_A)

# A second, distinct listing. The body lives in two encodings (verbatim in
# JSON-LD, newline-escaped in __NEXT_DATA__), so substitute both.
ID_B = 'm88888888888'
BODY_B = 'totally different body text with the word walnut in it'
HTML_B = HTML_A.replace(ID_A, ID_B)
HTML_B = HTML_B.replace(BODY_A, BODY_B).replace(
    BODY_A.replace(chr(10), chr(92) + 'n'), BODY_B)
assert S._mercari_description_from_item_html(HTML_B, ID_B) == BODY_B

PAGES = {ID_A: HTML_A, ID_B: HTML_B}
# resolved after the titles are defined, so it cannot collide with them

t.section('1. Worker-count arithmetic (what the box will actually run)')
orig_mem = S._container_memory_mb
os.environ['FB_LEAN_FETCH'] = '1'
for k in ('BROWSER_MAX_CONCURRENT', 'SCRAPE_BROWSERLESS_PEAK_MB',
          'SCRAPE_PEAK_MB_PER_WORKER', 'SCRAPE_MEMORY_RESERVE_MB'):
    os.environ.pop(k, None)
# Browser-free accounting (2026-11-02): steady state launches no browser, and
# live browsers are capped process-wide (BROWSER_MAX_CONCURRENT=1), so ONE
# browser peak is budgeted and each concurrent scrape costs its browser-free
# peak (40MB default): (RAM - 200 reserve - 1 x 206 lean) // 40. The old model
# charged every scrape for a browser, which held a 512MB box to one worker.
expected = {512: 2, 1024: 15, 2048: 41, 4096: 92}
for mb, want in expected.items():
    S._container_memory_mb = lambda m=mb: float(m)
    t.eq(S.memory_capped_workers(), want, '%dMB (lean) -> %d workers' % (mb, want))
# The DOM path's browser peak is 455MB, so budgeting for it leaves far less.
# FB_LEAN_FETCH=1 is still load-bearing for capacity, not just bandwidth.
S._container_memory_mb = lambda: 2048.0
os.environ['FB_LEAN_FETCH'] = '0'
t.eq(S.memory_capped_workers(), 34, '2048MB unlean -> 34 workers (lean gives 41)')
os.environ['FB_LEAN_FETCH'] = '1'
# Budgeting MORE live browsers must LOWER the cap: each one is reserved up front.
S._container_memory_mb = lambda: 1024.0
os.environ['BROWSER_MAX_CONCURRENT'] = '2'
t.eq(S.memory_capped_workers(), 10, '1024MB with 2 browser slots -> 10 workers (1 slot gives 15)')
os.environ.pop('BROWSER_MAX_CONCURRENT', None)
# The browser-free figure is an estimate; production measurement overrides it.
os.environ['SCRAPE_BROWSERLESS_PEAK_MB'] = '80'
t.eq(S.memory_capped_workers(), 7, '1024MB at a measured 80MB/scrape -> 7 workers')
os.environ.pop('SCRAPE_BROWSERLESS_PEAK_MB', None)
# Never zero: refusing to scrape at all is worse than queueing.
S._container_memory_mb = lambda: 220.0
t.ok(S.memory_capped_workers() >= 1, 'a tiny box still reports at least 1 worker')
S._container_memory_mb = lambda: None
t.eq(S.memory_capped_workers(), None, 'unknown RAM -> None (operator setting stands)')
S._container_memory_mb = orig_mem

t.section('2. Two users scraping CONCURRENTLY stay isolated')

LOGS = []          # (uid, message) as the callback saw them
LOG_LOCK = threading.Lock()
FETCHED = []       # (uid_guess, url)


def make_rows(item_id, title):
    return [{'title': title, 'price': 50.0,
             'link': 'https://www.mercari.com/us/item/%s/' % item_id,
             'image_url': 'http://img/x.jpg', 'listed_at': None}]


class FakeItemSession(object):
    def __init__(self, ctx=None):
        pass

    def fetch(self, url):
        with LOG_LOCK:
            FETCHED.append(url)
        for iid, html in PAGES.items():
            if iid in url:
                return html
        return ''

    def close(self):
        pass


# The title must contain the search term, or check_price_threshold rejects the
# row before any keyword filtering happens and the test proves nothing.
TITLE_A = 'Alpha console listing'
TITLE_B = 'Beta console listing'
ROWS_BY_TERM = {
    'alpha': make_rows(ID_A, TITLE_A),
    'beta': make_rows(ID_B, TITLE_B),
}

WORD_A = body_only_word(TITLE_A, BODY_A)
assert WORD_A, 'fixture body shares every word with the title'

_o = {}


def patch():
    _o['s'] = S._MercariItemSession
    _o['api'] = S._mercari_fetch_via_api
    _o['ai'] = S.check_image_with_ai
    _o['act'] = S.is_user_active
    _o['cool'] = S._mercari_in_cooldown
    S._MercariItemSession = FakeItemSession
    S._mercari_fetch_via_api = lambda terms, n: (
        {tt: list(ROWS_BY_TERM[tt]) for tt in terms}, None)
    S.check_image_with_ai = lambda *a, **k: True
    S.is_user_active = lambda *a, **k: True
    S._mercari_in_cooldown = lambda *a, **k: 0


def unpatch():
    S._MercariItemSession = _o['s']
    S._mercari_fetch_via_api = _o['api']
    S.check_image_with_ai = _o['ai']
    S.is_user_active = _o['act']
    S._mercari_in_cooldown = _o['cool']


def log_cb(uid, msg, kind='info'):
    with LOG_LOCK:
        LOGS.append((uid, msg))


os.environ['MERCARI_ITEM_DELAY_SEC'] = '0'
os.environ['MAX_LISTING_AGE_DAYS'] = '3650'

RESULTS = {}


def scrape(uid, term, cfg):
    try:
        RESULTS[uid] = S.scrape_mercari_for_user(
            uid, '95212', 25, {term: cfg}, [], False, 'balanced',
            debug=True, log_callback=log_cb, known_links=set(),
            scan_description=True)
    except Exception as e:                       # pragma: no cover
        RESULTS[uid] = e


# user-A wants the word that is in listing one's body; user-B excludes it.
# If keyword state leaked between threads, these would contaminate each other.
CFG_A = {'min': None, 'max': None, 'exclusions': [], 'includes': [WORD_A]}
CFG_B = {'min': None, 'max': None, 'exclusions': ['walnut'], 'includes': []}

patch()
try:
    ta = threading.Thread(target=scrape, args=('user-AAAA', 'alpha', CFG_A))
    tb = threading.Thread(target=scrape, args=('user-BBBB', 'beta', CFG_B))
    ta.start()
    tb.start()
    ta.join(120)
    tb.join(120)
finally:
    unpatch()

t.ok(not isinstance(RESULTS.get('user-AAAA'), Exception),
     'user A completed without raising')
t.ok(not isinstance(RESULTS.get('user-BBBB'), Exception),
     'user B completed without raising')
t.eq(len(RESULTS.get('user-AAAA') or []), 1,
     "A: kept its listing (body matched A's include)")
t.eq(len(RESULTS.get('user-BBBB') or []), 0,
     "B: dropped its listing (body matched B's exclude)")

# Console isolation. _LOG_STATE is threading.local(), and this is the assertion
# that keeps it that way: if it ever becomes a plain module global, one user's
# lines appear in another's Live Console — which matters a great deal more once
# accounts are shared.
uids = set(u for u, _ in LOGS)
t.eq(uids, {'user-AAAA', 'user-BBBB'}, 'every log line carries a real user id')
a_msgs = [m for u, m in LOGS if u == 'user-AAAA']
b_msgs = [m for u, m in LOGS if u == 'user-BBBB']
t.ok(a_msgs and b_msgs, 'both users produced console output')
t.ok(not any('user-BBBB' in m for m in a_msgs), "A's lines never mention B")
t.ok(not any('user-AAAA' in m for m in b_msgs), "B's lines never mention A")

t.section('3. One user failing must not take the other down')
RESULTS.clear()


def boom(*a, **k):
    raise RuntimeError('simulated platform failure')


patch()
try:
    orig_api = S._mercari_fetch_via_api

    def selective(terms, n):
        if 'beta' in terms:
            boom()
        return {tt: list(ROWS_BY_TERM[tt]) for tt in terms}, None

    S._mercari_fetch_via_api = selective
    ta = threading.Thread(target=scrape, args=('user-AAAA', 'alpha', CFG_A))
    tb = threading.Thread(target=scrape, args=('user-BBBB', 'beta', CFG_B))
    ta.start()
    tb.start()
    ta.join(120)
    tb.join(120)
    S._mercari_fetch_via_api = orig_api
finally:
    unpatch()

t.eq(len(RESULTS.get('user-AAAA') or []), 1,
     "A still succeeded while B's platform blew up")

t.section('4. Known shared state, documented rather than assumed')
# _BYTES is a module global and _reset_bytes() clears it for the whole process,
# so with workers > 1 the bandwidth figure is per-CYCLE, not per-user. The call
# site says so. Asserting it here means the day someone makes it per-user, this
# test tells them the reporting semantics changed.
S._reset_bytes()
S._record_bytes('mercari', 1000)
snap = S._bytes_snapshot()
t.eq(snap.get('mercari'), 1000, 'byte counter records')
S._reset_bytes()
t.eq(S._bytes_snapshot().get('mercari'), None,
     'a second scrape RESETS the shared counter (per-cycle, not per-user)')

t.done()
