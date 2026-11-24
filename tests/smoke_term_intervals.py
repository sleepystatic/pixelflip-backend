# -*- coding: utf-8 -*-
"""
Per-term scan intervals, SCRAPER side (migration 012), rebuilt 2026-11-20
(original: 19 assertions, lost).

term_interval_minutes() is the clamp the scheduler applies on every cycle: the
user's slower choice always wins, but nothing scans faster than its plan
allows — a forged 5 on Basic still scans at 10. Also pins that the scraper's
tables match app.py's and the dashboard's, and that /api/status's countdown
(_term_due_map) clamps exactly the same way.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import REPO, Suite, bootstrap

bootstrap()
import scraper_multi_user as S
import app as A

t = Suite('term-intervals')
tim = S.term_interval_minutes
PRO, BASIC = {'plan_tier': 'pro'}, {'plan_tier': 'basic'}

t.section('1. Floors per tier')
t.eq((S.interval_floor_for_tier('pro'), S.interval_floor_for_tier('basic'),
      S.interval_floor_for_tier('inactive'), S.interval_floor_for_tier(None)), (5, 10, 10, 10),
     'fastest rate: Pro 5, Basic 10, anything else 10')
t.eq((S.standard_floor_for_tier('pro'), S.standard_floor_for_tier('basic')), (10, 10),
     'non-priority floor is 10 everywhere (Pro used to be a slower 15)')
t.eq((S.max_priority_for_tier('pro'), S.max_priority_for_tier('basic')), (3, 0), 'priority slots: Pro 3, Basic 0')

t.section('2. The clamp')
t.eq(tim({'interval_minutes': 5}, PRO), 5, 'Pro term at 5 -> 5')
t.eq(tim({'interval_minutes': 5}, BASIC), 10, 'forged 5 on Basic -> scans at 10')
t.eq(tim({'interval_minutes': 30}, PRO), 30, "the user's slower choice wins (30 stays 30)")
t.eq(tim({'interval_minutes': 60}, BASIC), 60, 'Basic at 60 -> 60')
t.eq(tim({}, PRO), 10, 'no interval stored -> default 10')
t.eq(tim({'interval_minutes': 'fast'}, PRO), 10, 'junk interval -> default 10')
t.eq(tim({'interval_minutes': 1}, PRO), 5, 'a forged 1 on Pro -> the 5-minute floor')
t.eq(tim({'interval_minutes': 5}, {'plan_tier': None, 'is_pro': True}), 10,
     'legacy is_pro without plan_tier resolves to Basic -> 10')
t.eq(tim({'interval_minutes': 5}, {}), 10, 'no plan at all -> 10')

t.section('3. One table, three copies, all equal')
for name in ('PLAN_INTERVAL_FLOOR_MINUTES', 'PLAN_INTERVAL_OPTIONS', 'PLAN_MAX_PRIORITY_TERMS',
             'PLAN_STANDARD_FLOOR_MINUTES', 'DEFAULT_TERM_INTERVAL_MINUTES'):
    t.eq(getattr(A, name), getattr(S, name), 'app.py and scraper agree on %s' % name)
js = open(os.path.join(REPO, 'frontend', 'src', 'termIntervals.js'), encoding='utf-8').read()
opts = {k: [int(x) for x in v.split(',')] for k, v in re.findall(r'(pro|basic):\s*\[([\d,\s]+)\]', js)}
t.eq(opts, S.PLAN_INTERVAL_OPTIONS, "the dashboard's fallback option table matches")
t.ok(re.search(r'DEFAULT_TERM_INTERVAL\s*=\s*10\b', js) and re.search(r'FASTEST_TIER_INTERVAL\s*=\s*5\b', js),
     "the dashboard's default (10) and fastest (5) match")

t.section('4. The countdown clamps like the scheduler')


class Cur(object):
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, args=None):
        pass

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class Conn(object):
    def __init__(self, rows):
        self.rows = rows

    def cursor(self, cursor_factory=None):
        return Cur(self.rows)


rows = [{'search_term': 'fast', 'interval_minutes': 5, 'last_ts': 1000.0},
        {'search_term': 'never', 'interval_minutes': 30, 'last_ts': None},
        {'search_term': 'blank', 'interval_minutes': None, 'last_ts': 2000.0}]
due = A._term_due_map(Conn(rows), 'u', 'basic')
t.eq(due['fast'], 1000.0 + 10 * 60, 'Basic countdown: a forged 5 counts down 10 minutes, like the scheduler')
t.eq(due['never'], 30 * 60, 'never scanned -> due one interval after epoch 0 (i.e. now)')
t.eq(due['blank'], 2000.0 + 10 * 60, 'no stored interval -> default 10')
t.eq(A._term_due_map(Conn(rows), 'u', 'pro')['fast'], 1000.0 + 5 * 60, 'Pro countdown honours 5')

t.done()
