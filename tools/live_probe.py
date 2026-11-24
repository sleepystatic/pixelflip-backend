# -*- coding: utf-8 -*-
"""
LIVE end-to-end probe: N users scanned at once through the REAL scrape pipeline
against the REAL marketplaces, with nothing written anywhere.

    .venv\\Scripts\\python.exe tools\\live_probe.py               # 5 users, 2 workers
    .venv\\Scripts\\python.exe tools\\live_probe.py --users 2     # lighter

What is real: every platform fetch and parser, process-wide pacing, the shared
search cache, filters, include/exclude, description scanning (Pro users — on
OfferUp that is the live listing-details lookup, one paced request per new
listing whose keywords need its body), the Mercari token replay,
scrape_for_user's lease/abort/timing code, and 2 workers in parallel the way
production runs them.

What is NOT touched: the database (every read/write the scan does is stubbed in
memory, and get_db_connection is a tripwire that refuses anything unexpected),
notifications (no email / push / SMS), the Mercari token store (read once,
READ-ONLY, never written back), Google Vision (image filter off).

Traffic is bounded: *_MAX_ROWS_PER_TERM = 4, three overlapping terms, one scan
per user. Roughly one user's first scan per user, which is what production does
all day — but do not loop it; OfferUp throttles on request rate.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

ap = argparse.ArgumentParser()
ap.add_argument('--users', type=int, default=5)
ap.add_argument('--pro', type=int, default=2, help='how many of the users are Pro (Facebook + descriptions)')
ap.add_argument('--workers', type=int, default=2)
ap.add_argument('--rows', type=int, default=4, help='max listings per term per platform')
args = ap.parse_args()

os.environ.update({
    'ENABLE_SCRAPER_THREAD': '0', 'ENABLE_CLEANUP_THREAD': '0', 'SCHEMA_ENSURE': '0',
    'MERCARI_ALLOW_CAPTURE': '0', 'DESCRIPTION_SCAN_ENABLED': '1', 'SEARCH_CACHE_ENABLED': '1',
    'AI_IMAGE_FILTER_PLATFORMS': '', 'FB_LEAN_FETCH': '1',
    'CRAIGSLIST_MAX_ROWS_PER_TERM': str(args.rows), 'OFFERUP_MAX_ROWS_PER_TERM': str(args.rows),
    'MERCARI_MAX_ROWS_PER_TERM': str(args.rows), 'FB_MAX_ROWS_PER_TERM': str(args.rows),
})
sys.path.insert(0, REPO)
os.chdir(REPO)
from dotenv import load_dotenv                     # noqa: E402
load_dotenv(os.path.join(REPO, '.env'))
import psycopg2                                     # noqa: E402
import scraper_multi_user as S                      # noqa: E402
import listing_notifications as LN                  # noqa: E402

# ------------------------------------------------------------ Mercari token: read-only
ctx = None
try:
    cx = psycopg2.connect(os.getenv('DATABASE_URL'), connect_timeout=15)
    cx.set_session(readonly=True)
    cur = cx.cursor()
    cur.execute("SELECT value FROM scraper_state WHERE key = %s", ('mercari_api_ctx',))
    row = cur.fetchone()
    cx.rollback()
    cx.close()
    payload = row[0] if row else None
    payload = json.loads(payload) if isinstance(payload, str) else payload
    ctx = (payload or {}).get('ctx')
except Exception as e:
    print('Mercari token read failed (%s) - Mercari will report unavailable' % type(e).__name__)
exp = S._mercari_token_expiry(ctx) if ctx else None
S._load_persisted_mercari_ctx = lambda: ctx
S._persist_mercari_ctx = lambda c: None            # NEVER write the shared token back
S._MERCARI_API_CTX = None

# ------------------------------------------------------------ database: stubbed + tripwire
TRIPPED = []


def _tripwire():
    TRIPPED.append(sys._getframe(1).f_code.co_name)
    raise RuntimeError('live_probe: database access refused')


S.get_db_connection = _tripwire
SAVED, ALERTED = defaultdict(set), defaultdict(set)
LOCK = threading.Lock()
TERMS = {}


def save(uid, rows):
    with LOCK:
        new = {r['link'] for r in rows if r['link'] not in SAVED[uid]}
        SAVED[uid] |= new
    return new


S.save_listings_bulk = save
S.record_alerted_listings = lambda uid, rows: ALERTED[uid].update(r['link'] for r in rows)
S.get_alerted_links = lambda uid: set(ALERTED[uid])
S.get_seen_listings = lambda uid: set(SAVED[uid])
S.get_blocked_links_and_fingerprints = lambda uid: (set(), set())
S.get_recent_listing_signatures = lambda *a, **k: []
S.get_user_search_terms = lambda uid: TERMS[uid]
S.stamp_terms_scraped = lambda uid, terms: None
S.get_user_exclusions = lambda uid: []
S.get_user_notification_prefs = lambda uid: {'notification_channels': {}}
S.get_user_auth_email = lambda uid: None
S.is_user_active = lambda *a, **k: True
S.claim_user_for_scrape = lambda uid, lease_sec=None: True
S.release_user_scrape_claim = lambda uid: None
S._stamp_user_scrape_complete = lambda uid, ms: None
S._clear_scrape_stamp_for_retry = lambda uid: None
S.check_image_with_ai = lambda *a, **k: True
S._save_city_cache = lambda *a, **k: None          # geocode cache is a tracked repo file
LN.dispatch_scrape_digest_notifications = lambda *a, **k: []

# ------------------------------------------------------------ users
POOL = ['nintendo switch', 'ps5', 'gameboy']
USERS = []
for i in range(args.users):
    uid = 'probe%03d-0000-4000-8000-000000000000' % i
    pro = i < args.pro
    terms = [POOL[i % 3], POOL[(i + 1) % 3]]
    TERMS[uid] = {t: {'min': None, 'max': None, 'includes': [],
                      # An exclusion makes description scanning matter: a title
                      # that passes may still be excluded by its body.
                      'exclusions': ['broken', 'for parts'] if pro else [],
                      'is_priority': False, 'interval_minutes': 10, 'last_scraped_ts': 0.0}
                  for t in terms}
    USERS.append({'user_id': uid, 'zip_code': '95212', 'search_radius': 25,
                  'platforms': {'craigslist': True, 'offerup': True, 'mercari': True, 'facebook': pro},
                  'ai_enabled': False, 'ai_strictness': 'balanced', 'check_interval_minutes': 10,
                  'plan_tier': 'pro' if pro else 'basic', 'is_pro': pro, 'scan_description': pro,
                  'buyer_include_local': True, 'buyer_include_shipping': True})

LINES = defaultdict(list)


def log_cb(uid, msg, kind='info'):
    with LOCK:
        LINES[uid].append((kind, msg))


TIMING = re.compile(r'(\w+) (\d+(?:\.\d+)?)s \(')


def run(user):
    t0 = time.time()
    try:
        S.scrape_for_user(dict(user), log_callback=log_cb, debug=True)
        err = None
    except Exception as e:
        err = '%s: %s' % (type(e).__name__, str(e)[:120])
    return user['user_id'], time.time() - t0, err


print('LIVE PROBE  users=%d (pro %d)  workers=%d  rows/term=%d  mercari token: %s'
      % (args.users, args.pro, args.workers, args.rows,
         ('valid %.0fh' % ((exp - time.time()) / 3600)) if exp and exp > time.time() else 'MISSING/EXPIRED'))
wall0 = time.time()
with ThreadPoolExecutor(max_workers=args.workers) as pool:
    results = list(pool.map(run, USERS))
wall = time.time() - wall0

print('=' * 78)
tot = defaultdict(float)
for uid, dur, err in results:
    u = next(x for x in USERS if x['user_id'] == uid)
    lines = LINES[uid]
    timing = next((m for k, m in lines if m.startswith('Scan timing:')), '')
    plats = {m.group(1): float(m.group(2)) for m in TIMING.finditer(timing)}
    for k, v in plats.items():
        tot[k] += v
    scanned = next((m for k, m in lines if m.startswith('Marketplaces scanned:')), '')
    not_scanned = next((m for k, m in lines if m.startswith('Marketplaces NOT scanned:')), '')
    desc_warn = [m for k, m in lines if 'descriptions unavailable' in m]
    errors = [m for k, m in lines if k == 'error' and 'descriptions unavailable' not in m
              and not m.startswith('Marketplaces NOT scanned')]
    print('%s %-5s terms=%s  %5.1fs  saved %d' % (uid[:8], u['plan_tier'], list(TERMS[uid]), dur, len(SAVED[uid])))
    print('    %s' % (scanned or '(no "Marketplaces scanned" line)'))
    if not_scanned:
        print('    !! %s' % not_scanned)
    for w in desc_warn:
        print('    ~  %s' % w[:150])
    for e in errors[:4]:
        print('    !! %s' % e[:150])
    if err:
        print('    !! raised %s' % err)
    if plats:
        print('    timing: %s' % ', '.join('%s %.1fs' % kv for kv in sorted(plats.items(), key=lambda kv: -kv[1])))
print('=' * 78)
print('wall clock for all %d users: %.1fs  |  summed per-platform time: %s'
      % (args.users, wall, ', '.join('%s %.0fs' % kv for kv in sorted(tot.items(), key=lambda kv: -kv[1]))))
import search_cache as SC                           # noqa: E402
st = SC.stats()
print('shared search cache: hits %d, misses %d, stores %d' % (st.get('hits', 0), st.get('misses', 0), st.get('stores', 0)))
print('OfferUp item-page breaker: %s' % ('OPEN (stopped asking)' if S._ou_item_wall_open() else 'closed'))
print('database tripwire: %s' % ('NOT touched' if not TRIPPED else 'refused %d call(s) from %s' % (len(TRIPPED), sorted(set(TRIPPED)))))
