# -*- coding: utf-8 -*-
"""
START/STOP chaos test of the REAL scheduler, lease and abort paths (2026-11-23).

    .venv\\Scripts\\python.exe tests\\loadtest_startstop.py            # 6 users, 2 workers
    .venv\\Scripts\\python.exe tests\\loadtest_startstop.py --users 8 --seed 7

Same harness idea as loadtest_multiuser.py (virtual clock, replayed marketplace
payloads through the real parsers, in-memory database with a tripwire for any
unfaked SQL — nothing reaches production or a marketplace), plus:

  * the armed state is MUTABLE and read through the real SQL paths:
    get_active_users (scheduler), is_user_active (mid-scan abort checks), and the
    cross-instance lease UPDATEs (claim / release) all hit the fake table;
  * a chaos thread stops users mid-scan, flips random users, and does rapid
    STOP+START on users that are scanning, the way the API does it (flip
    is_active, invalidate the active cache), then leaves everyone armed for a
    quiet final stretch.

Invariants checked (exit 1 if a HARD one breaks):
  H1 never two scans of the same user at once (even across a rapid STOP+START)
  H2 no scan STARTS doing platform work for a user who was off the whole time
  H3 no alert digest goes out for a user whose scanner is OFF at that moment
  H4 every user is scanned again after their final re-arm (nobody gets stuck)
  H5 no listing is alerted twice
  H6 every lease is released once scans finish
  H7 a scan that ends with the scanner OFF does not stamp completion
  H8 no unfaked SQL, tracebacks or error lines from the scheduler
"""
import argparse
import hashlib
import io
import json
import math
import os
import random
import re
import sys
import threading
import time as _rt
from collections import defaultdict
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = __import__('tempfile').gettempdir()

ap = argparse.ArgumentParser()
ap.add_argument('--users', type=int, default=6)
ap.add_argument('--pro-frac', type=float, default=0.5)
ap.add_argument('--workers', type=int, default=2)
ap.add_argument('--scale', type=float, default=20.0)
ap.add_argument('--minutes', type=float, default=40.0, help='virtual minutes of chaos + quiet tail')
ap.add_argument('--quiet', type=float, default=14.0, help='final virtual minutes with no toggles')
ap.add_argument('--toggle-every', type=float, default=35.0, help='virtual seconds between chaos events')
ap.add_argument('--seed', type=int, default=20261123)
args = ap.parse_args()

os.environ.update({
    'DATABASE_URL': 'postgresql://invalid.invalid:1/none',
    'ENABLE_SCRAPER_THREAD': '0', 'ENABLE_CLEANUP_THREAD': '0', 'SCHEMA_ENSURE': '0',
    'SCRAPE_MAX_WORKERS': str(args.workers),
    'DESCRIPTION_SCAN_ENABLED': '0', 'MERCARI_ALLOW_CAPTURE': '0', 'OFFERUP_LISTING_API': '0',
    'FB_LEAN_FETCH': '1', 'OFFERUP_BROWSERLESS': '1', 'AI_IMAGE_FILTER_PLATFORMS': '',
})
sys.path.insert(0, REPO)
os.chdir(REPO)
import scraper_multi_user as S          # noqa: E402
import listing_notifications as LN      # noqa: E402
import search_cache as SC               # noqa: E402

RNG = random.Random(args.seed)


class VClock(object):
    def __init__(self, k):
        self.k, self.m0, self.t0 = k, _rt.monotonic(), _rt.time()

    def time(self):
        return self.t0 + (_rt.monotonic() - self.m0) * self.k

    def sleep(self, s):
        if s and s > 0:
            _rt.sleep(s / self.k)

    def monotonic(self):
        return self.m0 + (_rt.monotonic() - self.m0) * self.k

    def perf_counter(self):
        return self.monotonic()

    def __getattr__(self, name):
        return getattr(_rt, name)


VC = VClock(args.scale)
S.time = VC
SC.time = VC

# ---------------------------------------------------------------- users & terms
POOL = ['2ds', '3ds', 'gameboy advance sp', '2ds xl', '3ds xl', 'ds lite', 'gameboy advance',
        'gameboy color', 'gameboy micro', 'ps2', 'switch oled', 'psp', 'n64', 'gamecube',
        'wii u', 'ps vita', 'snes', 'sega genesis', 'dreamcast', 'xbox 360']
