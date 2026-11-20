# -*- coding: utf-8 -*-
"""OfferUp description scanning (handoff 1.1c). Offline: parses committed fixtures."""
import io
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, body_only_word, bootstrap, fixture_glob

bootstrap()
import scraper_multi_user as S
import curl_cffi.requests as cffi_requests

t = Suite('offerup-description')

PAGES = {}
for p in fixture_glob('ou_item_*.html'):
    PAGES[os.path.basename(p)[len('ou_item_'):-len('.html')]] = io.open(p, encoding='utf-8').read()
if len(PAGES) < 2:
    t.skip('need at least 2 ou_item_*.html fixtures, found %d '
           '(OfferUp throttles on request RATE — refresh after a cooldown)' % len(PAGES))

IDS = sorted(PAGES)
ID_A, ID_B = IDS[0], IDS[1]


class FakeResp(object):
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status
        self.content = text.encode('utf-8', 'replace')


FETCHES = []


def fake_get(url, **kw):
    FETCHES.append(url)
    for iid, html in PAGES.items():
        if iid in url:
            return FakeResp(html)
    return FakeResp('', 404)


# _offerup_item_details imports curl_cffi at call time, so patching the module
# attribute is enough.
cffi_requests.get = fake_get


def url_for(iid):
    return 'https://offerup.com/item/detail/%s' % iid


t.section('1. _offerup_item_details against real payloads')
del FETCHES[:]
at, desc = S._offerup_item_details(url_for(ID_A), 0, want_posted=True, want_description=True)
t.ok(isinstance(at, datetime), 'both wanted -> postDate parsed to datetime')
t.ok(at is not None and at.tzinfo is not None, 'date is timezone-aware')
t.ok(isinstance(desc, str) and desc, 'both wanted -> description is a string')
t.eq(len(FETCHES), 1, 'both fields cost exactly ONE fetch')
print("  body: %r" % (desc or '')[:110])

del FETCHES[:]
at2, d2 = S._offerup_item_details(url_for(ID_A), 0, want_posted=True)
t.ok(isinstance(at2, datetime), 'date only -> date returned')
t.eq(d2, None, 'date only -> no description')
t.eq(len(FETCHES), 1, 'date only -> one fetch')

del FETCHES[:]
at3, d3 = S._offerup_item_details(url_for(ID_A), 0, want_description=True)
t.eq(at3, None, 'body only -> no date')
# The old walk required postDate to be present to accept the object, so a page
# fetched purely for its description would have found nothing.
t.ok(isinstance(d3, str) and d3, 'body only -> description STILL found')
t.eq(d3, desc, 'body-only description matches the both-wanted one')
t.eq(len(FETCHES), 1, 'body only -> one fetch')

del FETCHES[:]
t.eq(S._offerup_item_details(url_for(ID_A), 0), (None, None), 'neither wanted -> (None, None)')
t.eq(len(FETCHES), 0, 'neither wanted -> ZERO fetches')

del FETCHES[:]
t.eq(S._offerup_item_details('https://offerup.com/nope', 0, want_description=True),
     (None, None), 'non-item url -> (None, None)')
t.eq(len(FETCHES), 0, 'non-item url -> no fetch')
t.eq(S._offerup_item_details(url_for('dead-id-not-saved'), 0, want_description=True),
     (None, None), 'HTTP 404 -> (None, None)')

# Identity scoping: serve page A under a different id and it must fail closed,
# never attribute a related item's text to the listing we asked for.
PAGES['scoping-probe'] = PAGES[ID_A]
t.eq(S._offerup_item_details(url_for('scoping-probe'), 0, want_description=True),
     (None, None), 'id mismatch -> fails closed')
del PAGES['scoping-probe']
t.ok(not isinstance(desc, (list, dict)),
     'description is the plain string, not descriptionHast')

t.section('2. END TO END: scrape_offerup_for_user actually called')
word_a = body_only_word('OfferUp listing one', desc or '')
_, body_b = S._offerup_item_details(url_for(ID_B), 0, want_description=True)
t.ok(word_a, 'found a body word in listing A')

ITEMS = [
    {'title': 'OfferUp listing one', 'price': 400.0, 'link': url_for(ID_A),
     'image_url': 'http://img/a.jpg', 'city': None},
    {'title': 'OfferUp listing two', 'price': 400.0, 'link': url_for(ID_B),
     'image_url': 'http://img/b.jpg', 'city': None},
]

_o = {}


def patch():
    _o['gql'] = S._offerup_fetch_via_graphql
    _o['ai'] = S.check_image_with_ai
    _o['act'] = S.is_user_active
    _o['zip'] = S._zip_to_latlon
    S._offerup_fetch_via_graphql = lambda *a, **k: (list(ITEMS), None)
    S.check_image_with_ai = lambda *a, **k: True
    S.is_user_active = lambda *a, **k: True
    S._zip_to_latlon = lambda *a, **k: (38.0, -121.26)


def unpatch():
    S._offerup_fetch_via_graphql = _o['gql']
    S.check_image_with_ai = _o['ai']
    S.is_user_active = _o['act']
    S._zip_to_latlon = _o['zip']


def run(cfg, scan_description, posted='0'):
    del FETCHES[:]
    os.environ['OFFERUP_FETCH_ITEM_POSTED'] = posted
    os.environ['OFFERUP_ITEM_PAGE_DELAY_SEC'] = '0'
    os.environ['MAX_LISTING_AGE_DAYS'] = '3650'
    patch()
    try:
        return S.scrape_offerup_for_user(
            'ou-test', '95212', 3000, {'offerup': cfg}, [], False, 'balanced',
            debug=True, log_callback=None, known_links=set(),
            scan_description=scan_description)
    finally:
        unpatch()


base = {'min': None, 'max': None}
res = run(dict(base, exclusions=[], includes=[word_a]), True)
t.ok(len(res) >= 1, 'A: at least the listing whose BODY matched survives')
t.eq(len(FETCHES), 2, 'A: one item page per candidate')

res = run(dict(base, exclusions=[], includes=[word_a]), False)
t.eq(len(FETCHES), 0, 'B: feature off -> ZERO item fetches')

res = run(dict(base, exclusions=[], includes=[]), True)
t.eq(len(res), 2, 'C: no keywords -> both kept')
t.eq(len(FETCHES), 0, 'C: no keywords -> ZERO fetches')

res = run(dict(base, exclusions=['offerup'], includes=[word_a]), True)
t.eq(len(res), 0, 'D: title-excluded -> dropped')
t.eq(len(FETCHES), 0, 'D: title-excluded -> ZERO fetches (pass 1)')

# The point of the refactor: the post date and the body share ONE page.
res = run(dict(base, exclusions=[], includes=[]), True, posted='1')
t.eq(len(FETCHES), 2, 'E: date+body together still cost one page per listing')
t.ok(all(isinstance(r.get('listed_at'), datetime) for r in res),
     'E: listed_at populated from that same fetch')

res = run(dict(base, exclusions=[], includes=[]), False, posted='1')
t.eq(len(res), 2, 'F: legacy posted-date path unchanged')
t.eq(len(FETCHES), 2, 'F: legacy path still fetches for the date')
t.ok(all(isinstance(r.get('listed_at'), datetime) for r in res),
     'F: legacy postDate still parsed')

t.done()
