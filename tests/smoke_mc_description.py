# -*- coding: utf-8 -*-
"""Mercari description scanning (handoff 1.1d). Offline: parses committed fixtures."""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, body_only_word, bootstrap, fixture_glob

bootstrap()
import scraper_multi_user as S

t = Suite('mercari-description')

pages = fixture_glob('mc_item_*.html')
if not pages:
    t.skip('no mc_item_*.html fixtures')

HTML = io.open(pages[0], encoding='utf-8').read()
m = re.search(r'"ItemDetail:(m\d+)"', HTML)
if not m:
    t.skip('fixture has no ItemDetail key')
ITEM_ID = m.group(1)

t.section('1. Extractor against a real item page (%s)' % ITEM_ID)
d = S._mercari_description_from_item_html(HTML, ITEM_ID)
print("  body: %r" % (d or '')[:130])
t.ok(d, 'description extracted')

low = (d or '').lower()
# og:description reads like a description and is not one: it is Mercari's
# marketing copy, near-identical on every listing. Matching against it would
# make 'new', 'condition' and 'deals' hit the entire platform.
t.ok('shop smarter' not in low, 'og:description marketing copy NOT returned')
t.ok('discover amazing secondhand deals' not in low, 'no marketing boilerplate in body')

# A removed Mercari listing redirects, so a mismatched id must fail closed.
t.eq(S._mercari_description_from_item_html(HTML, 'm00000000000'), None,
     'wrong item id -> None (fails closed on a redirect)')
t.eq(S._mercari_description_from_item_html(HTML, None), d,
     'no id -> a single ItemDetail entry is accepted')
t.eq(S._mercari_description_from_item_html('', ITEM_ID), None, 'empty html -> None')
t.eq(S._mercari_description_from_item_html(None, ITEM_ID), None, 'None html -> None')

# JSON-LD fallback, for when the serverState shape moves.
stripped = re.sub(r'<script id="__NEXT_DATA__".*?</script>', '', HTML, flags=re.S)
t.ok('__NEXT_DATA__' not in stripped, 'fixture really has __NEXT_DATA__ removed')
d2 = S._mercari_description_from_item_html(stripped, ITEM_ID)
t.ok(d2 and d2 == d, 'JSON-LD fallback finds the same body')
# ...but JSON-LD is not id-keyed, so it must not fire for another item's page.
t.eq(S._mercari_description_from_item_html(stripped, 'm00000000000'), None,
     'JSON-LD fallback gated on the page really being this item')

t.section('2. Link parsing')
t.eq(S._mercari_item_id_from_link('https://www.mercari.com/us/item/m97282368816/'),
     'm97282368816', 'id from a full url')
t.eq(S._mercari_item_id_from_link('/us/item/m123456/'), 'm123456', 'id from a relative url')
t.eq(S._mercari_item_id_from_link('https://example.com/x'), None, 'non-item -> None')
t.eq(S._mercari_item_id_from_link(''), None, 'empty -> None')
t.eq(S._mercari_item_id_from_link(None), None, 'None -> None')

t.section('3. END TO END: scrape_mercari_for_user actually called')
OTHER_ID = 'm55555555555'
OTHER_HTML = HTML.replace(ITEM_ID, OTHER_ID)
# Give the second listing a distinct body. It occurs in two encodings —
# verbatim in the JSON-LD block and newline-escaped inside __NEXT_DATA__ — so
# replacing only one leaves the copy the extractor prefers untouched.
OTHER_BODY = 'used console, small scratch on the screen, works perfectly'
OTHER_HTML = OTHER_HTML.replace(d, OTHER_BODY).replace(
    d.replace(chr(10), chr(92) + 'n'), OTHER_BODY)
assert S._mercari_description_from_item_html(OTHER_HTML, OTHER_ID) == OTHER_BODY, \
    'second fixture did not take'

PAGES = {ITEM_ID: HTML, OTHER_ID: OTHER_HTML}
FETCHES = []


class FakeItemSession(object):
    def __init__(self, ctx=None):
        pass

    def fetch(self, url):
        FETCHES.append(url)
        for iid, html in PAGES.items():
            if iid in url:
                return html
        return ''

    def close(self):
        pass


ROWS = [
    {'title': 'Mercari listing one', 'price': 30.0,
     'link': 'https://www.mercari.com/us/item/%s/' % ITEM_ID,
     'image_url': 'http://img/a.jpg', 'listed_at': None},
    {'title': 'Mercari listing two', 'price': 180.0,
     'link': 'https://www.mercari.com/us/item/%s/' % OTHER_ID,
     'image_url': 'http://img/b.jpg', 'listed_at': None},
]

_o = {}


def patch():
    _o['s'] = S._MercariItemSession
    _o['api'] = S._mercari_fetch_via_api
    _o['ai'] = S.check_image_with_ai
    _o['act'] = S.is_user_active
    _o['cool'] = S._mercari_in_cooldown
    S._MercariItemSession = FakeItemSession
    S._mercari_fetch_via_api = lambda terms, n: ({t2: list(ROWS) for t2 in terms}, None)
    S.check_image_with_ai = lambda *a, **k: True
    S.is_user_active = lambda *a, **k: True
    S._mercari_in_cooldown = lambda *a, **k: 0


def unpatch():
    S._MercariItemSession = _o['s']
    S._mercari_fetch_via_api = _o['api']
    S.check_image_with_ai = _o['ai']
    S.is_user_active = _o['act']
    S._mercari_in_cooldown = _o['cool']


def run(cfg, scan_description):
    del FETCHES[:]
    os.environ['MERCARI_ITEM_DELAY_SEC'] = '0'
    os.environ['MAX_LISTING_AGE_DAYS'] = '3650'
    patch()
    try:
        return S.scrape_mercari_for_user(
            'mc-test', '95212', 25, {'mercari': cfg}, [], False, 'balanced',
            debug=True, log_callback=None, known_links=set(),
            scan_description=scan_description)
    finally:
        unpatch()


base = {'min': None, 'max': None}
word = body_only_word('Mercari listing one', d)
t.ok(word, 'found a body-only word to test with')

res = run(dict(base, exclusions=[], includes=[word]), True)
t.eq(len(res), 1, 'A: only the listing whose BODY matched survives')
t.ok(ITEM_ID in res[0]['link'], 'A: and it is the right listing')
t.eq(len(FETCHES), 2, 'A: one item page per candidate')

res = run(dict(base, exclusions=[], includes=[word]), False)
t.eq(len(res), 0, 'B: feature off -> include never matches a title')
t.eq(len(FETCHES), 0, 'B: feature off -> ZERO item fetches')

res = run(dict(base, exclusions=['scratch'], includes=[]), True)
t.eq(len(res), 1, "C: the listing whose body says 'scratch' is dropped")
t.ok(ITEM_ID in res[0]['link'], 'C: the clean one survives')

res = run(dict(base, exclusions=[], includes=[]), True)
t.eq(len(res), 2, 'D: no keywords -> both kept')
t.eq(len(FETCHES), 0, 'D: no keywords -> ZERO fetches')

res = run(dict(base, exclusions=['mercari'], includes=[word]), True)
t.eq(len(res), 0, 'E: title-excluded -> dropped')
t.eq(len(FETCHES), 0, 'E: title-excluded -> ZERO fetches (pass 1)')

t.done()
