# -*- coding: utf-8 -*-
# Multi-user load test of the REAL scheduler (main) and REAL scrape pipeline
# (_scrape_for_user_impl, platform scrapers, filters, dedup, digest), on an
# accelerated virtual clock.
#
#   * Marketplace transport is REPLAYED: real parsers run on real captured
#     payloads (FB item page fixture, live OfferUp GraphQL response) so CPU and
#     memory are realistic; network latency is modelled from measurements.
#   * Database and notifications are in-memory fakes. A tripwire raises on any
#     SQL that is not explicitly faked, so nothing can reach production.
#   * time.sleep / time.time inside scraper_multi_user run on a virtual clock K×
#     faster than real time, so every interval, pause and sleep keeps its ratio.
#     CPU work is NOT compressed (it is real), which slightly inflates virtual
#     durations; per-scan CPU is reported separately and exactly.
import argparse
import ctypes
import ctypes.wintypes
import hashlib
import io
import json
import math
import os
import re
import sys
import threading
import time as _rt
from collections import defaultdict
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = __import__('tempfile').gettempdir()      # logs go to temp, never the repo

ap = argparse.ArgumentParser()
ap.add_argument('--users', type=int, default=10)
ap.add_argument('--pro-frac', type=float, default=1.0, help='fraction of users that are max-config Pro')
ap.add_argument('--workers', type=int, default=1, help='SCRAPE_MAX_WORKERS')
ap.add_argument('--scale', type=float, default=10.0, help='virtual seconds per real second')
ap.add_argument('--minutes', type=float, default=45.0, help='virtual minutes to simulate')
ap.add_argument('--fresh-every', type=float, default=20.0, help='virtual minutes between new listings per (platform, term)')
ap.add_argument('--tag', default='run')
args = ap.parse_args()

os.environ.update({
    'DATABASE_URL': 'postgresql://invalid.invalid:1/none',
    'ENABLE_SCRAPER_THREAD': '0', 'ENABLE_CLEANUP_THREAD': '0',
    'SCRAPE_MAX_WORKERS': str(args.workers),
    'DESCRIPTION_SCAN_ENABLED': '0', 'MERCARI_ALLOW_CAPTURE': '0',
    'FB_LEAN_FETCH': '1', 'OFFERUP_BROWSERLESS': '1',
    'AI_IMAGE_FILTER_PLATFORMS': '',
})
sys.path.insert(0, REPO)
os.chdir(REPO)
import scraper_multi_user as S          # noqa: E402
import listing_notifications as LN      # noqa: E402


# ---------------------------------------------------------------- memory (Windows)
class _PMC(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]


if os.name == 'nt':
    _k32 = ctypes.windll.kernel32
    _k32.GetCurrentProcess.restype = ctypes.c_void_p
    _psapi = ctypes.windll.psapi
    _psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]

    def ws_mb():
        p = _PMC()
        p.cb = ctypes.sizeof(_PMC)
        _psapi.GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(p), p.cb)
        return p.WorkingSetSize / 1048576.0
else:
    def ws_mb():
        try:
            with open('/proc/self/status') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        return int(line.split()[1]) / 1024.0
        except Exception:
            pass
        return 0.0


# ---------------------------------------------------------------- virtual clock
class VClock(object):
    def __init__(self, k):
        self.k = k
        self.m0 = _rt.monotonic()
        self.t0 = _rt.time()

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
import search_cache as SC  # noqa: E402
SC.time = VC               # TTLs must run on the same clock as everything else

