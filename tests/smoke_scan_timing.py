# -*- coding: utf-8 -*-
"""
Per-platform scan timing: a scrape must report WHERE its time went.

Production scrapes measured 184-247s for a single search term and nothing
recorded which marketplace was responsible, which left "add workers" as the only
visible lever — and that one needs RAM. Since the capacity arithmetic is
workers >= users * duration / interval, halving the duration is worth exactly
as much as doubling the box. This suite proves the breakdown is accurate, so
that decision can be made from evidence.

Offline: the platform scrapers are replaced with stubs that burn a known,
deliberately unequal amount of time.
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import scraper_multi_user as S

t = Suite('scan-timing')

# Deliberately unequal, and deliberately not in dispatch order, so a breakdown
# that merely echoed the order would not pass.
BURN = {'Craigslist': 0.30, 'OfferUp': 0.10, 'Mercari': 0.05, 'Facebook': 0.50}
LOGS = []


def make_stub(label):
    def stub(*a, **k):
        time.sleep(BURN[label])
        return []
    return stub


def boom(*a, **k):
    time.sleep(BURN['OfferUp'])
    raise RuntimeError('simulated platform failure')


_o = {}


def patch(offerup_fails=False):
    _o.update({
        'cl': S.scrape_craigslist_for_user, 'ou': S.scrape_offerup_for_user,
        'mc': S.scrape_mercari_for_user, 'fb': S.scrape_facebook_for_user,
        'terms': S.get_user_search_terms, 'due': S.due_terms_for_user,
        'seen': S.get_seen_listings, 'blocked': S.get_blocked_links_and_fingerprints,
        'sigs': S.get_recent_listing_signatures, 'excl': S.get_user_exclusions,
        'prefs': S.get_user_notification_prefs, 'save': S.save_listings_bulk,
        'stamp': S.stamp_terms_scraped, 'active': S.is_user_active,
        'geo': S.reset_geocode_budget,
        'alerted': S.get_alerted_links, 'rec': S.record_alerted_listings,
    })
    S.scrape_craigslist_for_user = make_stub('Craigslist')
    S.scrape_offerup_for_user = boom if offerup_fails else make_stub('OfferUp')
    S.scrape_mercari_for_user = make_stub('Mercari')
    S.scrape_facebook_for_user = make_stub('Facebook')
    S.get_user_search_terms = lambda uid: {
        'nintendo': {'min': None, 'max': None, 'exclusions': [], 'includes': [],
                     'is_priority': False, 'interval_minutes': 10, 'last_scraped_ts': 0.0}}
    S.due_terms_for_user = lambda all_terms, cfg: (dict(all_terms), None)
    S.get_seen_listings = lambda uid: set()
    S.get_blocked_links_and_fingerprints = lambda uid: (set(), set())
    S.get_recent_listing_signatures = lambda *a, **k: set()
    S.get_user_exclusions = lambda uid: []
    S.get_user_notification_prefs = lambda uid: {}
    S.save_listings_bulk = lambda uid, rows: set()
    S.stamp_terms_scraped = lambda uid, terms: None
    S.is_user_active = lambda *a, **k: True
    S.reset_geocode_budget = lambda *a, **k: None
    S.get_alerted_links = lambda uid: set()
    S.record_alerted_listings = lambda uid, rows: None


def unpatch():
    S.scrape_craigslist_for_user = _o['cl']
    S.scrape_offerup_for_user = _o['ou']
    S.scrape_mercari_for_user = _o['mc']
    S.scrape_facebook_for_user = _o['fb']
    S.get_user_search_terms = _o['terms']
    S.due_terms_for_user = _o['due']
    S.get_seen_listings = _o['seen']
    S.get_blocked_links_and_fingerprints = _o['blocked']
    S.get_recent_listing_signatures = _o['sigs']
    S.get_user_exclusions = _o['excl']
    S.get_user_notification_prefs = _o['prefs']
    S.save_listings_bulk = _o['save']
    S.stamp_terms_scraped = _o['stamp']
    S.is_user_active = _o['active']
    S.reset_geocode_budget = _o['geo']
    S.get_alerted_links = _o['alerted']
    S.record_alerted_listings = _o['rec']


CFG = {
    'user_id': 'timing-test-user', 'zip_code': '95212', 'search_radius': 25,
    'platforms': {'craigslist': True, 'offerup': True, 'mercari': True, 'facebook': True},
    'ai_enabled': False, 'ai_strictness': 'balanced', 'check_interval': 10,
    'plan_tier': 'pro', 'is_pro': True, 'buyer_include_local': True,
    'buyer_include_shipping': True, 'scan_description': False,
}


def run(offerup_fails=False):
    del LOGS[:]
    patch(offerup_fails=offerup_fails)
    try:
        S._scrape_for_user_impl(
            dict(CFG), log_callback=lambda uid, m, k='info': LOGS.append((m, k)), debug=True)
    finally:
        unpatch()
    return next((m for m, _ in LOGS if m.startswith('Scan timing:')), None)


t.section('1. The breakdown appears and is accurate')
line = run()
t.ok(line, 'a "Scan timing:" line was emitted')
print("  %s" % line)

parsed = dict((m.group(1), float(m.group(2)))
              for m in re.finditer(r'(\w+) (\d+(?:\.\d+)?)s \(', line or ''))
for label, burn in BURN.items():
    got = parsed.get(label)
    t.ok(got is not None, '%s appears in the breakdown' % label)
    if got is not None:
        # 0.5s tolerance: these are whole-second renderings of sub-second burns.
        t.ok(abs(got - burn) < 0.6, '%s reported ~%.1fs (burned %.2fs)' % (label, got, burn))

t.section('2. Ordered by cost, because that is what makes it actionable')
order = re.findall(r'(\w+) \d+(?:\.\d+)?s \(', line or '')
platforms_in_order = [o for o in order if o in BURN]
t.eq(platforms_in_order, ['Facebook', 'Craigslist', 'OfferUp', 'Mercari'],
     'platforms listed most-expensive first, not in dispatch order')
t.ok(order and order[-1] == 'other', "'other' comes last")

t.section('3. Totals reconcile')
m = re.match(r'Scan timing: (\d+(?:\.\d+)?)s total', line or '')
t.ok(m, 'total is reported')
if m:
    total = float(m.group(1))
    t.ok(total >= sum(BURN.values()) - 0.6,
         'total (%.0fs) is at least the sum of the parts' % total)
    other = parsed.get('other')
    t.ok(other is not None, "'other' is reported (unaccounted overhead)")
    if other is not None:
        t.ok(abs(total - (sum(v for k, v in parsed.items() if k != 'other') + other)) < 1.5,
             'parts plus other reconcile with the total')

t.section('4. A FAILING platform still reports the time it burned')
# A marketplace that takes 90s to time out is exactly the cost that needs to
# show up. Timing only the success path would hide it.
line2 = run(offerup_fails=True)
t.ok(line2, 'timing line still emitted when a platform raises')
print("  %s" % line2)
parsed2 = dict((mm.group(1), float(mm.group(2)))
               for mm in re.finditer(r'(\w+) (\d+(?:\.\d+)?)s \(', line2 or ''))
t.ok('OfferUp' in parsed2, 'the failed platform still appears')
t.ok(parsed2.get('OfferUp', 0) >= BURN['OfferUp'] - 0.6,
     'and its burned time is counted, not dropped')
t.ok(parsed2.get('Facebook', 0) >= BURN['Facebook'] - 0.6,
     'the other platforms still ran and are still timed')



t.section('5. The coalescing window is sized against a FALSIFIED premise')
# coalesce_window_sec()'s docstring says "Default 120s because a scrape takes
# roughly that long". Production says otherwise: 184-247s, median 202s. Terms
# coming due in the gap between the window and the real duration are NOT swept
# in, so they are due the instant the scrape ends and trigger a second, almost
# entirely redundant scrape — which is the exact waste coalescing exists to stop.
os.environ.pop('SCRAPE_COALESCE_WINDOW_SEC', None)
t.eq(S.coalesce_window_sec(), 120, 'default window is still 120s')

NOW = 1_000_000.0
MEASURED_SCRAPE_SEC = 202          # production median


def terms_due_at(*offsets):
    """Terms whose next-due time is NOW + offset, on a 10-minute interval."""
    out = {}
    for i, off in enumerate(offsets):
        out['term%d' % i] = {
            'min': None, 'max': None, 'exclusions': [], 'includes': [],
            'interval_minutes': 10,
            'last_scraped_ts': NOW + off - 600,   # due at NOW+off
        }
    return out


cfg = {'plan_tier': 'pro', 'check_interval': 10}
# term0 due now; term1 due 150s in — inside the real scrape, outside the window.
due, _ = S.due_terms_for_user(terms_due_at(0, 150), cfg, now=NOW)
t.eq(sorted(due), ['term0'],
     'a term due 150s in is NOT swept: it lands mid-scrape and starts a second one')

os.environ['SCRAPE_COALESCE_WINDOW_SEC'] = str(MEASURED_SCRAPE_SEC + 40)
t.eq(S.coalesce_window_sec(), MEASURED_SCRAPE_SEC + 40, 'window is configurable')
due, _ = S.due_terms_for_user(terms_due_at(0, 150), cfg, now=NOW)
t.eq(sorted(due), ['term0', 'term1'],
     'sized to the real duration, that term rides along instead')

# The acceleration stays bounded by the same argument already in the docstring:
# a term is never pulled in by more than the window, and a 5-minute term cannot
# be swept faster than its own interval allows.
due, _ = S.due_terms_for_user(terms_due_at(0, 999), cfg, now=NOW)
t.eq(sorted(due), ['term0'], 'a term far outside the window is still held back')
os.environ.pop('SCRAPE_COALESCE_WINDOW_SEC', None)

t.section('6. Which marketplaces actually ran (and which did not)')
# "Scan complete. No new matches found." reads identically whether all four were
# searched and found nothing, or one threw and was never searched. With shared
# accounts that is the difference between "no deals today" and "it is broken".
run()
scanned = [(m, k) for m, k in LOGS if m.startswith('Marketplaces scanned:')]
notrun = [(m, k) for m, k in LOGS if m.startswith('Marketplaces NOT scanned:')]
t.eq(len(scanned), 1, 'a healthy scan reports which marketplaces ran')
t.eq(len(notrun), 0, '...and reports nothing as failed')
for label in BURN:
    t.ok(label in scanned[0][0], '%s is listed as scanned' % label)
t.eq(scanned[0][1], 'info', 'the healthy line is info, not an error')
print("  %s" % scanned[0][0])

run(offerup_fails=True)
scanned = [(m, k) for m, k in LOGS if m.startswith('Marketplaces scanned:')]
notrun = [(m, k) for m, k in LOGS if m.startswith('Marketplaces NOT scanned:')]
t.eq(len(notrun), 1, 'a failed marketplace is reported separately')
t.ok('OfferUp' in notrun[0][0], 'and it names the one that failed')
t.ok('RuntimeError' in notrun[0][0], 'with the failure type, not just "failed"')
t.eq(notrun[0][1], 'error', 'THE FAILURE LINE IS RED')
t.ok(scanned and 'Facebook' in scanned[0][0],
     'the three that did run are still reported as scanned')
t.ok('OfferUp' not in scanned[0][0], 'the failed one is not claimed as scanned')
print("  %s" % notrun[0][0])

t.section('7. Descriptions unavailable warns loudly, and only when it should')
CAP = []


def cap(uid, m, k='info'):
    CAP.append((m, k))


# Nothing read, several checked: the OfferUp-403 case. Must be red.
del CAP[:]
S.warn_descriptions_unavailable('OfferUp', 0, 12, cap, 'u')
t.eq(len(CAP), 1, 'warns when a platform produced no descriptions at all')
t.eq(CAP[0][1], 'error', 'and it is RED')
t.ok('TITLES ONLY' in CAP[0][0], 'it says what actually happened to the filter')
t.ok('other marketplaces are unaffected' in CAP[0][0].lower(),
     'and scopes the damage so it does not read as total failure')
print("  %s" % CAP[0][0])

# Partial is normal — sellers leave descriptions blank. Warning here would train
# the user to ignore the line that matters.
del CAP[:]
S.warn_descriptions_unavailable('Facebook', 7, 3, cap, 'u')
t.eq(CAP, [], 'silent when SOME descriptions were read')
S.warn_descriptions_unavailable('Mercari', 0, 0, cap, 'u')
t.eq(CAP, [], 'silent when none were even wanted')
S.warn_descriptions_unavailable('Craigslist', 0, 5, None, 'u')
t.eq(CAP, [], 'no callback -> no crash')

t.done()
