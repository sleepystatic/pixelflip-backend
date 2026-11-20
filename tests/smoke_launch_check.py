# -*- coding: utf-8 -*-
"""
tools/launch_check.py, offline: the parsers and the verdicts it prints.

The checker's job is to say PASS only when a launch step is really done, so
these pin the cases that would make it lie: a second SPF record (permanent SPF
error), a relative logo src that only breaks on NESTED pages, a /health from the
old code, a test Stripe key on Render, an expired Mercari token. No network,
no database.
"""
import base64
import importlib.util
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import REPO, Suite

spec = importlib.util.spec_from_file_location('launch_check', os.path.join(REPO, 'tools', 'launch_check.py'))
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)

t = Suite('launch-check')


class Rep(L.Report):
    def __init__(self):
        L.Report.__init__(self)
        self.rows = []

    def add(self, status, item, message):
        self.rows.append((status, item, message))

    def section(self, title):
        pass

    def status(self, item):
        return [s for s, i, _ in self.rows if i == item]


t.section('1. nslookup TXT parsing (Windows and Linux layouts)')
WIN = ('Server:  router.lan\nAddress:  192.168.1.1\n\nNon-authoritative answer:\n'
       'pixelflip.app\ttext =\n\n\t"v=spf1 include:spf.privateemail.com"\n\t" ~all"\n'
       'pixelflip.app\ttext =\n\n\t"google-site-verification=abc"\n')
t.eq(L.parse_nslookup_txt(WIN), ['v=spf1 include:spf.privateemail.com ~all', 'google-site-verification=abc'],
     'Windows: multi-string record joined, two records kept apart')
LIN = ('Non-authoritative answer:\n_dmarc.pixelflip.app\ttext = "v=DMARC1; p=none; '
       'rua=mailto:x@pixelflip.app"\n\nAuthoritative answers can be found from:\n')
t.eq(L.parse_nslookup_txt(LIN), ['v=DMARC1; p=none; rua=mailto:x@pixelflip.app'], 'Linux layout')
t.eq(L.parse_nslookup_txt("*** UnKnown can't find _dmarc.x: Non-existent domain"), [], 'NXDOMAIN -> no records')

t.section('2. SPF / DMARC verdicts')
t.eq(L.spf_verdict([], 'spf.privateemail.com')[0], 'FAIL', 'no SPF -> FAIL')
t.eq(L.spf_verdict(['v=spf1 include:spf.privateemail.com ~all'], 'spf.privateemail.com')[0], 'PASS', 'one correct SPF -> PASS')
two = ['v=spf1 include:spf.privateemail.com ~all', 'v=spf1 include:other ~all']
st, msg = L.spf_verdict(two, 'spf.privateemail.com')
t.ok(st == 'FAIL' and 'permanent' in msg, 'TWO SPF records -> FAIL (permerror), not PASS')
t.eq(L.spf_verdict(['v=spf1 include:mailgun.org ~all'], 'spf.privateemail.com')[0], 'FAIL', 'SPF without the include -> FAIL')
t.eq(L.dmarc_verdict(['v=DMARC1; p=none; rua=mailto:a@b']), ('PASS', 'policy p=none'), 'DMARC policy read')
t.eq(L.dmarc_verdict([])[0], 'FAIL', 'no DMARC -> FAIL')
t.eq(L.dmarc_verdict(['v=DMARC1; p=none', 'v=DMARC1; p=reject'])[0], 'FAIL', 'two DMARC records -> FAIL')

t.section('3. Token, timing and logo helpers')
payload = base64.urlsafe_b64encode(json.dumps({'exp': 1767225600}).encode()).decode().rstrip('=')
t.eq(L.jwt_exp('Bearer h.' + payload + '.s'), 1767225600.0, 'JWT exp decoded (no signature needed)')
t.eq(L.jwt_exp('not-a-jwt'), None, 'garbage -> None')
t.eq(L.scan_seconds('Scan timing: 75s total — offerup 40s (53%), other 2s (3%)'), 75.0, 'scan duration parsed')
t.eq(L.scan_seconds('Marketplaces scanned: x'), None, 'other lines ignored')
html = ('<header><img src="/logo.gif" alt="PixelFlip"></header>'
        '<footer><img width="44" alt="PixelFlip Logo" src="logo.gif"></footer>')