# Measured / documented real-world latencies, in (virtual) seconds.
NET = {
    'fb_search': 2.0,          # browserless FB search page (docstring measurement)
    'ou_fetch': 1.2,           # OfferUp warm-up GET + GraphQL POST (measured 0.7 + 0.5)
    'mc_base': 1.5, 'mc_per_term': 0.3,   # Mercari search API replay (prod log ~2s for 1 term)
    'cl_search': 0.7,          # Craigslist search page
    'cl_detail': 0.75,         # Craigslist detail page + polite delay (6 in parallel)
}

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
    if pro:
        plats = {'craigslist': True, 'offerup': True, 'mercari': True, 'facebook': True}
        terms = {t: {'is_priority': j < 3, 'interval_minutes': 5 if j < 3 else 10, 'last_scraped_ts': None}
                 for j, t in enumerate(rot[:10])}
    else:
        plats = {'craigslist': True, 'offerup': True, 'mercari': True, 'facebook': False}
        terms = {t: {'is_priority': False, 'interval_minutes': 10, 'last_scraped_ts': None}
                 for t in rot[:4]}
    USERS[uid] = {'user_id': uid, 'zip_code': '95212', 'search_radius': 25, 'platforms': plats,
                  'ai_enabled': False, 'ai_strictness': 'balanced', 'check_interval_minutes': 10,
                  'plan_tier': 'pro' if pro else 'basic', 'is_pro': pro,
                  'buyer_include_local': True, 'buyer_include_shipping': True,
                  'last_scraped_ts': None}
    TERMS[uid] = terms
    UCFG[uid] = {'plan_tier': 'pro' if pro else 'basic', 'check_interval_minutes': 10}


class FakeCursor(object):
    def __init__(self):
        self.rows = []

    def execute(self, sql, args_=None):
        s = ' '.join(sql.split())
        if 'FROM user_search_terms' in s:
            self.rows = [{'user_id': u, 'search_term': t, 'is_priority': c['is_priority'],
                          'interval_minutes': c['interval_minutes'],
                          'last_scraped_ts': c['last_scraped_ts']}
                         for u, ts in TERMS.items() for t, c in ts.items()]
        elif 'FROM user_settings' in s and 'is_active = TRUE' in s:
            self.rows = [dict(u) for u in USERS.values()]
        else:
            raise AssertionError('UNFAKED SQL reached the fake DB: ' + s[:140])

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

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

# ---------------------------------------------------------------- in-memory state
LOCK = threading.Lock()
SAVED = defaultdict(set)        # user -> links in "listings"
ALERTED = defaultdict(set)      # user -> links in alerted_listings
NOTIFIED = defaultdict(set)     # user -> links that went out in a digest
DIGESTS = defaultdict(list)     # user -> [count per digest]
DUPES = [0]
SCANS = []
LATE = {'priority': [], 'standard': []}
ERRORS = []
ACTIVE = [0, 0]                 # current, max
TL = threading.local()
SIM_T0 = [None]


