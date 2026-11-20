# -*- coding: utf-8 -*-
"""
Description scanning is Pro-only, at BOTH gates, and title/description are
matched as SEPARATE haystacks inside ONE scrape.

Offline. Imports app.py, which is why _harness.bootstrap() forces
ENABLE_CLEANUP_THREAD=0 first: app.py starts a cleanup thread at import that
deletes old production listings on a timer.
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap, fixture_glob

bootstrap()
import scraper_multi_user as S

t = Suite('pro-gate-and-haystacks')

t.section('1. GATE ONE — what the API offers and stores (app._plan_limits)')
import app as A

for flag in ('1', '0'):
    os.environ['DESCRIPTION_SCAN_ENABLED'] = flag
    for tier in ('pro', 'basic', 'inactive', None, 'PRO'):
        allowed = A._plan_limits(tier).get('description_scan_allowed')
        expect = (flag == '1' and (tier or '').strip().lower() == 'pro')
        t.eq(bool(allowed), expect, 'master=%s tier=%r -> allowed' % (flag, tier))

# The settings route's clamp is: if not allowed -> want_desc = False.
os.environ['DESCRIPTION_SCAN_ENABLED'] = '1'
for tier, expect in (('pro', True), ('basic', False), ('inactive', False)):
    forged = True                        # a hand-rolled POST asking for it
    if not A._plan_limits(tier).get('description_scan_allowed'):
        forged = False
    t.eq(forged, expect, 'forged POST scan_description=True as %s' % tier)

t.section('2. GATE TWO — what the scraper acts on (get_active_users)')


class FakeCursor(object):
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn(object):
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, **k):
        return FakeCursor(self._rows)

    def close(self):
        pass


def row(**kw):
    r = {'user_id': 'u', 'zip_code': '95212', 'search_radius': 25,
         'platforms': {'mercari': True}, 'ai_enabled': False,
         'ai_strictness': 'balanced', 'check_interval_minutes': 10,
         'plan_tier': 'pro', 'is_pro': True, 'buyer_include_local': True,
         'buyer_include_shipping': True, 'scan_description': True}
    r.update(kw)
    return r


_real = S.get_db_connection
for label, r, flag, expect in [
    ('pro + column True',            row(),                                  '1', True),
    ('BASIC + column True (forged)', row(plan_tier='basic', is_pro=False),    '1', False),
    ('inactive + column True',       row(plan_tier=None, is_pro=False),       '1', False),
    ('pro + column False',           row(scan_description=False),            '1', False),
    ('pro + column True, master OFF', row(),                                 '0', False),
]:
    os.environ['DESCRIPTION_SCAN_ENABLED'] = flag
    S.get_db_connection = lambda rr=r: FakeConn([rr])
    try:
        users = S.get_active_users()
    finally:
        S.get_db_connection = _real
    t.eq(bool(users and users[0].get('scan_description')), expect,
         'scrape-time clamp: %s' % label)
os.environ['DESCRIPTION_SCAN_ENABLED'] = '1'

t.section('3. SEPARATE HAYSTACKS — through a real Mercari scrape')
pages = fixture_glob('mc_item_*.html')
if not pages:
    t.skip('no mc_item_*.html fixture for the seam test')
HTML = io.open(pages[0], encoding='utf-8').read()
ITEM_ID = re.search(r'"ItemDetail:(m\d+)"', HTML).group(1)
real_body = S._mercari_description_from_item_html(HTML, ITEM_ID)

SEAM = 'tooth marks on the shell, otherwise fine'
# The body occurs in TWO encodings: verbatim in the JSON-LD block and with
# newlines escaped inside __NEXT_DATA__. Replacing only the verbatim one leaves
# the copy the extractor prefers untouched, and this test then passes for the
# wrong reason — so replace both and PROVE the substitution took.
SEAM_HTML = HTML.replace(real_body, SEAM).replace(
    real_body.replace(chr(10), chr(92) + 'n'), SEAM)
check = S._mercari_description_from_item_html(SEAM_HTML, ITEM_ID)
t.eq(check, SEAM, 'seam fixture took (extractor sees the substituted body)')

FETCHES = []


class FakeItemSession(object):
    def __init__(self, ctx=None):
        pass

    def fetch(self, url):
        FETCHES.append(url)
        return SEAM_HTML if ITEM_ID in url else ''

    def close(self):
        pass


ROWS = [{'title': 'Nintendo Switch shell in blue', 'price': 40.0,
         'link': 'https://www.mercari.com/us/item/%s/' % ITEM_ID,
         'image_url': 'http://img/a.jpg', 'listed_at': None}]

_o = {}


def patch():
    _o['s'] = S._MercariItemSession
    _o['api'] = S._mercari_fetch_via_api
    _o['ai'] = S.check_image_with_ai
    _o['act'] = S.is_user_active
    _o['cool'] = S._mercari_in_cooldown
    S._MercariItemSession = FakeItemSession
    S._mercari_fetch_via_api = lambda terms, n: ({x: list(ROWS) for x in terms}, None)
    S.check_image_with_ai = lambda *a, **k: True
    S.is_user_active = lambda *a, **k: True
    S._mercari_in_cooldown = lambda *a, **k: 0


def unpatch():
    S._MercariItemSession = _o['s']
    S._mercari_fetch_via_api = _o['api']
    S.check_image_with_ai = _o['ai']
    S.is_user_active = _o['act']
    S._mercari_in_cooldown = _o['cool']


def run(cfg, scan_description=True):
    del FETCHES[:]
    os.environ['MERCARI_ITEM_DELAY_SEC'] = '0'
    os.environ['MAX_LISTING_AGE_DAYS'] = '3650'
    patch()
    try:
        return S.scrape_mercari_for_user(
            'seam', '95212', 25, {'switch': cfg}, [], False, 'balanced',
            debug=True, log_callback=None, known_links=set(),
            scan_description=scan_description)
    finally:
        unpatch()


base = {'min': None, 'max': None}
# Title ends "blue", body starts "tooth" — concatenated they spell "bluetooth".
t.eq(len(run(dict(base, exclusions=[], includes=['bluetooth']))), 0,
     "include 'bluetooth' does NOT match across the title/body seam")
t.eq(len(run(dict(base, exclusions=[], includes=['blue']))), 1,
     "include 'blue' matches the TITLE")
t.eq(len(run(dict(base, exclusions=[], includes=['tooth']))), 1,
     "include 'tooth' matches the BODY")
t.eq(len(run(dict(base, exclusions=['bluetooth'], includes=[]))), 1,
     "exclude 'bluetooth' does NOT match across the seam either")
t.eq(len(run(dict(base, exclusions=['tooth'], includes=[]))), 0,
     "exclude 'tooth' DOES match the body")

t.section('4. ONE SCRAPE — title and body filtering in a single call')
t.eq(len(run(dict(base, exclusions=['blue'], includes=['tooth']))), 0,
     'title exclude outranks a body include, in ONE call')
t.eq(len(FETCHES), 0, '...and it never bought a page (dropped in pass 1)')
t.eq(len(run(dict(base, exclusions=['zzz'], includes=['tooth']))), 1,
     'body include admits it, in the SAME call')
t.eq(len(FETCHES), 1, '...having fetched exactly one item page')

t.section('5. Ambiguous entitlement resolves DOWN, not up')
# A row with is_pro set and no plan_tier used to resolve to 'pro' in all three
# derivation sites, so any non-Stripe path that set is_pro handed out the most
# expensive plan. Migration 017 made legacy rows explicit; this guards the rule.
for row, want in (
    ({'plan_tier': 'pro', 'is_pro': True}, 'pro'),
    ({'plan_tier': 'basic', 'is_pro': True}, 'basic'),
    ({'plan_tier': 'basic', 'is_pro': False}, 'basic'),
    ({'plan_tier': None, 'is_pro': True}, 'basic'),
    ({'plan_tier': '', 'is_pro': True}, 'basic'),
    ({'plan_tier': None, 'is_pro': False}, 'inactive'),
    ({'plan_tier': 'PRO', 'is_pro': False}, 'pro'),
    ({'plan_tier': 'nonsense', 'is_pro': True}, 'basic'),
):
    t.eq(A._effective_plan_tier(row), want, 'app.py: %r' % (row,))
    t.eq(S._tier_from_db_row(row), want, 'scraper: %r' % (row,))
t.eq(A._effective_plan_tier(None), 'inactive', 'no row at all -> inactive')

# The two must never disagree: one guards the API, the other guards the scrape.
# A split would mean the dashboard offers a feature the scraper refuses to run.
for row in ({'plan_tier': None, 'is_pro': True}, {'plan_tier': 'basic', 'is_pro': True},
            {'plan_tier': None, 'is_pro': False}):
    t.eq(A._effective_plan_tier(row), S._tier_from_db_row(row),
         'app and scraper agree on %r' % (row,))

t.done()
