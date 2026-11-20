# -*- coding: utf-8 -*-
"""Facebook description scanning (handoff 1.1a). Offline: parses committed fixtures."""
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, body_only_word, bootstrap, fixture_glob

bootstrap()
os.environ['FB_TERM_DELAY_SEC'] = '0'
os.environ['FB_ITEM_DELAY_SEC'] = '0'
os.environ['FB_LEAN_FETCH'] = '1'
# The suite's end-to-end tests drive the BROWSER fallback with a fake
# session. Browserless is tested separately in section 6 with its own
# fake; left on here it would make these tests hit the network.
os.environ['FB_BROWSERLESS'] = '0'
import scraper_multi_user as S

t = Suite('facebook-description')

PAGES = {}
for p in fixture_glob('fb_item_*.html'):
    PAGES[os.path.basename(p)[len('fb_item_'):-len('.html')]] = io.open(p, encoding='utf-8').read()
if len(PAGES) < 2:
    t.skip('need at least 2 fb_item_*.html fixtures, found %d' % len(PAGES))

IDS = sorted(PAGES)

t.section('1. Extractor against real saved item pages')
BODIES = {}
for iid in IDS:
    d = S._fb_description_from_item_html(PAGES[iid], iid)
    BODIES[iid] = d
    t.ok(d, '%s: description extracted' % iid)
    print("  %s: %r" % (iid, (d or '')[:110]))
    # id pinning must fail closed rather than attribute a stranger's text
    t.eq(S._fb_description_from_item_html(PAGES[iid], '9999999999999'), None,
         '%s: wrong listing_id -> None' % iid)

t.eq(S._fb_description_from_item_html('', IDS[0]), None, 'empty html -> None')
t.eq(S._fb_description_from_item_html(None, IDS[0]), None, 'None html -> None')

# The trap this function exists for: an item page carries ~21 listings (the one
# requested plus a related-items rail) and the target's own id appears MORE THAN
# ONCE — on a compact card with no description, and on the full PDP object that
# has one. Anchoring on the title key and taking the first id match returns the
# card, so the body reads as missing. Measured on two live pages.
iid = IDS[0]
first = None
for m in re.finditer(re.escape(S._FB_JSON_TITLE_KEY) + r'\s*:', PAGES[iid]):
    blob = S._fb_json_object_at(PAGES[iid], m.start())
    if not blob:
        continue
    try:
        obj = json.loads(blob)
    except Exception:
        continue
    if isinstance(obj, dict) and str(obj.get('id') or '') == iid:
        first = obj.get('redacted_description')
        break
t.ok(first is None, 'title-key + first-id-match yields NO description (documents the trap)')

t.section('2. Link/url helpers')
real = ('https://www.facebook.com/marketplace/item/1640411724309419/'
        '?ref=search&referral_code=null&referral_story_type=post&__tn__=!%3AD')
t.eq(S._fb_listing_id_from_link(real), '1640411724309419', 'id from the real feed link format')
t.eq(S._fb_listing_id_from_link('https://www.facebook.com/marketplace/item/123/'), '123', 'bare url')
t.eq(S._fb_listing_id_from_link('https://example.com/x'), None, 'non-marketplace -> None')
t.eq(S._fb_listing_id_from_link(''), None, 'empty -> None')
t.eq(S._fb_listing_id_from_link(None), None, 'None -> None')
t.eq(S._fb_item_url('123'), 'https://www.facebook.com/marketplace/item/123/',
     'canonical url drops tracking params')

t.eq(S._term_wants_description({}), False, 'no keywords -> no fetch')
t.eq(S._term_wants_description(None), False, 'None cfg -> no fetch')
t.eq(S._term_wants_description({'exclusions': ['broken'], 'includes': []}), True, 'exclude only')
t.eq(S._term_wants_description({'exclusions': [], 'includes': ['charger']}), True, 'include only')
t.eq(S._term_wants_description({'exclusions': ['  '], 'includes': ['']}), False,
     'whitespace-only keywords -> no fetch')

t.section('3. is_excluded two-pass semantics')
title = 'Nintendo item listing'
# Must be a word the TITLE cannot satisfy, or "title alone drops it" is not a
# real assertion. Real bodies repeat their title, so pick deliberately.
BODY, word = None, None
for _iid in IDS:
    _w = body_only_word(title, BODIES[_iid])
    if _w:
        BODY, word = BODIES[_iid], _w
        break
