# -*- coding: utf-8 -*-
"""
Deploy safety and the boot self-check (2026-11-12). No database, no network,
no real threads touching anything: every target is stubbed.

Pins:
  * `python app.py` honours an explicit ENABLE_SCRAPER_THREAD=0 (it used to
    scan the PRODUCTION database anyway, since the local .env points at it);
    unset still starts the loop, because production ran that way pre-gunicorn;
  * under gunicorn the scraper/cleanup threads start on the FIRST REQUEST, in
    the serving process — never at import, which --preload (Render's default
    GUNICORN_CMD_ARGS) would run in the master;
  * /health reports version, server, scanner thread, preload, billing, config
    warnings and Mercari token state, and never leaks exception text;
  * boot_check prints presence and modes, never a secret value;
  * verify_stripe reports a bad id instead of crashing (it used to TypeError).
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
os.environ['BOOT_SELF_CHECK'] = '0'      # no Stripe calls from the app import
import app as A
import boot_check as B
import verify_stripe as V
import scraper_multi_user as S

t = Suite('boot')


def setenv(**kw):
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
t.section('1. `python app.py` scraper decision')
setenv(DISABLE_SCRAPER_THREAD=None, ENABLE_SCRAPER_THREAD=None)
t.eq(A._main_scraper_decision(), 'start', 'nothing set -> start (pre-gunicorn production behaviour kept)')
setenv(ENABLE_SCRAPER_THREAD='0')
t.eq(A._main_scraper_decision(), 'explicit-off', 'ENABLE_SCRAPER_THREAD=0 is now honoured')
setenv(ENABLE_SCRAPER_THREAD='1', DISABLE_SCRAPER_THREAD='1')
t.eq(A._main_scraper_decision(), 'disabled', 'DISABLE_SCRAPER_THREAD=1 wins over everything')
setenv(DISABLE_SCRAPER_THREAD=None)
A._scraper_thread_started = True
t.eq(A._main_scraper_decision(), 'already', 'a loop started at import is never doubled')
A._scraper_thread_started = False
t.ok(not A._UNDER_GUNICORN, 'a plain import (tests, python app.py) is not mistaken for gunicorn')

# ---------------------------------------------------------------------------
t.section('2. Under gunicorn, threads start on the first request, in that process')
release = threading.Event()
CALLS = {'scraper': [], 'cleanup': []}


def fake_scraper():
    CALLS['scraper'].append(os.getpid())
    release.wait(5)


def fake_cleanup():
    CALLS['cleanup'].append(os.getpid())


A.start_background_scraper = fake_scraper
A.cleanup_old_listings = fake_cleanup
S._mercari_token_state = lambda: ('ok', time.time() + 30 * 3600)


class Cur(object):
    def __init__(self, fail=None):
        self.fail = fail

    def execute(self, sql, args=None):
        if self.fail:
            raise self.fail

    def fetchone(self):
        return (1,)

    def close(self):
        pass


class Conn(object):
    def __init__(self, fail=None):
        self.fail = fail

    def cursor(self, cursor_factory=None):
        return Cur(self.fail)

    def close(self):
        pass


A.get_db_connection = lambda: Conn()
setenv(ENABLE_SCRAPER_THREAD='1', ENABLE_CLEANUP_THREAD='1')
A._UNDER_GUNICORN = True
A._deferred_threads_checked = False
client = A.app.test_client()

t.eq(CALLS['scraper'], [], 'nothing started before the first request')
r = client.get('/health')
time.sleep(0.2)
t.eq(len(CALLS['scraper']), 1, 'first request starts the scraper loop')
t.eq(len(CALLS['cleanup']), 1, '...and the cleanup loop')
t.eq(CALLS['scraper'][0], os.getpid(), '...in the process serving requests')
body = r.get_json()
t.eq(body['checks'].get('scraper_thread'), 'running', '/health reports the loop running')
client.get('/health')
time.sleep(0.1)
t.eq(len(CALLS['scraper']), 1, 'a second request starts nothing more')

t.section('3. /health deploy facts')
t.ok('version' in body and 'python' in body['version'], 'version block present (proves new code is live)')
t.ok(body['version'].get('server') not in (None, ''), 'server software reported')
t.eq(body['checks'].get('gunicorn_preload'), False, 'not preloaded when import and serving pid match')
t.ok('billing' in body['checks'] and 'config_warnings' in body['checks'], 'billing + config warnings present')
t.eq(body['checks'].get('mercari_token', {}).get('state'), 'ok', 'Mercari token state present')
t.eq(r.status_code, 200, 'status code still keyed on the database only')

real_pid = A._IMPORT_PID
A._IMPORT_PID = os.getpid() + 1          # as if imported in a preloading master
t.eq(client.get('/health').get_json()['checks'].get('gunicorn_preload'), True,
     'preload is detected when the importing pid differs')
A._IMPORT_PID = real_pid

release.set()
time.sleep(0.2)
t.eq(client.get('/health').get_json()['checks'].get('scraper_thread'), 'dead',
     'a loop that exited is reported dead, not running')

A.get_db_connection = lambda: Conn(RuntimeError('could not connect to db.secret-host.internal'))
A._mercari_health_cache.update(at=0.0, value=None)
S._mercari_token_state = lambda: (_ for _ in ()).throw(AssertionError('must not query a down DB'))
r = client.get('/health')
t.ok(r.status_code == 503 and 'secret-host' not in r.get_data(as_text=True),
     'database failure -> 503 without the exception text')
t.ok('mercari_token' not in r.get_json()['checks'],
     'with the DB down, /health does not also query the token (stays fast)')

# ---------------------------------------------------------------------------
t.section('4. boot_check never prints a secret')
SECRETS = {
    'DATABASE_URL': 'postgres://u:SECRET-DB-PW@host/db',
    'SUPABASE_URL': 'https://SECRET-PROJECT.supabase.co',
    'SUPABASE_ANON_KEY': 'SECRET-ANON', 'SUPABASE_SERVICE_ROLE_KEY': 'SECRET-SERVICE',
    'STRIPE_SECRET_KEY': 'sk_live_SECRETSTRIPE', 'STRIPE_WEBHOOK_SECRET': 'whsec_SECRETHOOK',
    'STRIPE_PRICE_BASIC_ID': 'price_b', 'STRIPE_PRICE_PRO_ID': 'price_p',
    'MAILGUN_API_KEY': 'SECRET-MG', 'MAILGUN_DOMAIN': 'mg.example', 'MAILGUN_FROM_EMAIL': 'a@b.c',
    'VAPID_PUBLIC_KEY': 'SECRET-VPUB', 'VAPID_PRIVATE_KEY': 'SECRET-VPRIV',
    'TWILIO_ACCOUNT_SID': 'SECRET-SID', 'TWILIO_AUTH_TOKEN': 'SECRET-TOK', 'TWILIO_FROM_NUMBER': '+1555',
    'FB_PROXY_URL': 'http://SECRET-PROXY-USER:pw@proxy:1', 'GOOGLE_VISION_API_KEY': 'SECRET-VISION',
    'AI_IMAGE_FILTER_PLATFORMS': 'facebook', 'RENDER': 'true', 'RENDER_GIT_COMMIT': 'abcdef1234567890',
    'MAX_ACTIVE_USERS': '10',
}
text = '\n'.join(B.config_report(SECRETS))
leaked = [k for k, v in SECRETS.items() if 'SECRET' in v and v in text]
t.eq(leaked, [], 'no secret value appears in the boot report')
t.ok('stripe=LIVE' in text, 'Stripe key MODE is reported')
t.ok('MAX_ACTIVE_USERS=10' in text, 'non-secret switches are reported with values')
t.ok('commit=abcdef123456' in text, 'commit from RENDER_GIT_COMMIT')
t.eq(B.warning_codes(SECRETS), [], 'a complete live config has no warnings')

t.section('5. boot_check warnings name what silently breaks')
cfg = dict(SECRETS)
cfg.pop('GOOGLE_VISION_API_KEY')
cfg.pop('FB_PROXY_URL')
cfg['PROXY_URL'] = 'http://shared'
cfg['STRIPE_SECRET_KEY'] = 'sk_test_x'
codes = B.warning_codes(cfg)
t.ok('vision_missing' in codes, 'advertised image filter without Vision -> vision_missing')
t.ok('fb_proxy_missing' in codes, 'PROXY_URL alone does not count for Facebook (it never falls back)')
t.ok('stripe_test_key_on_render' in codes, 'a test key on Render is flagged')
t.ok(B.enabled({'RENDER': 'true'}), 'runs on Render by default')
t.ok(not B.enabled({'RENDER': 'true', 'BOOT_SELF_CHECK': '0'}), 'BOOT_SELF_CHECK=0 turns it off')
t.ok(not B.enabled({}), 'off locally and in tests')

# ---------------------------------------------------------------------------
t.section('6. verify_stripe: resolves ids, reports problems, never crashes')


class R(object):
    def __init__(self, status, body):
        self.status_code = status
        self._b = body
        self.text = str(body)

    def json(self):
        return self._b


def make_http(prices, coupon=None, endpoints=None):
    def http_get(url, auth=None, params=None, timeout=None):
        if '/prices/' in url:
            pid = url.rsplit('/', 1)[1]
            return prices.get(pid) or R(404, {'error': {'message': 'No such price: ' + pid}})
        if '/coupons/' in url:
            return coupon or R(404, {'error': {'message': 'No such coupon'}})
        if url.endswith('/webhook_endpoints'):
            return R(200, {'data': endpoints if endpoints is not None else []})
        raise AssertionError(url)
    return http_get


GOOD_PRICE = lambda amt: R(200, {'unit_amount': amt, 'currency': 'usd', 'livemode': True,
                                 'active': True, 'recurring': {'interval': 'month'}})
GOOD_EP = [{'url': 'https://svc.onrender.com/api/webhook', 'status': 'enabled',
            'enabled_events': sorted(V.REQUIRED_EVENTS)}]
env = {'STRIPE_SECRET_KEY': 'sk_live_SECRETSTRIPE', 'STRIPE_PRICE_BASIC_ID': 'price_b',
       'STRIPE_PRICE_PRO_ID': 'price_p', 'STRIPE_PREBETA_COUPON_ID': 'cpn',
       'STRIPE_WEBHOOK_SECRET': 'whsec_x'}
out = []
mode, problems = V.check_stripe(env, emit=out.append, service_url='https://svc.onrender.com',
                                http_get=make_http({'price_b': GOOD_PRICE(499), 'price_p': GOOD_PRICE(999)},
                                                   R(200, {'percent_off': 50, 'duration': 'forever',
                                                           'livemode': True, 'valid': True}), GOOD_EP))
t.eq((mode, problems), ('LIVE', []), 'a correct live config has no problems')
t.ok(any('4.99 USD/month' in l for l in out), 'amounts are printed for a human to compare')
t.ok(not any('SECRETSTRIPE' in l for l in out), 'the key itself is never printed')

mode, problems = V.check_stripe(env, emit=lambda l: None,
                                http_get=make_http({'price_p': GOOD_PRICE(999)}, None, GOOD_EP))
t.ok(any('STRIPE_PRICE_BASIC_ID' in p and '404' in p for p in problems),
     'a 404 price is REPORTED (used to crash with TypeError)')
t.ok(any('STRIPE_PREBETA_COUPON_ID' in p for p in problems), 'a missing coupon is reported')

archived = R(200, {'unit_amount': 499, 'currency': 'usd', 'livemode': True, 'active': False})
_, problems = V.check_stripe(env, emit=lambda l: None,
                             http_get=make_http({'price_b': archived, 'price_p': GOOD_PRICE(999)},
                                                R(200, {'percent_off': 50, 'valid': True}), GOOD_EP))
t.ok(any('archived' in p for p in problems), 'an archived price is a problem')

disabled_ep = [dict(GOOD_EP[0], status='disabled')]
_, problems = V.check_stripe(env, emit=lambda l: None,
                             http_get=make_http({'price_b': GOOD_PRICE(499), 'price_p': GOOD_PRICE(999)},
                                                R(200, {'percent_off': 50, 'valid': True}), disabled_ep))
t.ok(any('ENABLED webhook' in p for p in problems), 'a disabled webhook endpoint is a problem')
t.eq(V.stripe_key_mode('rk_live_abc'), 'LIVE', 'restricted live keys are recognised')
mode, problems = V.check_stripe({}, emit=lambda l: None, http_get=make_http({}))
t.ok(mode is None and problems, 'no key -> reported, no network calls needed')

t.section('7. boot_check.start feeds /health')
B._started = False
real_get = V.requests.get
V.requests.get = make_http({'price_b': GOOD_PRICE(499), 'price_p': GOOD_PRICE(999)},
                           R(200, {'percent_off': 50, 'valid': True, 'livemode': True}), GOOD_EP)
lines = []
t.ok(B.start(dict(env, RENDER='true'), emit=lines.append, background=False), 'start() runs when enabled')
V.requests.get = real_get
summary = B.health_summary(dict(env, RENDER='true'))
t.eq(summary['billing'], {'mode': 'live', 'check': 'ok', 'problems': 0}, 'billing summary: live, ok')
t.ok(any(l.startswith('stripe: all ids resolve') for l in lines), 'boot log states the verdict')
n = len(lines)
t.ok(B.start(dict(env, RENDER='true'), emit=lines.append) and len(lines) == n,
     'a second start() re-runs nothing')

t.done()
