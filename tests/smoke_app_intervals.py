# -*- coding: utf-8 -*-
"""
Per-term intervals, API side, rebuilt 2026-11-20 (original: 25 assertions, lost).

What a settings save may store. The UI cannot be trusted, so app.py enforces
both halves server-side: which rates a tier may use at all
(_sanitize_term_interval) and how many terms may sit at the fastest one
(_resolve_term_intervals, the 3-term cap). Off-menu values fall back to the
default instead of erroring, so a stale or hostile client can never lock a user
out of saving. Terms ALREADY at 5 outrank newly-raised ones (the tiebreak), so
picking a fourth never silently demotes a term chosen earlier.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import app as A

t = Suite('app-intervals')
san = A._sanitize_term_interval
res = A._resolve_term_intervals

t.section('1. The menu per tier')
t.eq(A._term_interval_options_for_tier('pro'), [5, 10, 15, 30, 60], 'Pro menu')
t.eq(A._term_interval_options_for_tier('basic'), [10, 15, 30, 60], 'Basic menu (no 5, no 45)')
t.eq(A._term_interval_options_for_tier('inactive'), [10, 15, 30, 60], 'anything else gets the Basic menu')
t.eq(A._term_interval_options_for_tier(None), [10, 15, 30, 60], 'no tier -> Basic menu')

t.section('2. One value at a time')
t.eq(san(15, 'basic'), 15, 'on-menu value kept')
t.eq(san('30', 'pro'), 30, 'numeric strings accepted')
t.eq(san(5, 'basic'), 10, 'a forged 5 on Basic -> default 10, not an error')
t.eq(san(45, 'pro'), 10, 'off-menu 45 -> default')
t.eq(san(None, 'pro'), 10, 'missing -> default')
t.eq(san('soon', 'pro'), 10, 'junk -> default')

t.section('3. The 3-term cap')
out, demoted = res({'a': {'interval': 5}, 'b': {'interval': 5}, 'c': {'interval': 5}}, 'pro', 3)
t.ok(out == {'a': 5, 'b': 5, 'c': 5} and demoted == 0, 'three at 5 on Pro: all kept')
four = {'a': {'interval': 5}, 'b': {'interval': 5}, 'c': {'interval': 5}, 'd': {'interval': 5}}
out, demoted = res(four, 'pro', 3)
t.ok(demoted == 1 and sum(v == 5 for v in out.values()) == 3, 'four at 5: one demoted, three kept')
t.eq(out['d'], 10, '...the demoted one goes to the default 10, not an error')
out, demoted = res({'a': {'interval': 5}, 'b': {'interval': 30}}, 'pro', 3)
t.ok(out == {'a': 5, 'b': 30} and demoted == 0, 'slower terms never count against the cap')
out, demoted = res({'a': {'interval': 5}, 'b': {'interval': 5}}, 'basic', 0)
t.ok(out == {'a': 10, 'b': 10} and demoted == 0, 'Basic: 5 is not on the menu, so nothing to count')

t.section('4. Tiebreak: terms already at 5 outrank newly-raised ones')
prev = {'zebra': 5, 'a': 10, 'b': 10, 'c': 10}
req = {'zebra': {'interval': 5}, 'a': {'interval': 5}, 'b': {'interval': 5}, 'c': {'interval': 5}}
out, demoted = res(req, 'pro', 3, prev_interval=prev)
t.eq(out['zebra'], 5, "'zebra' was already at 5 and keeps it (alphabetical order would have dropped it)")
t.ok(demoted == 1 and sum(v == 5 for v in out.values()) == 3, '...one of the NEW ones is demoted instead')
t.eq(out['c'], 10, '...the last of the new ones, deterministically')

t.section('5. Old clients and odd input')
out, _ = res({'a': {'priority': True}, 'b': {'priority': False}}, 'pro', 3)
t.eq(out, {'a': 5, 'b': 10}, 'old boolean `priority` still maps on Pro (half-deployed frontend)')
out, _ = res({'a': {'priority': True}}, 'basic', 0)
t.eq(out, {'a': 10}, '...and is ignored on Basic')
out, _ = res({'a': None}, 'pro', 3)
t.eq(out, {'a': 10}, 'a term with no settings gets the default')
t.eq(res({}, 'pro', 3), ({}, 0), 'no terms -> nothing to do')

t.done()
