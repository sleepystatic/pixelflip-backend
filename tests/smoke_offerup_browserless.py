# -*- coding: utf-8 -*-
"""
Browser-free OfferUp (2026-10-19) and its warm-session reuse (2026-11-02).

Replays the committed live GetModularFeed capture (fixtures/offerup_search_
graphql.json, 49 listing tiles) through the REAL fetch path with a fake curl_cffi
Session, so it needs neither OfferUp nor a browser. What it pins down:

  * the GraphQL entry point uses curl_cffi and never touches the browser while
    that works — OfferUp was the last per-term Chromium launch;
  * the __cf_bm warm-up GET is paid once per thread, not once per term;
  * a non-200 drops the session so the next call re-warms (a stale or flagged
    cookie is the likeliest cause), while an empty 200 is a real empty result.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap, FIXTURES

bootstrap()
import scraper_multi_user as S
import curl_cffi.requests as cffi_requests

t = Suite('offerup-browserless')
path = os.path.join(FIXTURES, 'offerup_search_graphql.json')
if not os.path.exists(path):
    t.skip('fixtures/offerup_search_graphql.json missing')
CAPTURE = open(path, 'rb').read()
EMPTY = json.dumps({'data': {'modularFeed': {'looseTiles': []}}}).encode()

COUNT = {'get': 0, 'post': 0, 'sessions': 0}
SCRIPT = {'status': 200, 'body': CAPTURE}


class FakeResp(object):
    def __init__(self, status, content):
        self.status_code = status
        self.content = content
        self.text = content.decode('utf-8', 'replace')


class FakeSession(object):
    def __init__(self, *a, **k):
        COUNT['sessions'] += 1
        self.warm = False

    def get(self, url, **k):
        COUNT['get'] += 1
        self.warm = True
        return FakeResp(200, b'User-agent: *\n')

    def post(self, url, **k):
        assert self.warm, 'POST without the warm-up cookie'
        COUNT['post'] += 1
        return FakeResp(SCRIPT['status'], SCRIPT['body'])

    def close(self):
        pass


cffi_requests.Session = FakeSession
S._offerup_drop_curl_session()
os.environ.pop('OFFERUP_SESSION_MAX_AGE_SEC', None)

t.section('1. Parses the live capture with no browser')
items, err = S._offerup_fetch_via_curl('nintendo ds lite', 38.03, -121.26, force_no_proxy=True)
t.ok(err is None, 'a 200 is not an error (got %r)' % (err,))
t.ok(len(items or []) >= 40, 'listing tiles parsed (got %d)' % len(items or []))
t.ok(all(i['link'].startswith('https://offerup.com/item/detail/') for i in items),
     'every row links to /item/detail/')
t.ok(all(i['title'] and i['price'] for i in items), 'every row has a title and a price')

t.section('2. The warm-up is paid once per thread, not per term')
for term in ('ds lite', '3ds', 'gameboy'):
    S._offerup_fetch_via_curl(term, 38.03, -121.26, force_no_proxy=True)
t.eq(COUNT['get'], 1, 'four terms -> ONE warm-up GET (was one per term)')
t.eq(COUNT['post'], 4, '...and four GraphQL POSTs')
os.environ['OFFERUP_SESSION_MAX_AGE_SEC'] = '0'
S._offerup_fetch_via_curl('x', 38.03, -121.26, force_no_proxy=True)
t.eq(COUNT['get'], 2, 'an aged-out session re-warms')
os.environ.pop('OFFERUP_SESSION_MAX_AGE_SEC', None)

t.section('3. Failures: drop the session; an empty 200 is a real empty result')
SCRIPT.update(status=403, body=b'blocked')
_, err = S._offerup_fetch_via_curl('x', 38.03, -121.26, force_no_proxy=True)
t.eq(err, 'HTTP 403', 'a 403 surfaces as an error')
SCRIPT.update(status=200, body=CAPTURE)
gets = COUNT['get']
S._offerup_fetch_via_curl('x', 38.03, -121.26, force_no_proxy=True)
t.eq(COUNT['get'], gets + 1, 'the call after a 403 warms a fresh session')
SCRIPT.update(status=200, body=EMPTY)
items, err = S._offerup_fetch_via_curl('x', 38.03, -121.26, force_no_proxy=True)
t.ok(err is None and items == [], 'an empty 200 is success with zero rows, not an error')
SCRIPT.update(status=200, body=CAPTURE)

t.section('4. The GraphQL entry point prefers curl and keeps the browser as fallback')
browser_calls = []
orig_browser = S._offerup_fetch_via_graphql_browser
S._offerup_fetch_via_graphql_browser = lambda *a, **k: (browser_calls.append(1) or ([], 'browser'))
try:
    os.environ['OFFERUP_BROWSERLESS'] = '1'
    items, err = S._offerup_fetch_via_graphql('x', 38.03, -121.26, force_no_proxy=True)
    t.ok(err is None and len(items) >= 40 and not browser_calls,
         'browser-free path used; the browser is never launched')
    SCRIPT.update(status=403, body=b'blocked')
    S._offerup_fetch_via_graphql('x', 38.03, -121.26, force_no_proxy=True)
    t.eq(len(browser_calls), 1, 'when curl fails, the browser fallback runs (once)')
    SCRIPT.update(status=200, body=CAPTURE)
    os.environ['OFFERUP_BROWSERLESS'] = '0'
    S._offerup_fetch_via_graphql('x', 38.03, -121.26, force_no_proxy=True)
    t.eq(len(browser_calls), 2, 'OFFERUP_BROWSERLESS=0 forces the browser path')
finally:
    S._offerup_fetch_via_graphql_browser = orig_browser
    os.environ.pop('OFFERUP_BROWSERLESS', None)
t.ok(getattr(orig_browser, '__wrapped__', None) is not None,
     'the browser fallback is wrapped by the process-wide browser budget')

t.done()
