# -*- coding: utf-8 -*-
"""Craigslist description scanning (handoff 1.1b). Offline: parses committed fixtures."""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, body_only_word, bootstrap, fixture_glob

bootstrap()
import scraper_multi_user as S
from bs4 import BeautifulSoup

t = Suite('craigslist-description')

pages = fixture_glob('cl_item_*.html')
if not pages:
    t.skip('no cl_item_*.html fixtures')

t.section('1. Extractor against real saved detail pages (%d)' % len(pages))
for p in pages:
    name = os.path.basename(p)
    soup = BeautifulSoup(io.open(p, 'rb').read(), 'html.parser')
    desc = S._craigslist_description_from_soup(soup)
    print("  %s: %r" % (name, (desc or '')[:110]))
    t.ok(desc, '%s: description extracted' % name)
    low = (desc or '').lower()
    # Both of these appear on EVERY posting. Left in, every Craigslist
    # description contains "contact" and "post", so an exclude on either would
    # silently kill the whole platform.
    t.ok('qr code link' not in low, '%s: QR boilerplate stripped' % name)
    t.ok('show contact info' not in low, "%s: 'show contact info' stripped" % name)
    # The caller reuses this soup for the price and gallery image afterwards, so
    # the extractor must not have mutated it.
    t.ok(soup.select_one('.print-information') is not None
         or soup.select_one('.print-qrcode-container') is not None,
         '%s: ORIGINAL soup still intact (extractor copied)' % name)
    t.ok(soup.select_one('#postingbody') is not None,
         '%s: #postingbody still present after extraction' % name)
    try:
        S._best_craigslist_gallery_image(soup)
        S._extract_craigslist_price_from_listing_soup(soup)
        t.ok(True, '%s: price/image helpers still run on the same soup' % name)
    except Exception as e:
        t.ok(False, '%s: helpers raised %s' % (name, type(e).__name__))

t.eq(S._craigslist_description_from_soup(BeautifulSoup('<html></html>', 'html.parser')),
     None, 'no #postingbody -> None')

t.section('2. Enrichment writes the field only when asked')


class FakeResp(object):
    status_code = 200

    def __init__(self, content):
        self.content = content


class FakeSess(object):
    def __init__(self, content):
        self.content = content

    def get(self, url, timeout=None):
        return FakeResp(self.content)


raw = io.open(pages[0], 'rb').read()
l1 = {'link': 'http://x', 'price': None}
S._enrich_craigslist_from_detail(FakeSess(raw), l1, 3650, 0.0, want_description=True)
t.ok(l1.get('description'), 'want_description=True -> field populated')

l2 = {'link': 'http://x', 'price': None}
S._enrich_craigslist_from_detail(FakeSess(raw), l2, 3650, 0.0)
t.eq('description' in l2, False, 'default (off) -> field not even created')

t.section('3. Filtering on the body, and the title/description seam')
soup = BeautifulSoup(raw, 'html.parser')
body = S._craigslist_description_from_soup(soup)
title = 'Craigslist listing'
word = body_only_word(title, body)
t.ok(word, 'found a body-only word to test with')
print("  using body word %r" % word)
inc = {'x': {'min': None, 'max': None, 'exclusions': [], 'includes': [word]}}
t.eq(S.is_excluded(title, 10, [], inc, 'x'), True, 'title only -> include miss drops it')
t.eq(S.is_excluded(title, 10, [], inc, 'x', description=body), False, 'body rescues it')
exc = {'x': {'min': None, 'max': None, 'exclusions': [word], 'includes': []}}
t.eq(S.is_excluded(title, 10, [], exc, 'x'), False, 'title only -> exclude misses')
t.eq(S.is_excluded(title, 10, [], exc, 'x', description=body), True, 'body triggers the exclude')

seam = {'x': {'min': None, 'max': None, 'exclusions': [], 'includes': ['bluetooth']}}
t.eq(S.is_excluded('speaker in blue', 10, [], seam, 'x', description='tooth marks'), True,
     'no match across the title/description seam')

t.section('4. Warn when the setting cannot take effect')
# Without the detail page there is no body to read. All-or-nothing means the
# user must never be left guessing whether their filter applied.
old_active = S.is_user_active
os.environ['CRAIGSLIST_FETCH_DETAIL'] = '0'
S.is_user_active = lambda *a, **k: False   # returns before any network call
try:
    msgs = []
    S.scrape_craigslist_for_user(
        'u', '95212', 25,
        {'nintendo': {'min': None, 'max': None, 'exclusions': ['broken'], 'includes': []}},
        [], False, 'balanced', debug=True,
        log_callback=lambda uid, m, k='info': msgs.append(m),
        known_links=set(), scan_description=True)
    t.ok(any('CRAIGSLIST_FETCH_DETAIL' in m for m in msgs),
         'warns when fetch_detail is off but scanning is on')
    msgs2 = []
    S.scrape_craigslist_for_user(
        'u', '95212', 25,
        {'nintendo': {'min': None, 'max': None, 'exclusions': ['broken'], 'includes': []}},
        [], False, 'balanced', debug=True,
        log_callback=lambda uid, m, k='info': msgs2.append(m),
        known_links=set(), scan_description=False)
    t.ok(not any('CRAIGSLIST_FETCH_DETAIL' in m for m in msgs2),
         'silent when scanning is off')
finally:
    S.is_user_active = old_active
    os.environ['CRAIGSLIST_FETCH_DETAIL'] = '1'

t.done()
