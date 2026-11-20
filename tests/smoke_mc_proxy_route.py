# -*- coding: utf-8 -*-
"""
Mercari item-page proxy routing: decided ONCE per scrape, probe response reused,
explicit modes honoured. Fully offline — the HTTP layer is faked.

WHY THIS MATTERS
Item pages are ~232KB each. Measured 2026-09-28 from a residential connection,
the same page came back identically direct and proxied, so routing them through
the metered residential proxy is pure waste there. But production runs on
Render, whose datacenter egress Cloudflare treats far more harshly, so it cannot
be hardcoded either way — hence probe once, remember, and cap the cost of being
wrong at a single ~6KB challenge page per scrape.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import scraper_multi_user as S
import curl_cffi.requests as cffi_requests

t = Suite('mercari-proxy-route')

GOOD = '<html>' + 'x' * 5000 + '</html>'
CHALLENGE = '<html><title>Just a moment...</title></html>'
CALLS = []           # (url, went_through_proxy)


def make_session(direct_ok):
    class FakeResp(object):
        def __init__(self, text):
            self.text = text
            self.status_code = 200
            self.content = text.encode()

    class FakeSession(object):
        @property
        def cookies(self):
            class C(dict):
                def update(self, *a, **k):
                    pass
            return C()

        def get(self, url, headers=None, proxies=None, **kw):
            CALLS.append((url, proxies is not None))
            if proxies is None and not direct_ok:
                return FakeResp(CHALLENGE)
            return FakeResp(GOOD)

        def close(self):
            pass

    return FakeSession


CTX = {'headers': {'authorization': 'Bearer x'}, 'cookies': {},
       'variables': {'a': 1}, 'ext': 'e'}


def run(mode, direct_ok, n=3, proxy_configured=True):
    del CALLS[:]
    if mode is None:
        os.environ.pop('MERCARI_ITEM_PROXY', None)
    else:
        os.environ['MERCARI_ITEM_PROXY'] = mode
    cffi_requests.Session = make_session(direct_ok)
    orig = S._get_proxy
    S._get_proxy = ((lambda p: 'http://u:p@proxy.example:1234') if proxy_configured
                    else (lambda p: None))
    try:
        sess = S._MercariItemSession(CTX)
        out = [sess.fetch('https://www.mercari.com/us/item/m%d/' % i) for i in range(n)]
        sess.close()
        return out
    finally:
        S._get_proxy = orig


t.section('auto mode, direct WORKS (a residential host)')
out = run(None, direct_ok=True, n=3)
t.eq(len([o for o in out if o]), 3, 'all 3 pages returned content')
t.eq(len(CALLS), 3, '3 pages cost 3 requests — the probe response was REUSED')
t.eq([p for _, p in CALLS], [False, False, False], 'every request went direct')

t.section('auto mode, direct CHALLENGED (a datacenter host like Render)')
out = run(None, direct_ok=False, n=3)
t.eq(len([o for o in out if o]), 3, 'all 3 pages still returned content')
t.eq(len(CALLS), 4, '3 pages cost 4 requests — ONE wasted probe for the scrape')
t.eq([p for _, p in CALLS], [False, True, True, True],
     'probe direct once, then proxied for the rest')

t.section('explicit modes')
run('always', direct_ok=True, n=2)
t.eq([p for _, p in CALLS], [True, True], 'always -> never tries direct')
run('never', direct_ok=False, n=2)
t.eq([p for _, p in CALLS], [False, False], 'never -> never uses the proxy')

t.section('mode parsing')
sess = S._MercariItemSession(CTX)
for val, want in (('always', 'always'), ('never', 'never'), ('bogus', 'auto'), ('', 'auto')):
    os.environ['MERCARI_ITEM_PROXY'] = val
    t.eq(sess._proxy_mode(), want, 'MERCARI_ITEM_PROXY=%r -> %s' % (val, want))
os.environ.pop('MERCARI_ITEM_PROXY', None)
t.eq(sess._proxy_mode(), 'auto', 'unset -> auto')

t.section('no proxy configured at all')
run(None, direct_ok=True, n=2, proxy_configured=False)
t.eq([p for _, p in CALLS], [False, False], 'direct, and no probe waste')
t.eq(len(CALLS), 2, '2 pages cost 2 requests')

t.done()