def fresh_ids(platform, term, n):
    ids = [hashlib.sha1(('%s|%s|%d' % (platform, term, i)).encode()).hexdigest()[:12] for i in range(n)]
    if args.fresh_every > 0 and SIM_T0[0] is not None:
        b = int((VC.time() - SIM_T0[0]) // (args.fresh_every * 60))
        ids += [hashlib.sha1(('%s|%s|fresh|%d' % (platform, term, k)).encode()).hexdigest()[:12]
                for k in range(1, b + 1)]
    return ids


def price_of(h):
    return float(40 + int(h[:2], 16) % 160)


# --- Facebook: real parser on a real 929KB page, rows rewritten to the term.
FB_HTML = io.open(os.path.join(REPO, 'tests', 'fixtures', 'fb_item_1053796454323946.html'), encoding='utf-8').read()
_real_fb_collect = S._fb_collect_from_json


REQS = {'facebook': [], 'offerup': []}      # (start, end) virtual times per request


def fake_fb_fetch(url, label='', log_callback=None, user_id=None, debug=False):
    t0 = VC.time()
    VC.sleep(NET['fb_search'])
    with LOCK:
        REQS['facebook'].append((t0, VC.time()))
    TL.fb_term = label
    return FB_HTML, True


def fake_fb_collect(html):
    _real_fb_collect(html)                         # pay the real parse
    term = getattr(TL, 'fb_term', None)
    if not term:
        return []
    now = int(_rt.time())
    return [{'title': '%s facebook %s' % (term, h[:5]), 'price': price_of(h),
             'link': 'https://www.facebook.com/marketplace/item/%d/' % (int(h, 16) % 10 ** 15),
             'image_url': None, 'city': 'Stockton, CA', 'creation_time': now - 3600,
             'is_local': True, 'is_shipping': False}
            for h in fresh_ids('facebook', term, 14)]


S._fb_browserless_fetch = fake_fb_fetch
S._fb_collect_from_json = fake_fb_collect
S._fb_html_is_blocked = lambda html, lean=False: False

# --- OfferUp: real tile parser on the live GraphQL response.
OU_PAYLOAD = json.load(open(os.path.join(REPO, 'tests', 'fixtures', 'offerup_search_graphql.json'), 'rb'))


def fake_ou_curl(term, lat, lon, force_no_proxy=False):
    t0 = VC.time()
    # Session reuse (measured): first call on a thread pays the warm-up GET
    # (0.58s) + POST (0.56s); later calls reuse the cookie and pay only the POST,
    # until the session ages out (OFFERUP_SESSION_MAX_AGE_SEC, 600s).
    born = getattr(TL, 'ou_born', None)
    if born is None or VC.time() - born > 600:
        TL.ou_born = VC.time()
        VC.sleep(1.14)
    else:
        VC.sleep(0.56)
    with LOCK:
        REQS['offerup'].append((t0, VC.time()))
    S._offerup_tiles_to_items(OU_PAYLOAD)          # pay the real parse
    return [{'title': '%s offerup %s' % (term, h[:5]), 'price': price_of(h),
             'link': 'https://offerup.com/item/detail/%s' % h, 'image_url': None,
             'city': 'Stockton, CA', 'is_local': True, 'is_shipping': False}
            for h in fresh_ids('offerup', term, 16)], None


S._offerup_fetch_via_curl = fake_ou_curl

# --- Mercari: token replay is one call for many terms.
def fake_mc_api(terms, max_rows):
    terms = list(terms)
    VC.sleep(NET['mc_base'] + NET['mc_per_term'] * len(terms))
    return ({t: [{'title': '%s mercari %s' % (t, h[:5]), 'price': price_of(h),
                  'link': 'https://www.mercari.com/us/item/m%d/' % (int(h, 16) % 10 ** 11),
                  'image_url': 'http://img/x.jpg', 'listed_at': None}
                 for h in fresh_ids('mercari', t, 28)] for t in terms}, None)


S._mercari_fetch_via_api = fake_mc_api
S._mercari_in_cooldown = lambda *a, **k: 0


# --- Craigslist: stubbed at the platform level (plain HTTP; its cost is latency).
def fake_cl(user_id, zip_code, search_radius, search_terms, exclusions, ai_enabled, ai_strictness,
            debug=False, log_callback=None, known_links=None, scan_description=False):
    out = []
    for term in search_terms:
        VC.sleep(NET['cl_search'])
        new = []
        for h in fresh_ids('craigslist', term, 5):
            link = 'https://sfbay.craigslist.org/vgm/d/%s.html' % h
            if known_links and link in known_links:
                continue
            new.append((h, link))
        if new:
            VC.sleep(NET['cl_detail'] * math.ceil(len(new) / 6.0))
        for h, link in new:
            out.append({'title': '%s craigslist %s' % (term, h[:5]), 'price': price_of(h),
                        'link': link, 'platform': 'Craigslist', 'console_type': term,
                        'threshold': None, 'image_url': None,
                        'listed_at': datetime.now(timezone.utc).isoformat(),
                        'location': 'Stockton'})
    return out


S.scrape_craigslist_for_user = fake_cl

# --- persistence / user data / notifications
def fake_save(uid, rows):
    with LOCK:
        new = {l['link'] for l in rows if l['link'] not in SAVED[uid]}
        SAVED[uid] |= new
    return new


def fake_record_alerted(uid, rows):
    with LOCK:
        ALERTED[uid].update(l['link'] for l in rows)


def fake_terms(uid):
    return {t: {'min': None, 'max': None, 'exclusions': [], 'includes': [],
                'is_priority': c['is_priority'], 'interval_minutes': c['interval_minutes'],
                'last_scraped_ts': c['last_scraped_ts'] or 0.0}
            for t, c in TERMS[uid].items()}


def fake_stamp(uid, terms):
    now = VC.time()
    rec = getattr(TL, 'scan', None)
    start = rec['start'] if rec else now
    for t in terms:
        c = TERMS[uid][t]
        prev = c['last_scraped_ts']
        if prev:
            interval_s = S.term_interval_minutes(c, UCFG[uid]) * 60
            LATE['priority' if c['is_priority'] else 'standard'].append(start - (prev + interval_s))
        c['last_scraped_ts'] = now
    if rec is not None:
        rec['terms'] = len(terms)


def fake_dispatch(user_id, new_listings, prefs, user_email=None, **k):
    with LOCK:
        DIGESTS[user_id].append(len(new_listings))
        for l in new_listings:
            if l['link'] in NOTIFIED[user_id]:
                DUPES[0] += 1
            NOTIFIED[user_id].add(l['link'])
    return []


S.save_listings_bulk = fake_save
S.record_alerted_listings = fake_record_alerted
S.get_alerted_links = lambda uid: set(ALERTED[uid])
S.get_seen_listings = lambda uid: set(SAVED[uid])
S.get_blocked_links_and_fingerprints = lambda uid: (set(), set())
S.get_recent_listing_signatures = lambda *a, **k: []
S.get_user_search_terms = fake_terms
S.stamp_terms_scraped = fake_stamp
S.get_user_exclusions = lambda uid: []
S.get_user_notification_prefs = lambda uid: {'notification_channels': {'email': True}}
S.get_user_auth_email = lambda uid: '%s@example.invalid' % uid[:8]
S.is_user_active = lambda *a, **k: True
S.reset_geocode_budget = lambda *a, **k: None
S.city_to_latlon = lambda city: (37.957, -121.291)
S._zip_to_latlon = lambda z: (38.0316, -121.2591)
S.check_image_with_ai = lambda *a, **k: True
S._patchright_clean_ua = lambda: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/150.0.0.0'
S.claim_user_for_scrape = lambda uid, lease_sec=None: True
S.release_user_scrape_claim = lambda uid: None
S._stamp_user_scrape_complete = lambda uid, ms: None
S._clear_scrape_stamp_for_retry = lambda uid: None
LN.dispatch_scrape_digest_notifications = fake_dispatch

# --- per-scan instrumentation around the real implementation
_real_impl = S._scrape_for_user_impl


def impl_wrapper(user_config, log_callback=None, debug=False):
    rec = {'uid': user_config['user_id'], 'start': VC.time(), 'terms': None, 'timing': None,
           'new': 0, 'plan': user_config.get('plan_tier')}
    TL.scan = rec
    with LOCK:
        ACTIVE[0] += 1
        ACTIVE[1] = max(ACTIVE[1], ACTIVE[0])
    cpu0 = _rt.thread_time()
    try:
        return _real_impl(user_config, log_callback=log_callback, debug=debug)
    finally:
        rec['end'] = VC.time()
        rec['dur'] = rec['end'] - rec['start']
        rec['cpu'] = _rt.thread_time() - cpu0
        with LOCK:
            ACTIVE[0] -= 1
            SCANS.append(rec)
        TL.scan = None


S._scrape_for_user_impl = impl_wrapper

_TIMING = re.compile(r'(\w+) (\d+(?:\.\d+)?)s \(')


def log_cb(uid, msg, kind='info'):
    rec = getattr(TL, 'scan', None)
    if msg.startswith('Scan timing:') and rec is not None:
        rec['timing'] = {m.group(1): float(m.group(2)) for m in _TIMING.finditer(msg)}
    elif msg.startswith('New match') and rec is not None:
        rec['new'] += 1
    if kind == 'error':
        with LOCK:
            ERRORS.append((uid[:8], msg[:160]))


# ---------------------------------------------------------------- run
def pct(xs, p):
    if not xs:
        return float('nan')
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(math.ceil(p / 100.0 * len(xs))) - 1)]