t.ok(word, 'found a body-only word to test with')
print("  using body word %r" % word)
terms = {'nintendo': {'min': None, 'max': None, 'exclusions': [], 'includes': [word]}}
t.eq(S.is_excluded(title, 50, [], terms, 'nintendo'), True,
     'title-only: include miss -> excluded (todays behaviour)')
t.eq(S.is_excluded(title, 50, [], terms, 'nintendo', description=BODY), False,
     'with body: include found in description -> KEPT')
t.eq(S.is_excluded(title, 50, [], terms, 'nintendo', check_includes=False), False,
     'pass 1 (check_includes=False) survives to the fetch')

t2 = {'nintendo': {'min': None, 'max': None, 'exclusions': ['broken'], 'includes': [word]}}
t.eq(S.is_excluded('Nintendo item broken', 50, [], t2, 'nintendo', check_includes=False), True,
     'pass 1 still drops a title-excluded listing')
t.eq(S.is_excluded(title, 50, [], t2, 'nintendo', description='it is broken sadly'), True,
     'exclude keyword found in the body -> excluded')

# The separate-haystack invariant: a title ending "blue" plus a body starting
# "tooth" must never read as "bluetooth".
t4 = {'x': {'min': None, 'max': None, 'exclusions': [], 'includes': ['bluetooth']}}
t.eq(S.is_excluded('Speaker in blue', 10, [], t4, 'x', description='tooth fairy pillow'), True,
     'no match across the title/description seam')
t.eq(S.is_excluded('Speaker', 10, [], t4, 'x', description='has bluetooth support'), False,
     'genuine bluetooth in the body matches')
t5 = {'x': {'min': None, 'max': None, 'exclusions': [], 'includes': []}}
t.eq(S.is_excluded('anything', 10, [], t5, 'x', description='whatever'), False,
     'empty includes = no filter')

t.section('4. END TO END: scrape_facebook_for_user actually called')

FETCHES = []


class FakeSession(object):
    def __init__(self, *a, **k):
        pass

    def fetch(self, url):
        FETCHES.append(url)
        for iid, html in PAGES.items():
            if ('/item/%s/' % iid) in url:
                return html
        # Stand-in search page: long enough and carrying the marker, so the real
        # _fb_html_is_blocked treats it as a live page.
        return '<html>' + (S._FB_JSON_TITLE_KEY + ':x') * 400 + '</html>'

    def rotate(self, p):
        pass

    def close(self):
        pass


ITEMS = [{'title': 'Nintendo item listing', 'price': 50.0,
          'link': 'https://www.facebook.com/marketplace/item/%s/?ref=search' % IDS[0],
          'image_url': 'http://img/1.jpg', 'city': None, 'creation_time': None}]

_o = {}


def patch():
    _o['S'] = S._FbLeanSession
    _o['ai'] = S.check_image_with_ai
    _o['act'] = S.is_user_active
    _o['zip'] = S._zip_to_latlon
    _o['reg'] = S.fb_region_for_zip
    _o['col'] = S._fb_collect_from_json
    S._FbLeanSession = FakeSession
    S.check_image_with_ai = lambda *a, **k: True
    S.is_user_active = lambda *a, **k: True
    S._zip_to_latlon = lambda *a, **k: None
    S.fb_region_for_zip = lambda *a, **k: None
    S._fb_collect_from_json = lambda h: list(ITEMS)


def unpatch():
    S._FbLeanSession = _o['S']
    S.check_image_with_ai = _o['ai']
    S.is_user_active = _o['act']
    S._zip_to_latlon = _o['zip']
    S.fb_region_for_zip = _o['reg']
    S._fb_collect_from_json = _o['col']


def run(cfg, scan_description):
    del FETCHES[:]
    patch()
    try:
        return S.scrape_facebook_for_user(
            'test-user', '95212', 25, {'nintendo': cfg}, [], False, 'balanced',
            debug=True, log_callback=None, known_links=set(),
            scan_description=scan_description)
    finally:
        unpatch()


def item_navs():
    return [u for u in FETCHES if '/marketplace/item/' in u]


