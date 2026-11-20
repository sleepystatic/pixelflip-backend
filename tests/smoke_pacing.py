# -*- coding: utf-8 -*-
"""
Multi-user safety primitives (2026-11-02): process-wide pacing per marketplace,
the browser budget, continuous-dispatch fairness, and the merged Mercari cycle
cache. These are what make SCRAPE_MAX_WORKERS > 1 safe; each assertion is the
property that would silently break first.

Offline: no network, no database, no browser.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import scraper_multi_user as S

t = Suite('pacing')
LOCK = threading.Lock()

t.section('1. _paced: ONE request in flight per platform, with the gap between')
# Per-scrape pauses used to let N concurrent scrapes hit OfferUp N times as
# often; OfferUp's antibot is rate-based. Process-wide, they must queue.
os.environ['OFFERUP_TERM_DELAY_SEC'] = '0.2'
S._PACE_LAST_END['offerup'] = 0.0
spans = []


def ou_worker():
    for _ in range(3):
        with S._paced('offerup'):
            a = time.time()
            time.sleep(0.03)
            with LOCK:
                spans.append((a, time.time()))


threads = [threading.Thread(target=ou_worker) for _ in range(4)]
for th in threads:
    th.start()
for th in threads:
    th.join(30)
spans.sort()
gaps = [spans[i + 1][0] - spans[i][1] for i in range(len(spans) - 1)]
t.eq(len(spans), 12, 'all 12 requests from 4 concurrent scrapes ran')
t.ok(min(gaps) >= 0.19, 'end-to-start gap never under the configured 0.2s (min %.3f)' % min(gaps))

# Platforms pace independently: Facebook must not wait behind OfferUp.
os.environ['OFFERUP_TERM_DELAY_SEC'] = '3'
os.environ['FB_TERM_DELAY_SEC'] = '0'
S._PACE_LAST_END['offerup'] = time.time()
a = time.time()
with S._paced('facebook'):
    pass
t.ok(time.time() - a < 0.5, "Facebook is not held up by OfferUp's 3s gap")

t.section('2. cached= skips the request AND the gap')
# Users sharing a search must not queue 3s apart just to read the same rows.
S._PACE_LAST_END['offerup'] = time.time()        # a request just ended
a = time.time()
with S._paced('offerup', cached=lambda: ['row']) as slot:
    hit = slot['hit']
t.ok(time.time() - a < 0.5, 'a cache hit does not wait out the 3s gap')
t.eq(hit, ['row'], 'and is handed the cached rows')
before = S._PACE_LAST_END['offerup']
with S._paced('offerup', cached=lambda: ['row']):
    pass
t.eq(S._PACE_LAST_END['offerup'], before, 'a hit does not reset the gap clock (no request was made)')
os.environ['OFFERUP_TERM_DELAY_SEC'] = '0.2'
S._PACE_LAST_END['offerup'] = time.time()
a = time.time()
with S._paced('offerup', cached=lambda: None) as slot:
    miss = slot['hit']
t.ok(miss is None and time.time() - a >= 0.18, 'a miss still waits the gap before its request')

t.section('3. Browser budget: never more live browsers than BROWSER_MAX_CONCURRENT')
t.eq(S._browser_budget(), 1, 'default budget is one browser')
live = [0, 0]


@S._uses_browser
def fake_launch():
    with LOCK:
        live[0] += 1
        live[1] = max(live[1], live[0])
    time.sleep(0.05)
    with LOCK:
        live[0] -= 1


threads = [threading.Thread(target=fake_launch) for _ in range(5)]
for th in threads:
    th.start()
for th in threads:
    th.join(30)
t.eq(live[1], 1, 'five concurrent launch attempts never overlap')


@S._uses_browser
def outer():
    return inner()


@S._uses_browser
def inner():
    return 'ok'


t.eq(outer(), 'ok', 'a launcher that calls another on the same thread does not deadlock')

held, release = threading.Event(), threading.Event()


def holder():
    with S._browser_slot():
        held.set()
        release.wait(5)


h = threading.Thread(target=holder)
h.start()
held.wait(5)
with S._browser_slot(blocking=False) as got:
    busy = got
release.set()
h.join(5)
t.eq(busy, False, 'non-blocking acquire reports BUSY instead of waiting (the UA probe relies on it)')
with S._browser_slot(blocking=False) as got:
    free = got
t.eq(free, True, 'and succeeds once the slot is free again')

# The Facebook lean session holds a slot while open; close() must give it back.
sess = S._FbLeanSession()
sess._slot = S._browser_slot()
sess._slot.__enter__()
sess.close()
with S._browser_slot(blocking=False) as got:
    t.eq(got, True, "_FbLeanSession.close() releases its browser slot")

t.section('4. Continuous dispatch: most-overdue first, never twice, slots reused')
S._IN_FLIGHT.clear()
due = [{'user_id': 'a', '_due_since': 300.0},
       {'user_id': 'b', '_due_since': 100.0},
       {'user_id': 'c', '_due_since': 200.0}]
pick = S._pick_to_start(due, 2)
t.eq([c['user_id'] for c in pick], ['b', 'c'], 'picks the two MOST overdue, not list order')
gate = threading.Event()
S._start_scrapes(pick, lambda cfg: (gate.wait(5), 7)[1], 2)
t.eq(S._in_flight_count(), 2, 'both are in flight')
t.eq(S._pick_to_start(due, 2), [], 'no free slot: nobody else starts, in-flight users are not re-picked')
t.eq([c['user_id'] for c in S._pick_to_start(due, 3)], ['a'],
     'one more slot: only the user not already in flight')
gate.set()
deadline = time.time() + 5
while time.time() < deadline and any(not f.done() for f in list(S._IN_FLIGHT.values())):
    time.sleep(0.02)
t.eq(S._reap_finished_scrapes(), 14, "reaping returns the finished scrapes' new-listing counts")
t.eq(S._in_flight_count(), 0, 'and frees their slots for the same cycle')

t.section('5. Mercari cycle cache: fetch only missing terms, merge, never replace')
# With continuous dispatch the scheduler prefetches every 15s for whoever it is
# starting. Replacing the cache would refetch every 15s and evict terms that a
# scrape already in flight has not reached yet.
calls = []
orig_api = S._mercari_fetch_via_api
S._mercari_fetch_via_api = lambda terms, n: (calls.append(sorted(terms))
                                             or ({x: [{'link': x}] for x in terms}, None))
os.environ.pop('MERCARI_SHARED_TTL_SEC', None)
S._MERCARI_CYCLE.clear()
try:
    t.eq(S.mercari_prefetch_cycle(['a', 'b']), (2, None), 'first call fetches both terms')
    t.eq(S.mercari_prefetch_cycle(['b', 'c']), (1, None), 'second call fetches only the new term')
    t.eq(calls[-1], ['c'], '...and asks the API for exactly that term')
    t.ok(S._mercari_cycle_items('a') is not None, 'earlier terms are kept (merged, not replaced)')
    t.eq(S.mercari_prefetch_cycle(['a', 'b', 'c']), (0, None), 'all fresh: no API call at all')
    t.eq(len(calls), 2, 'still only two API calls in total')
    # Back-date the entry past the (default 240s) TTL. Shrinking the TTL to 0
    # instead is flaky on Windows, where time.time() ticks in ~15ms steps and an
    # entry stored "just now" can read as exactly 0s old.
    S._MERCARI_CYCLE['a'] = (time.time() - 1000.0, S._MERCARI_CYCLE['a'][1])
    t.ok(S._mercari_cycle_items('a') is None, 'past the TTL an entry is not served')
    t.eq(S.mercari_prefetch_cycle(['a']), (1, None), 'and is fetched again')
finally:
    S._mercari_fetch_via_api = orig_api
    os.environ.pop('MERCARI_SHARED_TTL_SEC', None)
    S._MERCARI_CYCLE.clear()

t.section('6. Memory cap reports its inputs at boot')
line = S.memory_detection_report()
t.ok('browsers <= 1' in line and 'browser-free peak' in line,
     'boot line names the browser budget and the per-scrape estimate')
t.done()