real_stdout = sys.stdout
log_path = os.path.join(OUT, 'loadtest_%s.log' % args.tag)
sys.stdout = io.open(log_path, 'w', encoding='utf-8', errors='replace')
ws_base = ws_mb()
peak = [ws_base]
stop = threading.Event()


def sampler():
    while not stop.is_set():
        peak[0] = max(peak[0], ws_mb())
        _rt.sleep(0.2)


threading.Thread(target=sampler, daemon=True).start()
cpu_start = _rt.process_time()
SIM_T0[0] = VC.time()
threading.Thread(target=S.main, kwargs={'log_callback': log_cb}, daemon=True).start()
_rt.sleep(args.minutes * 60.0 / args.scale)
stop.set()
cpu_total = _rt.process_time() - cpu_start
with LOCK:
    scans = list(SCANS)
sys.stdout.flush()
sys.stdout = real_stdout

# ---------------------------------------------------------------- report
sim_min = args.minutes
print('=' * 78)
print('LOAD TEST [%s]  users=%d (pro max-config %d, basic %d)  SCRAPE_MAX_WORKERS=%d  '
      'virtual %.0f min @ %gx' % (args.tag, args.users, n_pro, args.users - n_pro, args.workers,
                                    sim_min, args.scale))
print('=' * 78)
print('scans completed: %d   max concurrent: %d   errors logged: %d   duplicate alerts: %d'
      % (len(scans), ACTIVE[1], len(ERRORS), DUPES[0]))