base = {'min': None, 'max': None}
res = run(dict(base, exclusions=[], includes=[word]), True)
t.eq(len(res), 1, 'A: listing kept because the BODY matched the include')
t.eq(len(item_navs()), 1, 'A: exactly one item navigation')

res = run(dict(base, exclusions=[], includes=[word]), False)
t.eq(len(res), 0, 'B: feature off -> title-only, listing dropped')
t.eq(len(item_navs()), 0, 'B: feature off -> ZERO item navigations')

res = run(dict(base, exclusions=[], includes=[]), True)
t.eq(len(res), 1, 'C: no keywords -> listing kept')
t.eq(len(item_navs()), 0, 'C: no keywords -> ZERO navigations (pays nothing)')

res = run(dict(base, exclusions=['nintendo'], includes=[word]), True)
t.eq(len(res), 0, 'D: title-excluded listing dropped')
t.eq(len(item_navs()), 0, 'D: title-excluded -> ZERO navigations (dropped in pass 1)')

t.section('5. Master switch')
os.environ.pop('DESCRIPTION_SCAN_ENABLED', None)
t.eq(S._description_scan_enabled(), False, 'master switch defaults OFF')
os.environ['DESCRIPTION_SCAN_ENABLED'] = '1'
t.eq(S._description_scan_enabled(), True, "honours '1'")
os.environ['DESCRIPTION_SCAN_ENABLED'] = '0'
t.eq(S._description_scan_enabled(), False, "honours '0'")
os.environ.pop('DESCRIPTION_SCAN_ENABLED', None)

t.section('6. Browserless path, and the browser fallback beneath it')

# Facebook is the last platform that launched a browser in steady state. These
# assertions are what keep it that way: a healthy scrape must never touch the
# session, and a blocked one must still fall back rather than return nothing.
BL_CALLS = []
SESSION_FETCHES = []


class CountingSession(FakeSession):
    def fetch(self, url):
        SESSION_FETCHES.append(url)
        return FakeSession.fetch(self, url)


def fake_browserless(url, label='', log_callback=None, user_id=None, debug=False, ok=True):
    BL_CALLS.append(url)
    if not ok:
        return '', False
    for iid, html in PAGES.items():
        if ('/item/%s/' % iid) in url:
            return html, True
    return '<html>' + (S._FB_JSON_TITLE_KEY + ':x') * 400 + '</html>', True


def run_bl(cfg, browserless_ok):
    del BL_CALLS[:]
    del SESSION_FETCHES[:]
    del FETCHES[:]
    os.environ['FB_BROWSERLESS'] = '1'
    orig_bl = S._fb_browserless_fetch
    S._fb_browserless_fetch = (lambda *a, **k: fake_browserless(*a, **k)) if browserless_ok         else (lambda *a, **k: ('', False))
    _o['S'] = S._FbLeanSession
    patch()
    S._FbLeanSession = CountingSession
    try:
        return S.scrape_facebook_for_user(
            'bl-user', '95212', 25, {'nintendo': cfg}, [], False, 'balanced',
            debug=True, log_callback=None, known_links=set(), scan_description=True)
    finally:
        unpatch()
        S._fb_browserless_fetch = orig_bl
        os.environ['FB_BROWSERLESS'] = '0'


cfg_bl = dict(base, exclusions=[], includes=[word])
res = run_bl(cfg_bl, browserless_ok=True)
t.eq(len(res), 1, 'browserless: the listing is found')
t.ok(len(BL_CALLS) >= 2, 'browserless handled BOTH the search and the item page')
t.eq(SESSION_FETCHES, [],
     'NO BROWSER WAS TOUCHED — the whole point of this change')

res = run_bl(cfg_bl, browserless_ok=False)
t.eq(len(res), 1, 'fallback: the listing is still found when browserless fails')
t.ok(len(SESSION_FETCHES) >= 1,
     'the browser fallback ran, so a block degrades instead of losing the platform')

os.environ.pop('FB_BROWSERLESS', None)
t.eq(S._fb_browserless_enabled(), True, 'browserless is ON by default')
os.environ['FB_BROWSERLESS'] = '0'
t.eq(S._fb_browserless_enabled(), False, 'and can be turned off')
os.environ.pop('FB_BROWSERLESS', None)

t.done()
