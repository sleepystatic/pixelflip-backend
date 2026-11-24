# -*- coding: utf-8 -*-
"""
Scan coalescing, rebuilt 2026-11-20 (original: 22 assertions, lost).

Once any term is due, terms coming due within coalesce_window_sec() ride along
in the same scrape, so staggered terms do not each pay a full run. Pins the
window config, the sweep, its bound (never more than the window early), the
countdown it reports, and that /api/status uses the SAME window.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import scraper_multi_user as S
import app as A

t = Suite('coalesce')
NOW = 1_800_000_000.0
PRO = {'plan_tier': 'pro'}
BASIC = {'plan_tier': 'basic'}


def win(v):
    if v is None:
        os.environ.pop('SCRAPE_COALESCE_WINDOW_SEC', None)
    else:
        os.environ['SCRAPE_COALESCE_WINDOW_SEC'] = v
    return S.coalesce_window_sec()


def term(minutes, scraped_ago_sec=None):
    """A term at `minutes` cadence, last scanned `scraped_ago_sec` ago (None = never)."""
    return {'interval_minutes': minutes,
            'last_scraped_ts': None if scraped_ago_sec is None else NOW - scraped_ago_sec}


t.section('1. Window configuration')
t.eq(win(None), 120, 'default 120s (about one scrape)')
t.eq(win('300'), 300, 'configurable')
t.eq(win('0'), 0, '0 turns coalescing off')
t.eq(win('-50'), 0, 'negative clamps to 0')
t.eq(win('soon'), 120, 'garbage falls back to the default')
win(None)

t.section('2. Who is due')
t.eq(S.due_terms_for_user({}, PRO, now=NOW), ({}, None), 'no terms -> nothing due, no countdown')
due, nxt = S.due_terms_for_user({'a': term(10, 300)}, PRO, now=NOW)
t.ok(due == {} and nxt == NOW + 300, 'nothing due -> countdown to the soonest term')
due, _ = S.due_terms_for_user({'a': term(10)}, PRO, now=NOW)
t.eq(list(due), ['a'], 'a never-scanned term is due at once')

t.section('3. The sweep')
terms = {'due': term(10, 600), 'soon': term(10, 600 - 100), 'later': term(10, 600 - 200)}
due, nxt = S.due_terms_for_user(terms, PRO, now=NOW)
t.eq(sorted(due), ['due', 'soon'], 'a term due within 120s rides along; one due in 200s does not')
t.eq(nxt, NOW + 200, '...and the held term is the countdown')
edge = {'due': term(10, 600), 'edge': term(10, 600 - 120), 'past': term(10, 600 - 121)}
due, _ = S.due_terms_for_user(edge, PRO, now=NOW)
t.ok('edge' in due and 'past' not in due, 'bounded: exactly the window, never a second more')
win('0')
due, _ = S.due_terms_for_user(terms, PRO, now=NOW)
t.eq(list(due), ['due'], 'window 0 -> only terms that are actually due')
win(None)
due, _ = S.due_terms_for_user({'a': term(60, 3600), 'b': term(60, 3600 - 110)}, PRO, now=NOW)
t.eq(sorted(due), ['a', 'b'], 'a 60-minute term is pulled in at most ~2 minutes early')
due, _ = S.due_terms_for_user({'x': term(10, 0), 'y': term(10, 0)}, PRO, now=NOW)
t.eq(due, {}, 'terms scanned just now are not due again')

t.section('4. Cadence floors inside the sweep')
due, nxt = S.due_terms_for_user({'fast': term(5, 400)}, BASIC, now=NOW)
t.ok(due == {} and nxt == NOW + 200, 'a forged 5-minute term on Basic is held to 10 minutes')
due, _ = S.due_terms_for_user({'fast': term(5, 400)}, PRO, now=NOW)
t.eq(list(due), ['fast'], 'on Pro the same term is due at 5 minutes')

t.section('5. One window everywhere')
t.ok(A.coalesce_window_sec is S.coalesce_window_sec,
     '/api/status uses the scheduler\'s own window (the UI cannot promise a different grouping)')

t.done()