for kind, lab in (('pro', 'Pro'), ('basic', 'Basic')):
    ks = [s for s in scans if (s.get('plan') or '') == kind and s.get('terms')]
    if not ks:
        continue
    by_n = defaultdict(list)
    for s in ks:
        by_n[s['terms']].append(s)
    for n in sorted(by_n):
        g = by_n[n]
        durs = [s['dur'] for s in g]
        cpus = [s['cpu'] for s in g]
        plat = defaultdict(list)
        for s in g:
            for k, v in (s.get('timing') or {}).items():
                plat[k].append(v)
        pl = ', '.join('%s %.0fs' % (k, sum(v) / len(v)) for k, v in
                       sorted(plat.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])))
        print('  %-5s %2d-term scans: n=%-4d duration avg %5.1fs p95 %5.1fs | CPU/scan %.3fs | %s'
              % (lab, n, len(g), sum(durs) / len(durs), pct(durs, 95), sum(cpus) / len(cpus), pl))
for k in ('priority', 'standard'):
    xs = LATE[k]
    if xs:
        print('  %-8s term lateness (start vs due): p50 %6.1fs  p95 %6.1fs  max %6.1fs  (n=%d)'
              % (k, pct(xs, 50), pct(xs, 95), max(xs), len(xs)))
first = defaultdict(lambda: None)
for s in sorted(scans, key=lambda s: s['start']):
    if first[s['uid']] is None:
        first[s['uid']] = s['end'] - SIM_T0[0]
waits = [v for v in first.values() if v is not None]
print('  first-scan completion after arming: p50 %.0fs  max %.0fs  (users scanned at least once: %d/%d)'
      % (pct(waits, 50), max(waits) if waits else float('nan'), len(waits), args.users))
busy = sum(s['dur'] for s in scans)
print('  worker utilisation: %.0f%% of %d slot(s) busy' % (100.0 * busy / (sim_min * 60.0 * args.workers), args.workers))
digests = sum(len(v) for v in DIGESTS.values())
print('  digests sent: %d   listings alerted: %d   (alerted links recorded: %d)'
      % (digests, sum(sum(v) for v in DIGESTS.values()), sum(len(v) for v in ALERTED.values())))
cpu_scans = sum(s['cpu'] for s in scans)
print('  CPU: process %.1fs over %.0fs real  |  scans %.1fs  |  CPU per virtual minute %.2fs'
      % (cpu_total, sim_min * 60.0 / args.scale, cpu_scans, cpu_total / sim_min))
print('  memory (working set): baseline %.0fMB  peak %.0fMB  (+%.0fMB under load)'
      % (ws_base, peak[0], peak[0] - ws_base))
for plat, gap_env, gap_def in (('offerup', 'OFFERUP_TERM_DELAY_SEC', 3.0), ('facebook', 'FB_TERM_DELAY_SEC', 0.75)):
    rq = sorted(REQS[plat])
    if len(rq) < 2:
        continue
    gaps = [rq[i + 1][0] - rq[i][1] for i in range(len(rq) - 1)]   # end of one -> start of next
    overlap = sum(1 for g in gaps if g < -0.05)
    span = (rq[-1][1] - rq[0][0]) or 1.0
    print('  %-8s requests %4d | rate %.2f/min | min gap end->start %.2fs (configured %.2fs) | overlapping %d'
          % (plat, len(rq), 60.0 * len(rq) / span, min(gaps), float(os.getenv(gap_env, gap_def)), overlap))
if SC.enabled():
    st = SC.stats()
    print('  shared search cache: hits %d  misses %d  stores %d  (SEARCH_CACHE_ENABLED=1)'
          % (st.get('hits', 0), st.get('misses', 0), st.get('stores', 0)))
if ERRORS:
    print('  first errors:')
    for e in ERRORS[:6]:
        print('    ', e)
print('  scheduler log: %s' % log_path)
