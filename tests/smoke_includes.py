# -*- coding: utf-8 -*-
"""
Include / exclude keyword semantics (migration 014), rebuilt 2026-11-20.

The original suite (38 assertions) lived in a wiped scratchpad. This pins the
same rules against is_excluded / _include_matches in scraper_multi_user:

  * includes are WORD-BOUNDARY matches ('blue' is not "Bluetooth", 'red' is not
    "hundred", 'size 6' is not "size 65" but is "size 6.5");
  * includes are OR'd; empty includes = no filter (never "match nothing");
  * exclusions are substrings and OUTRANK includes;
  * keywords belong to ONE search term;
  * title and description are SEPARATE haystacks (no "blue" + "tooth" seam);
  * check_includes=False is the title-only first pass: an exclusion is final,
    an include miss is not.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import scraper_multi_user as S
import app as A

t = Suite('includes')
m = S._include_matches


def terms(**cfg):
    return {'iphone xr': dict(cfg)}


def excluded(title, price=100, desc=None, check_includes=True, **cfg):
    return S.is_excluded(title, price, [], search_terms=terms(**cfg), matched_term='iphone xr',
                         description=desc, check_includes=check_includes)


t.section('1. Word boundaries')
t.ok(m('blue iphone xr', 'blue'), "'blue' matches a whole word")
t.ok(not m('iphone xr bluetooth case', 'blue'), "'blue' does NOT match Bluetooth")
t.ok(not m('one hundred dollars', 'red'), "'red' does NOT match hundred")
t.ok(m('red iphone', 'red'), "'red' still matches red")
t.ok(not m('size 65 shoes', 'size 6'), "'size 6' does NOT match size 65")
t.ok(m('nike size 6.5', 'size 6'), "'size 6' DOES match size 6.5 ('.' is not a word char)")
t.ok(m('nike size 6.5', '6.5'), 'a keyword with punctuation matches')
t.ok(m('ps5 (disc) edition', 'disc'), 'brackets are boundaries')
t.ok(m('blue', 'BLUE'.lower()) and m('BLUE IPHONE'.lower(), 'Blue'), 'case-insensitive')
t.ok(not m('anything', '   '), 'a blank keyword matches nothing')
t.ok(m('c++ book', 'c++'), 'regex metacharacters are escaped, not interpreted')

t.section('2. Includes: OR, and empty means no filter')
t.ok(not excluded('iPhone XR blue 64GB', includes=['blue', 'red']), 'matches one of two includes -> kept')
t.ok(not excluded('iPhone XR red', includes=['blue', 'red']), 'matches the other -> kept')
t.ok(excluded('iPhone XR black', includes=['blue', 'red']), 'matches neither -> dropped')
t.ok(not excluded('iPhone XR black', includes=[]), 'EMPTY includes = no filter (kept)')
t.ok(not excluded('iPhone XR black'), 'no includes key at all = no filter')
t.ok(not excluded('iPhone XR black', includes=['', '  ']), 'blank include entries are ignored')
t.ok(excluded('iPhone XR bluetooth speaker', includes=['blue']), "'blue' include does not admit Bluetooth")

t.section('3. Exclusions: substrings, and they outrank includes')
t.ok(excluded('iPhone XR blue BROKEN screen', includes=['blue'], exclusions=['broken']),
     'excluded word rejects even when an include matched')
t.ok(excluded('iPhone XR showcase box', exclusions=['case']), "exclusions are substrings ('case' in showcase)")
t.ok(not excluded('iPhone XR', exclusions=['']), 'a blank exclusion excludes nothing')
t.ok(S.is_excluded('Cracked phone', 50, ['cracked']), 'the retired global list is still honoured')

t.section('4. Keywords belong to one search term')
cfgs = {'ps5': {'exclusions': ['case']}, 'phone case': {'exclusions': []}}
t.ok(S.is_excluded('PS5 with case', 400, [], search_terms=cfgs, matched_term='ps5'),
     "'case' excluded for the console term")
t.ok(not S.is_excluded('Phone case blue', 10, [], search_terms=cfgs, matched_term='phone case'),
     '...but not for the phone-case term')
t.ok(not S.is_excluded('anything', 10, [], search_terms=cfgs, matched_term='unknown term'),
     'an unknown term applies no keywords')
t.ok(not S.is_excluded('anything', 10, [], search_terms=None, matched_term='ps5'),
     'no term config at all applies no keywords')

t.section('5. Title and description are separate haystacks')
t.ok(not excluded('iPhone XR', desc='Comes in BLUE, 64GB', includes=['blue']),
     'include found only in the description -> kept')
t.ok(excluded('iPhone XR', desc='Screen is broken', exclusions=['broken']),
     'exclusion found only in the description -> dropped')
t.ok(excluded('Speaker blue', desc='tooth edition', includes=['bluetooth']),
     "'blue' + 'tooth' across the seam is NOT 'bluetooth'")
t.ok(excluded('iPhone XR', desc=None, includes=['blue']), 'no description -> title only')

t.section('6. Two-pass flow: check_includes=False')
t.ok(not excluded('iPhone XR black', check_includes=False, includes=['blue']),
     'pass 1: an include MISS on the title is not final (the body may have it)')
t.ok(excluded('iPhone XR cracked', check_includes=False, includes=['blue'], exclusions=['cracked']),
     'pass 1: an exclusion hit on the title IS final')

t.section('7. Price floor and the include cap')
os.environ['MIN_LISTING_PRICE'] = '20'
t.ok(S.is_excluded('cheap thing', 5, []), 'MIN_LISTING_PRICE drops listings below it')
t.ok(not S.is_excluded('fine thing', 25, []), '...and keeps those at or above it')
os.environ['MIN_LISTING_PRICE'] = '0'
t.ok(not S.is_excluded('free thing', 0, []), 'default 0 = no floor ("free" posts kept)')
t.eq(A.MAX_TERM_INCLUDES, 20, 'include keywords are capped per term (MAX_TERM_INCLUDES default 20)')

t.done()
