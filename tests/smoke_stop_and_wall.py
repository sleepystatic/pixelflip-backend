# -*- coding: utf-8 -*-
"""
Two fixes found by the 2026-11-23 live probe and start/stop chaos test, pinned
through the REAL scan function (_scrape_for_user_impl) with stubbed marketplaces.

  STOP means stop alerting: a scan whose user pressed STOP sends NO digest for
  what it had already found (the chaos test caught 8 such digests); a quick
  Stop+Start does still alert; marketplaces skipped because of a STOP are an
  info line, not a red "NOT scanned" that reads like an outage.

  OfferUp item pages: never requested by default (walled since ~2026-10, and
  even two refusals left the browser-free search answering 403); with
  OFFERUP_ITEM_PAGES=1 a circuit breaker stops after two refusals and one
  success closes it again.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import scraper_multi_user as S
import listing_notifications as LN

t = Suite('stop-and-wall')

# ---------------------------------------------------------------- scan harness
STATE = {'active': True, 'flip_after': None, 'calls': 0}
DIGESTS, LOG = [], []


def is_active(uid, force=False):
    STATE['calls'] += 1
    if STATE['flip_after'] is not None and STATE['calls'] > STATE['flip_after']:
        return False
    return STATE['active']


def platform(label, n):
    def fn(user_id, zip_code, radius, terms, exclusions, ai, strict, debug, cb,
           known_links=None, scan_description=False):
        # Distinct words and prices: near-identical titles at one price are
        # (correctly) collapsed by the relist fingerprint dedupe.
        words = ('cobalt', 'crimson', 'onyx')
        return [{'title': 'ds lite %s %s edition' % (words[i], label), 'price': 40.0 + 7 * i + len(label),
                 'platform': label,
                 'link': 'https://example.invalid/%s/%d' % (label, i), 'image_url': None,
                 'console_type': 'ds lite', 'threshold': None, 'listed_at': None, 'location': 'x'}
                for i in range(n)]
    return fn


S.scrape_craigslist_for_user = platform('craigslist', 3)
S.scrape_offerup_for_user = platform('offerup', 3)
S.scrape_mercari_for_user = platform('mercari', 3)
S.scrape_facebook_for_user = platform('facebook', 3)
S.is_user_active = is_active
SAVED = set()
S.save_listings_bulk = lambda uid, rows: {r['link'] for r in rows if r['link'] not in SAVED and not SAVED.add(r['link'])}
S.record_alerted_listings = lambda uid, rows: None
S.get_alerted_links = lambda uid: set()
S.get_seen_listings = lambda uid: {'https://example.invalid/old'}
S.get_blocked_links_and_fingerprints = lambda uid: (set(), set())
S.get_recent_listing_signatures = lambda *a, **k: []
S.get_user_search_terms = lambda uid: {'ds lite': {'min': None, 'max': None, 'exclusions': [], 'includes': [],
                                                   'is_priority': False, 'interval_minutes': 10,
                                                   'last_scraped_ts': 0.0}}
S.stamp_terms_scraped = lambda uid, terms: None
S.get_user_exclusions = lambda uid: []
S.get_user_notification_prefs = lambda uid: {'notification_channels': {'email': True}}
S.get_user_auth_email = lambda uid: 'x@example.invalid'
S.reset_geocode_budget = lambda *a, **k: None
S.check_image_with_ai = lambda *a, **k: True
LN.dispatch_scrape_digest_notifications = lambda uid, rows, prefs, **k: DIGESTS.append(len(rows)) or []
USER = {'user_id': 'stop-test', 'zip_code': '95212', 'search_radius': 25,
        'platforms': {'craigslist': True, 'offerup': True, 'mercari': True, 'facebook': True},
        'ai_enabled': False, 'ai_strictness': 'balanced', 'check_interval_minutes': 10,
        'plan_tier': 'pro', 'is_pro': True, 'buyer_include_local': True, 'buyer_include_shipping': True}


def scan(active=True, flip_after=None):
    SAVED.clear()
    del DIGESTS[:]
    del LOG[:]
    STATE.update(active=active, flip_after=flip_after, calls=0)
    S._scrape_for_user_impl(dict(USER), log_callback=lambda u, m, k='info': LOG.append((k, m)), debug=False)


t.section('1. A normal scan alerts once')
scan()
t.eq(DIGESTS, [12], 'four marketplaces x 3 new listings -> one digest of 12')

t.section('2. STOP during the scan: no alert, and the reason is said plainly')
scan(flip_after=2)      # active for the first two checks, then STOP
t.eq(DIGESTS, [], 'scanner off by the end -> NO digest (the chaos test found 8 of these)')
t.ok(any(k == 'info' and 'no alert sent' in m and 'feed' in m for k, m in LOG),
     '...and the console says the matches are in the feed instead')
t.ok(any(k == 'info' and m.startswith('Scan stopped by you') and 'skipped' in m for k, m in LOG),
     'marketplaces skipped by the STOP are an info line')
t.ok(not any(k == 'error' and 'NOT scanned' in m for k, m in LOG),
     '...not a red "Marketplaces NOT scanned" that reads like an outage')

t.section('3. A quick Stop+Start still alerts')
calls = {'n': 0}


def blip(uid, force=False):
    calls['n'] += 1
    return calls['n'] not in (3,)          # one OFF reading mid-scan, ON again after
S.is_user_active = blip
SAVED.clear()
del DIGESTS[:]
S._scrape_for_user_impl(dict(USER), log_callback=lambda u, m, k='info': None, debug=False)
t.ok(DIGESTS and DIGESTS[0] > 0, 'back ON at the end -> what was found is alerted')
S.is_user_active = is_active

t.section('4. OfferUp item pages: off by default, breaker when enabled')
REQS = []


class Resp(object):
    def __init__(self, status, text=''):
        self.status_code, self.text, self.content = status, text, text.encode()


import curl_cffi.requests as cffi                      # noqa: E402
RESULT = {'status': 403}
cffi.get = lambda url, **k: REQS.append(url) or Resp(RESULT['status'], RESULT.get('text', ''))
os.environ.pop('OFFERUP_ITEM_PAGES', None)
S._OU_ITEM_WALL.update(fails=0, until=0.0)
link = 'https://offerup.com/item/detail/abc-123'
t.eq(S._offerup_item_details(link, 0, want_description=True), (None, None), 'disabled -> no data')
t.eq(REQS, [], '...and NO request is made (the default since 2026-11-23)')

os.environ['OFFERUP_ITEM_PAGES'] = '1'
S._offerup_item_details(link, 0, want_description=True)
S._offerup_item_details(link, 0, want_description=True)
t.eq(len(REQS), 2, 'enabled: two refused requests...')
t.ok(S._ou_item_wall_open(), '...open the breaker')
S._offerup_item_details(link, 0, want_description=True)
t.eq(len(REQS), 2, 'while open, no further requests')
S._OU_ITEM_WALL['until'] = 0.0                           # cooldown elapsed
RESULT.update(status=200, text='<script id="__NEXT_DATA__" type="application/json">{"a":1}</script>')
S._offerup_item_details(link, 0, want_description=True)
t.ok(len(REQS) == 3 and S._OU_ITEM_WALL['fails'] == 0, 'after the cooldown a success closes it again')
os.environ.pop('OFFERUP_ITEM_PAGES')

t.done()