USERS, TERMS, UCFG = {}, {}, {}
n_pro = int(round(args.users * args.pro_frac))
for i in range(args.users):
    uid = '%08x-0000-4000-8000-%012x' % (i, i)
    pro = i < n_pro
    rot = [POOL[(i * 3 + j) % len(POOL)] for j in range(len(POOL))]
    plats = {'craigslist': True, 'offerup': True, 'mercari': True, 'facebook': pro}
    n_terms = 10 if pro else 3
    TERMS[uid] = {t: {'is_priority': pro and j < 3, 'interval_minutes': 5 if (pro and j < 3) else 10,
                      'last_scraped_ts': None} for j, t in enumerate(rot[:n_terms])}
    USERS[uid] = {'user_id': uid, 'zip_code': '95212', 'search_radius': 25, 'platforms': plats,
                  'ai_enabled': False, 'ai_strictness': 'balanced', 'check_interval_minutes': 10,
                  'plan_tier': 'pro' if pro else 'basic', 'is_pro': pro,
                  'buyer_include_local': True, 'buyer_include_shipping': True,
                  'last_scraped_ts': None}
    UCFG[uid] = {'plan_tier': 'pro' if pro else 'basic', 'check_interval_minutes': 10}

LOCK = threading.Lock()
ARMED = {u: True for u in USERS}
ARM_LOG = defaultdict(list)          # uid -> [(t, armed)]
CLAIMED = {}                          # uid -> lease expiry (virtual)
SQL_ERRORS = []
for u in USERS:
    ARM_LOG[u].append((0.0, True))


def armed_at(uid, t):
    state = True
    for when, s in ARM_LOG[uid]:
        if when <= t:
            state = s
        else:
            break
    return state


def off_throughout(uid, t0, t1):
    """True when the scanner was OFF for the whole [t0, t1] window."""
    if armed_at(uid, t0):
        return False
    return not any(s for when, s in ARM_LOG[uid] if t0 <= when <= t1)


class FakeCursor(object):
    def __init__(self):
        self.rows, self.one = [], None

    def execute(self, sql, a=None):
        s = ' '.join(sql.split())
        a = a or ()
        now = VC.time()
        self.rows, self.one = [], None
        if 'FROM user_search_terms' in s:
            self.rows = [{'user_id': u, 'search_term': t, 'is_priority': c['is_priority'],
                          'interval_minutes': c['interval_minutes'], 'last_scraped_ts': c['last_scraped_ts']}
                         for u, ts in TERMS.items() for t, c in ts.items()]
        elif 'FROM user_settings' in s and 'is_active = TRUE' in s:
            with LOCK:
                self.rows = [dict(u) for uid, u in USERS.items() if ARMED[uid]]
        elif s.startswith('SELECT is_active FROM user_settings WHERE user_id'):
            with LOCK:
                self.one = (ARMED.get(a[0], False),)
        elif 'SET scrape_claimed_until = NOW() +' in s:
            lease, uid = a
            with LOCK:
                if CLAIMED.get(uid, 0) < now:
                    CLAIMED[uid] = now + float(lease)
                    self.one = (uid,)
        elif s.startswith('UPDATE user_settings SET scrape_claimed_until = NULL'):
            with LOCK:
                CLAIMED.pop(a[0], None)
        else:
            with LOCK:
                SQL_ERRORS.append(s[:140])
            raise AssertionError('UNFAKED SQL reached the fake DB: ' + s[:140])

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one if self.one is not None else (self.rows[0] if self.rows else None)

    def close(self):
        pass


class FakeConn(object):
    def cursor(self, cursor_factory=None):
        return FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


S.get_db_connection = lambda: FakeConn()

# ---------------------------------------------------------------- replayed marketplaces
SIM_T0 = [None]
TL = threading.local()


