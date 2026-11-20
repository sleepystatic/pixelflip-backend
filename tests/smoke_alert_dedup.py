# -*- coding: utf-8 -*-
"""
Persistent alert dedup (2026-10-19).

THE BUG IT GUARDS: "already seen" used to mean "a row exists in `listings`",
and the cleanup thread purges `listings` after 7 days. A Facebook listing can
stay live for 45 days, so it re-inserted as "new" after every purge and the user
was alerted about the same listing once a week.

This drives the REAL _scrape_for_user_impl (dedup, persist, digest) with the
four platform scrapers stubbed to return the same live listings every time, and
purges `listings` between scans exactly as the cleanup thread does. Offline: the
database is in-memory and no email is sent.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import scraper_multi_user as S
import listing_notifications as LN
import psycopg2.extras as ex

t = Suite('alert-dedup')
REAL = {n: getattr(S, n) for n in ('get_alerted_links', 'record_alerted_listings',
                                    '_alerted_retention_days')}

LISTINGS = {}            # user -> links currently in `listings`
ALERTED = {}             # user -> links in alerted_listings
DIGESTS = []             # (user, [links])
WORDS = ('alpha', 'bravo', 'charlie')   # distinct tokens: the fingerprint drops 1-char ones


def live(platform):
    """The same three listings, still live, on every scan."""
    def stub(user_id, zip_code, search_radius, search_terms, *a, **k):
        return [{'title': 'nintendo ds lite %s %s' % (platform, w), 'price': 50.0 + 20 * i,
                 'link': 'https://example.invalid/%s/item/%s' % (platform, w),
                 'platform': platform, 'console_type': 'nintendo ds lite',
                 'threshold': None, 'image_url': None, 'listed_at': None,
                 'location': 'Stockton'} for i, w in enumerate(WORDS)]
    return stub


def save(uid, rows):
    have = LISTINGS.setdefault(uid, set())
    new = {r['link'] for r in rows if r['link'] not in have}
    have |= new
    return new


S.scrape_craigslist_for_user = live('Craigslist')
S.scrape_offerup_for_user = live('OfferUp')
S.scrape_mercari_for_user = live('Mercari')
S.scrape_facebook_for_user = live('Facebook')
S.get_user_search_terms = lambda uid: {
    'nintendo ds lite': {'min': None, 'max': None, 'exclusions': [], 'includes': [],
                         'is_priority': False, 'interval_minutes': 10, 'last_scraped_ts': 0.0}}
S.due_terms_for_user = lambda all_terms, cfg: (dict(all_terms), None)
S.get_seen_listings = lambda uid: set(LISTINGS.get(uid, ()))
S.get_blocked_links_and_fingerprints = lambda uid: (set(), set())
S.get_recent_listing_signatures = lambda *a, **k: []
S.get_alerted_links = lambda uid: set(ALERTED.get(uid, ()))
S.record_alerted_listings = lambda uid, rows: ALERTED.setdefault(uid, set()).update(r['link'] for r in rows)
S.save_listings_bulk = save
S.get_user_exclusions = lambda uid: []
S.get_user_notification_prefs = lambda uid: {'notification_channels': {'email': True}}
S.get_user_auth_email = lambda uid: 'x@example.invalid'
S.stamp_terms_scraped = lambda uid, terms: None
S.is_user_active = lambda *a, **k: True
S.reset_geocode_budget = lambda *a, **k: None
LN.dispatch_scrape_digest_notifications = (
    lambda user_id, new_listings, prefs, **k:
    DIGESTS.append((user_id, [l['link'] for l in new_listings])) or [])


def cfg(uid):
    return {'user_id': uid, 'zip_code': '95212', 'search_radius': 25,
            'platforms': {'craigslist': True, 'offerup': True, 'mercari': True, 'facebook': True},
            'ai_enabled': False, 'ai_strictness': 'balanced', 'check_interval': 10,
            'plan_tier': 'pro', 'is_pro': True, 'buyer_include_local': True,
            'buyer_include_shipping': True, 'scan_description': False}


def scan(uid):
    del DIGESTS[:]
    S._scrape_for_user_impl(cfg(uid), log_callback=lambda *a, **k: None, debug=True)
    return [l for u, links in DIGESTS if u == uid for l in links]


t.section('1. A first scan alerts, and records what it alerted')
first = scan('user-A')
t.eq(len(first), 12, 'four platforms x three listings alerted once')
t.eq(ALERTED.get('user-A'), set(first), 'every alerted link was recorded in alerted_listings')

t.section('2. THE BUG: after the 7-day listings purge, still-live listings stay quiet')
LISTINGS['user-A'] = set()                    # what cleanup_old_listings does at day 7
t.eq(scan('user-A'), [], 'no re-alert for listings that are still live after the purge')

t.section('3. It is the alert log doing the work, not something else')
LISTINGS['user-A'] = set()
ALERTED['user-A'] = set()                     # its retention expired too
t.eq(len(scan('user-A')), 12, 'with both cleared, the same listings alert again')

t.section('4. Dedup is per user')
t.eq(len(scan('user-B')), 12, 'user B is alerted even though user A already was')
t.eq(scan('user-B'), [], '...and only once')

t.section('5. The real helpers: retention window, upsert, fail-open')
CAP = {}


class Cur(object):
    def execute(self, sql, args=None):
        CAP['sql'], CAP['args'] = sql, args

    def fetchall(self):
        return [('L1',), ('L2',)]

    def close(self):
        pass


class Conn(object):
    def cursor(self, *a, **k):
        return Cur()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


S.get_db_connection = lambda: Conn()
os.environ['ALERTED_RETENTION_DAYS'] = '60'
t.eq(REAL['get_alerted_links']('u'), {'L1', 'L2'}, 'get_alerted_links returns the recorded links')
t.ok('first_alerted_at >=' in CAP['sql'] and CAP['args'][1] == 60, '...filtered to the 60-day window')
t.ok(REAL['_alerted_retention_days']() > 45, "retention outlives Facebook's 45-day listing age")
REC = {}
orig_ev = ex.execute_values
ex.execute_values = lambda cur, sql, rows, **k: REC.update(sql=sql, rows=list(rows))
try:
    REAL['record_alerted_listings']('u', [{'link': 'L9', 'title_fingerprint': 'fp'}])
finally:
    ex.execute_values = orig_ev
t.ok('ON CONFLICT (user_id, link) DO NOTHING' in REC.get('sql', ''),
     'recording is an upsert that keeps the FIRST alert time')
S.get_db_connection = lambda: (_ for _ in ()).throw(RuntimeError('db down'))
t.eq(REAL['get_alerted_links']('u'), set(), 'a DB outage fails OPEN (worst case: one duplicate alert)')
try:
    REAL['record_alerted_listings']('u', [{'link': 'x'}])
    t.ok(True, 'recording during an outage does not raise into the scrape')
except Exception as e:
    t.ok(False, 'recording raised: %r' % e)
os.environ.pop('ALERTED_RETENTION_DAYS', None)

t.done()
