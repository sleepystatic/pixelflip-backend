# -*- coding: utf-8 -*-
"""
Shared search cache: keys, TTL, failure handling, isolation, bounds.

NOT YET WIRED INTO ANY PLATFORM. Mercari already has its own equivalent
(mercari_prefetch_cycle); the other three are candidates. These tests exist so
the mechanism is proven before it goes anywhere near a scrape path.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import search_cache as C

t = Suite('search-cache')

os.environ['SEARCH_CACHE_ENABLED'] = '1'
os.environ['SEARCH_CACHE_TTL_SEC'] = '120'
os.environ['SEARCH_CACHE_MAX_ENTRIES'] = '256'
C.reset(stats_too=True)

t.section('1. Keys reflect what each platform search actually varies on')
# Mercari's /v1/api takes no location, so two users anywhere share a result.
t.eq(C.key_for('mercari', 'ds lite', region='sac', lat=38.0, lon=-121.2),
     C.key_for('mercari', 'ds lite'),
     'mercari: location is ignored entirely')
t.ok(C.key_for('mercari', 'ds lite') != C.key_for('mercari', '3ds xl'),
     'mercari: different terms do not collide')

# Facebook: the region slug in the path is the ONLY location control.
t.eq(C.key_for('facebook', 'ds lite', region='sac'),
     C.key_for('facebook', 'DS Lite', region='SAC'),
     'facebook: term and region are case-insensitive')
t.ok(C.key_for('facebook', 'ds lite', region='sac')
     != C.key_for('facebook', 'ds lite', region='la'),
     'facebook: different regions do NOT share')

# OfferUp: coordinates rounded, because a few streets cannot change a feed.
t.eq(C.key_for('offerup', 'ds lite', lat=38.0021, lon=-121.2604),
     C.key_for('offerup', 'ds lite', lat=38.0044, lon=-121.2611),
     'offerup: nearby coordinates share a key')
t.ok(C.key_for('offerup', 'ds lite', lat=38.0, lon=-121.2)
     != C.key_for('offerup', 'ds lite', lat=40.7, lon=-74.0),
     'offerup: distant coordinates do NOT share')

# Craigslist is scoped by postal + distance, so it is the least shareable.
t.ok(C.key_for('craigslist', 'ds lite', site='stockton', postal='95212', radius=25)
     != C.key_for('craigslist', 'ds lite', site='stockton', postal='95212', radius=100),
     'craigslist: radius is part of the key')
t.ok(C.key_for('craigslist', 'ds lite', site='stockton', postal='95212', radius=25)
     != C.key_for('craigslist', 'ds lite', site='stockton', postal='95370', radius=25),
     'craigslist: postal is part of the key')

t.section('2. Store and serve')
k = C.key_for('mercari', 'ds lite')
t.eq(C.get(k), None, 'cold key -> miss')
rows = [{'title': 'A', 'price': 1.0}, {'title': 'B', 'price': 2.0}]
t.eq(C.put(k, rows), True, 'non-empty result stored')
got = C.get(k)
t.eq(len(got or []), 2, 'hit returns the rows')
t.eq([r['title'] for r in got], ['A', 'B'], 'rows come back intact')

# A consumer mutating its rows must not corrupt the next user's copy: the scrape
# loops annotate rows as they iterate.
got[0]['title'] = 'MUTATED'
again = C.get(k)
t.eq(again[0]['title'], 'A', 'each caller gets an independent copy')
rows[1]['title'] = 'ALSO MUTATED'
t.eq(C.get(k)[1]['title'], 'B', 'the stored snapshot is decoupled from the caller too')

t.section('3. A failure must never be cached')
# An empty result is what a block looks like. Caching one would turn a single
# user's bad proxy draw into a platform-wide outage for everyone on that key.
k2 = C.key_for('facebook', 'blocked term', region='sac')
t.eq(C.put(k2, []), False, 'empty list rejected')
t.eq(C.put(k2, None), False, 'None rejected')
t.eq(C.get(k2), None, 'nothing was stored')
before = C.stats()['rejected_empty']
C.put(k2, [])
t.eq(C.stats()['rejected_empty'], before + 1, 'rejections are counted')

t.section('4. TTL')
os.environ['SEARCH_CACHE_TTL_SEC'] = '0.3'
k3 = C.key_for('mercari', 'ttl probe')
C.put(k3, rows)
t.ok(C.get(k3) is not None, 'fresh entry serves')
time.sleep(0.45)
t.eq(C.get(k3), None, 'expired entry does not serve')
os.environ['SEARCH_CACHE_TTL_SEC'] = '0'
C.put(k3, rows)
t.eq(C.get(k3), None, 'TTL=0 disables the cache entirely')
os.environ['SEARCH_CACHE_TTL_SEC'] = '120'

t.section('5. Off by default')
os.environ.pop('SEARCH_CACHE_ENABLED', None)
t.eq(C.enabled(), False, 'absent env -> disabled')
C.reset()
t.eq(C.put(k, rows), False, 'disabled -> put is a no-op')
t.eq(C.get(k), None, 'disabled -> get always misses')
for val, want in (('1', True), ('true', True), ('on', True), ('yes', True),
                  ('0', False), ('false', False), ('', False), ('bogus', False)):
    os.environ['SEARCH_CACHE_ENABLED'] = val
    t.eq(C.enabled(), want, 'SEARCH_CACHE_ENABLED=%r -> %s' % (val, want))
os.environ['SEARCH_CACHE_ENABLED'] = '1'

t.section('6. Bounded, and thread-safe')
os.environ['SEARCH_CACHE_MAX_ENTRIES'] = '10'
C.reset(stats_too=True)
for i in range(25):
    C.put(C.key_for('mercari', 'term-%d' % i), [{'title': str(i)}])
t.ok(C.stats()['entries'] <= 10, 'entry count stays within the cap')
t.ok(C.stats()['evictions'] >= 15, 'older entries were evicted')
os.environ['SEARCH_CACHE_MAX_ENTRIES'] = '256'

C.reset(stats_too=True)
errors = []


def hammer(n):
    try:
        for i in range(200):
            kk = C.key_for('mercari', 'shared-%d' % (i % 7))
            if C.get(kk) is None:
                C.put(kk, [{'title': 't%d' % i}])
    except Exception as e:                        # pragma: no cover
        errors.append(e)


threads = [threading.Thread(target=hammer, args=(i,)) for i in range(6)]
for th in threads:
    th.start()
for th in threads:
    th.join(30)
t.eq(errors, [], '6 threads hammering it raised nothing')
t.ok(C.stats()['entries'] <= 7, 'concurrent writers did not duplicate keys')

t.section('7. Hit-rate reporting (how the real benefit gets measured)')
C.reset(stats_too=True)
kk = C.key_for('mercari', 'measured')
C.get(kk)                      # miss
C.put(kk, rows)
C.get(kk)                      # hit
C.get(kk)                      # hit
s = C.stats()
t.eq(s['hits'], 2, 'hits counted')
t.eq(s['misses'], 1, 'misses counted')
t.ok(abs(s['hit_rate'] - (2 / 3.0)) < 1e-9, 'hit_rate computed')
t.ok('search cache:' in C.format_stats(), 'format_stats renders a log line')
print("  %s" % C.format_stats())

C.reset(stats_too=True)
os.environ.pop('SEARCH_CACHE_ENABLED', None)
t.done()