def fresh_ids(platform, term, n):
    ids = [hashlib.sha1(('%s|%s|%d' % (platform, term, i)).encode()).hexdigest()[:12] for i in range(n)]
    if SIM_T0[0] is not None:
        b = int((VC.time() - SIM_T0[0]) // (4 * 60))          # new listings every 4 virtual min
        ids += [hashlib.sha1(('%s|%s|fresh|%d' % (platform, term, k)).encode()).hexdigest()[:12]
                for k in range(1, b + 1)]
    return ids


def price_of(h):
    return float(40 + int(h[:2], 16) % 160)


FB_HTML = io.open(os.path.join(REPO, 'tests', 'fixtures', 'fb_item_1053796454323946.html'), encoding='utf-8').read()
_real_fb_collect = S._fb_collect_from_json
OU_PAYLOAD = json.load(open(os.path.join(REPO, 'tests', 'fixtures', 'offerup_search_graphql.json'), 'rb'))
PLATFORM_CALLS = defaultdict(list)   # uid -> [(t, platform, armed_at_call)]


def note_platform(platform):
    rec = getattr(TL, 'scan', None)
    if rec is not None:
        with LOCK:
            PLATFORM_CALLS[rec['uid']].append((VC.time(), platform, ARMED[rec['uid']]))


def fake_fb_fetch(url, label='', log_callback=None, user_id=None, debug=False):
    note_platform('facebook')
    VC.sleep(2.0)
    TL.fb_term = label
    return FB_HTML, True


def fake_fb_collect(html):
    _real_fb_collect(html)
    term = getattr(TL, 'fb_term', None)
    if not term:
        return []
    now = int(_rt.time())
    return [{'title': '%s facebook %s' % (term, h[:5]), 'price': price_of(h),
             'link': 'https://www.facebook.com/marketplace/item/%d/' % (int(h, 16) % 10 ** 15),
             'image_url': None, 'city': 'Stockton, CA', 'creation_time': now - 3600,
             'is_local': True, 'is_shipping': False} for h in fresh_ids('facebook', term, 10)]


def fake_ou_curl(term, lat, lon, force_no_proxy=False):
    note_platform('offerup')
    VC.sleep(0.8)
    S._offerup_tiles_to_items(OU_PAYLOAD)
    return [{'title': '%s offerup %s' % (term, h[:5]), 'price': price_of(h),
             'link': 'https://offerup.com/item/detail/%s' % h, 'image_url': None,
             'city': 'Stockton, CA', 'is_local': True, 'is_shipping': False}
            for h in fresh_ids('offerup', term, 12)], None


def fake_mc_api(terms, max_rows):
    note_platform('mercari')
    terms = list(terms)
    VC.sleep(1.5 + 0.3 * len(terms))
    return ({t: [{'title': '%s mercari %s' % (t, h[:5]), 'price': price_of(h),
                  'link': 'https://www.mercari.com/us/item/m%d/' % (int(h, 16) % 10 ** 11),
                  'image_url': 'http://img/x.jpg', 'listed_at': None}
                 for h in fresh_ids('mercari', t, 12)] for t in terms}, None)


def fake_cl(user_id, zip_code, search_radius, search_terms, exclusions, ai_enabled, ai_strictness,
            debug=False, log_callback=None, known_links=None, scan_description=False):
    note_platform('craigslist')
    out = []
    for term in search_terms:
        VC.sleep(0.7)
        for h in fresh_ids('craigslist', term, 4):
            link = 'https://sfbay.craigslist.org/vgm/d/%s.html' % h
            if known_links and link in known_links:
                continue
            out.append({'title': '%s craigslist %s' % (term, h[:5]), 'price': price_of(h), 'link': link,
                        'platform': 'Craigslist', 'console_type': term, 'threshold': None,
                        'image_url': None, 'listed_at': datetime.now(timezone.utc).isoformat(),
                        'location': 'Stockton'})
    return out


S._fb_browserless_fetch = fake_fb_fetch
S._fb_collect_from_json = fake_fb_collect
S._fb_html_is_blocked = lambda html, lean=False: False
S._offerup_fetch_via_curl = fake_ou_curl
S._mercari_fetch_via_api = fake_mc_api
S._mercari_in_cooldown = lambda *a, **k: 0
S.scrape_craigslist_for_user = fake_cl

# ---------------------------------------------------------------- persistence / notifications
SAVED, ALERTED, NOTIFIED = defaultdict(set), defaultdict(set), defaultdict(set)
DIGESTS, DUPES, STAMPS, STOP_CLEARS = [], [0], [], []


def fake_save(uid, rows):
    with LOCK:
        new = {l['link'] for l in rows if l['link'] not in SAVED[uid]}
        SAVED[uid] |= new
    return new


def fake_dispatch(user_id, new_listings, prefs, user_email=None, **k):
    with LOCK:
        DIGESTS.append((VC.time(), user_id, len(new_listings), ARMED[user_id]))
        for l in new_listings:
            if l['link'] in NOTIFIED[user_id]:
                DUPES[0] += 1
            NOTIFIED[user_id].add(l['link'])
    return []


def fake_stamp_terms(uid, terms):
    now = VC.time()
    for t in terms:
        TERMS[uid][t]['last_scraped_ts'] = now


def fake_terms(uid):
    return {t: {'min': None, 'max': None, 'exclusions': [], 'includes': [],
                'is_priority': c['is_priority'], 'interval_minutes': c['interval_minutes'],
                'last_scraped_ts': c['last_scraped_ts'] or 0.0} for t, c in TERMS[uid].items()}


S.save_listings_bulk = fake_save
S.record_alerted_listings = lambda uid, rows: ALERTED[uid].update(l['link'] for l in rows)
S.get_alerted_links = lambda uid: set(ALERTED[uid])
S.get_seen_listings = lambda uid: set(SAVED[uid])
S.get_blocked_links_and_fingerprints = lambda uid: (set(), set())
S.get_recent_listing_signatures = lambda *a, **k: []
S.get_user_search_terms = fake_terms
S.stamp_terms_scraped = fake_stamp_terms
S.get_user_exclusions = lambda uid: []
S.get_user_notification_prefs = lambda uid: {'notification_channels': {'email': True}}
S.get_user_auth_email = lambda uid: '%s@example.invalid' % uid[:8]
S.reset_geocode_budget = lambda *a, **k: None
S.city_to_latlon = lambda city: (37.957, -121.291)
S._zip_to_latlon = lambda z: (38.0316, -121.2591)
S.check_image_with_ai = lambda *a, **k: True
S._patchright_clean_ua = lambda: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/150.0.0.0'
S._stamp_user_scrape_complete = lambda uid, ms: STAMPS.append((VC.time(), uid, ARMED[uid]))
S._clear_scrape_stamp_for_retry = lambda uid: STOP_CLEARS.append((VC.time(), uid))
LN.dispatch_scrape_digest_notifications = fake_dispatch

# ---------------------------------------------------------------- per-scan instrumentation
SCANS, IN_FLIGHT, MAX_PER_USER, ERRORS = [], defaultdict(int), defaultdict(int), []
_real_impl = S._scrape_for_user_impl


def impl_wrapper(user_config, log_callback=None, debug=False):
    uid = user_config['user_id']
    rec = {'uid': uid, 'start': VC.time(), 'armed_at_start': ARMED[uid]}
    TL.scan = rec
    with LOCK:
        IN_FLIGHT[uid] += 1
        MAX_PER_USER[uid] = max(MAX_PER_USER[uid], IN_FLIGHT[uid])
    try:
        return _real_impl(user_config, log_callback=log_callback, debug=debug)
    finally:
        rec['end'] = VC.time()
        rec['armed_at_end'] = ARMED[uid]
        with LOCK:
            IN_FLIGHT[uid] -= 1
            SCANS.append(rec)
        TL.scan = None


S._scrape_for_user_impl = impl_wrapper


def log_cb(uid, msg, kind='info'):
    if kind == 'error':
        with LOCK:
            ERRORS.append((uid[:8], msg[:160]))


# ---------------------------------------------------------------- chaos
EVENTS = []


def set_armed(uid, on, why):
    """What /api/start and /api/stop do to the state the scraper reads."""
    t = VC.time() - SIM_T0[0]
    with LOCK:
        ARMED[uid] = on
        ARM_LOG[uid].append((VC.time(), on))
        EVENTS.append((t, uid[:8], 'START' if on else 'STOP', why, IN_FLIGHT[uid] > 0))
    S._invalidate_active_cache(uid)


def chaos():
    end_chaos = SIM_T0[0] + (args.minutes - args.quiet) * 60
    while VC.time() < end_chaos:
        VC.sleep(args.toggle_every * (0.5 + RNG.random()))
        scanning = [u for u in USERS if IN_FLIGHT[u] > 0 and ARMED[u]]
        roll = RNG.random()
        if roll < 0.4 and scanning:
            u = RNG.choice(scanning)
            set_armed(u, False, 'stop mid-scan')
            if RNG.random() < 0.6:                       # most come back later
                VC.sleep(RNG.uniform(20, 120))
                set_armed(u, True, 're-arm after mid-scan stop')
        elif roll < 0.7:
            u = RNG.choice(list(USERS))
            set_armed(u, not ARMED[u], 'random flip')
        else:
            pool = scanning or [u for u in USERS if ARMED[u]]
            if pool:
                u = RNG.choice(pool)
                set_armed(u, False, 'rapid stop')
                VC.sleep(RNG.uniform(0.5, 2.0))
                set_armed(u, True, 'rapid start')
    for u in USERS:                                     # quiet tail: everyone back on
        if not ARMED[u]:
            set_armed(u, True, 'final re-arm')


# ---------------------------------------------------------------- run
real_stdout = sys.stdout
log_path = os.path.join(OUT, 'loadtest_startstop.log')
sys.stdout = io.open(log_path, 'w', encoding='utf-8', errors='replace')
SIM_T0[0] = VC.time()
threading.Thread(target=S.main, kwargs={'log_callback': log_cb}, daemon=True).start()
ch = threading.Thread(target=chaos, daemon=True)
ch.start()
_rt.sleep(args.minutes * 60.0 / args.scale)
# Let in-flight scans finish (up to 3 virtual minutes) before checking leases.
deadline = _rt.monotonic() + 180.0 / args.scale
while _rt.monotonic() < deadline and any(IN_FLIGHT.values()):
    _rt.sleep(0.05)
sys.stdout.flush()
sys.stdout = real_stdout
log_text = io.open(log_path, encoding='utf-8', errors='replace').read()

# ---------------------------------------------------------------- invariants
t0 = SIM_T0[0]
hard = []


def check(ok, label, detail=''):
    print('  %s  %s%s' % ('PASS' if ok else 'FAIL', label, ('  -- ' + detail) if detail and not ok else ''))
    if not ok:
        hard.append(label)


print('=' * 78)
print('START/STOP CHAOS  users=%d (pro %d, basic %d)  workers=%d  virtual %.0f min @ %gx  seed %d'
      % (args.users, n_pro, args.users - n_pro, args.workers, args.minutes, args.scale, args.seed))
print('=' * 78)
kinds = defaultdict(int)
for e in EVENTS:
    kinds[e[3]] += 1
print('chaos events: %d  (%s)  — %d hit a scan in flight'
      % (len(EVENTS), ', '.join('%s %d' % kv for kv in sorted(kinds.items())), sum(1 for e in EVENTS if e[4])))
print('scans: %d   aborted by STOP (stamp cleared): %d   completed (stamped): %d   digests: %d'
      % (len(SCANS), len(STOP_CLEARS), len(STAMPS), len(DIGESTS)))

check(max(MAX_PER_USER.values() or [0]) <= 1, 'H1 never two scans of one user at once',
      str(dict((u[:8], n) for u, n in MAX_PER_USER.items() if n > 1)))
bad_starts = [s for s in SCANS if off_throughout(s['uid'], s['start'], s['end'])
              and any(t >= s['start'] and t <= s['end'] for t, _p, _a in PLATFORM_CALLS[s['uid']])]
check(not bad_starts, 'H2 no marketplace work for a user who was OFF the whole scan',
      '%d scans' % len(bad_starts))
late_digests = [d for d in DIGESTS if not d[3]]
check(not late_digests, 'H3 no alert digest sent while that user\'s scanner was OFF',
      '%d digests, e.g. %s' % (len(late_digests), [(round(d[0] - t0), d[1][:8], d[2]) for d in late_digests[:3]]))
stuck = []
for u in USERS:
    last_on = max(t for t, s in ARM_LOG[u] if s)
    if not any(s['uid'] == u and s['start'] >= last_on and s.get('armed_at_end') for s in SCANS):
        stuck.append(u[:8])
check(not stuck, 'H4 every user scanned again after their final re-arm', 'stuck: %s' % stuck)
check(DUPES[0] == 0, 'H5 no listing alerted twice', '%d duplicates' % DUPES[0])
check(not CLAIMED, 'H6 every lease released', 'still claimed: %s' % [u[:8] for u in CLAIMED])
bad_stamps = [x for x in STAMPS if not x[2]]
check(not bad_stamps, 'H7 a scan ending with the scanner OFF never stamps completion',
      '%d stamps while off' % len(bad_stamps))
tb = log_text.count('Traceback')
check(not SQL_ERRORS and not tb and not ERRORS, 'H8 no unfaked SQL, tracebacks or error lines',
      'sql %d, tracebacks %d, errors %s' % (len(SQL_ERRORS), tb, ERRORS[:3]))

durs = sorted(s['end'] - s['start'] for s in SCANS if s.get('armed_at_end'))
if durs:
    print('completed-scan duration: p50 %.0fs  p95 %.0fs  max %.0fs  (virtual)'
          % (durs[len(durs) // 2], durs[int(0.95 * (len(durs) - 1))], durs[-1]))
print('log: %s' % log_path)
sys.exit(1 if hard else 0)