t.eq(L.find_logo_src(html), 'logo.gif', 'the FOOTER logo is found, not the navbar one')

t.section('4. Landing: a relative logo src fails on a nested page')


class R(object):
    def __init__(self, status, text='', body=None):
        self.status_code, self.text, self._b = status, text, body

    def json(self):
        if self._b is None:
            raise ValueError('not json')
        return self._b


def fake_site(logo_src, pricing):
    def get(url, timeout=45):
        if url.endswith(L.NESTED_PAGE):
            return R(200, '<footer><img alt="PixelFlip Logo" src="%s"></footer>' % logo_src)
        if url.endswith('/logo.gif'):
            return R(200 if url == 'https://pixelflip.app/logo.gif' else 404)
        if url.endswith('/pricing'):
            return R(200, pricing)
        return R(404)
    return get


L.get = fake_site('logo.gif', '<li>Scans every 5 minutes — twice as fast as Basic</li>')
rep = Rep()
L.check_landing(rep, 'https://pixelflip.app')
t.eq(rep.status('footer logo'), ['FAIL'], 'relative src -> /blog/logo.gif 404 -> FAIL')
t.eq(rep.status('pro copy'), ['FAIL'], 'old overstated copy -> FAIL')
L.get = fake_site('/logo.gif', '<li>3 priority terms scanned every 5 minutes</li>')
rep = Rep()
L.check_landing(rep, 'https://pixelflip.app')
t.eq(rep.status('footer logo') + rep.status('pro copy'), ['PASS', 'PASS'], 'fixed footer + honest copy -> PASS')

t.section('5. Deploy verdicts from /health')
OLD = {'status': 'ok', 'checks': {'database': 'ok', 'scraper_last_cycle_age_seconds': 12.0}}
L.get = lambda url, timeout=45: R(200, body=OLD)
rep = Rep()
L.check_deploy(rep, 'https://api')
t.eq(rep.status('new code live'), ['FAIL'], 'old code (no version block) -> FAIL')

L.local_head = lambda repo: 'abc123def4567890'
NEW = {'status': 'ok', 'version': {'commit': 'abc123def456', 'branch': 'main', 'python': '3.12.7',
                                   'server': 'gunicorn', 'started_at': 'x'},
       'checks': {'database': 'ok', 'scraper_thread': 'running', 'scraper_last_cycle_age_seconds': 9.0,
                  'gunicorn_preload': False, 'billing': {'mode': 'live', 'check': 'ok', 'problems': 0},
                  'config_warnings': [], 'mercari_token': {'state': 'ok', 'hours_left': 90.0,
                                                           'expires_at': 'x'}}}
L.get = lambda url, timeout=45: R(200, body=NEW)
rep = Rep()
L.check_deploy(rep, 'https://api')
fails = [r for r in rep.rows if r[0] != 'PASS']
t.eq(fails, [], 'a correct deploy is all PASS')

bad = json.loads(json.dumps(NEW))
bad['version']['server'] = 'werkzeug'
bad['checks'].update(gunicorn_preload=True, scraper_thread='dead',
                     billing={'mode': 'test', 'check': 'ok', 'problems': 0},
                     config_warnings=['stripe_test_key_on_render', 'vision_missing'],
                     mercari_token={'state': 'expired', 'expires_at': '2026-10-16T07:00+00:00'})
L.get = lambda url, timeout=45: R(200, body=bad)
rep = Rep()
L.check_deploy(rep, 'https://api')
t.eq(rep.status('server'), ['WARN'], 'werkzeug -> Blueprint not applied (WARN)')
t.eq(rep.status('gunicorn preload'), ['WARN'], 'preload on -> WARN')
t.eq(rep.status('scanner thread'), ['FAIL'], 'dead scanner thread -> FAIL')
t.eq(rep.status('stripe'), ['FAIL'], 'test Stripe key on Render -> FAIL')
t.eq(sorted(rep.status('config')), ['FAIL', 'WARN'], 'stripe warning FAILs, vision only WARNs')
t.eq(rep.status('mercari token'), ['FAIL'], 'expired Mercari token -> FAIL')

t.done()
