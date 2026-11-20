# -*- coding: utf-8 -*-
"""
Launch gates (2026-11-11), through the REAL Flask endpoints and helpers:

  1. An unrecognised Stripe price grants BASIC, never Pro, and a Basic checkout
     never charges the legacy (Pro) STRIPE_PRICE_ID.
  2. An account with no paid plan cannot START, and the scheduler switches off
     one that is already armed (a subscription that lapsed while running).
  3. MAX_ACTIVE_USERS caps how many scanners run at once: START answers 409 past
     it, a scanner that is already on is never blocked, and checkout pauses for
     NEW subscribers only, so nobody pays for a spot they cannot use.

In-memory stand-in for user_settings: no database, no Stripe, no email.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import app as A
import scraper_multi_user as S
import scrape_logs

t = Suite('capacity-billing')

LOGS = []
scrape_logs.add_log = lambda uid, msg, kind='info': LOGS.append((uid, msg, kind))
A._auth_cache_get = lambda token: token          # Bearer <user_id> signs in as user_id

ROWS = {}                                          # user_id -> user_settings row
STATE = {'running': 0, 'locks': 0, 'sql': [], 'rollbacks': 0}


def account(uid, plan_tier='pro', is_pro=True, is_active=False):
    ROWS[uid] = {'user_id': uid, 'plan_tier': plan_tier, 'is_pro': is_pro,
                 'is_active': is_active, 'zip_code': '95212'}


class Cur(object):
    def __init__(self):
        self.res, self.rowcount, self.description = None, 0, None

    def execute(self, sql, args=None):
        s = ' '.join(sql.split())
        STATE['sql'].append(s)
        self.res, self.rowcount = None, 0
        if s.startswith('SELECT * FROM user_settings WHERE user_id'):
            row = ROWS.get(args[0])
            if row:
                self.description = [(k,) for k in row]
                self.res = tuple(row.values())
        elif s.startswith('SELECT plan_tier, is_pro FROM user_settings'):
            row = ROWS.get(args[0])
            self.res = (row['plan_tier'], row['is_pro']) if row else None
        elif 'pg_advisory_xact_lock' in s:
            STATE['locks'] += 1
        elif s.startswith('SELECT COUNT(*) FROM user_settings WHERE is_active = TRUE'):
            self.res = (STATE['running'],)
        elif s.startswith('UPDATE user_settings SET is_active = TRUE'):
            row = ROWS[args[0]]
            if not row['is_active']:
                row['is_active'], self.rowcount = True, 1
        elif s.startswith('UPDATE user_settings SET is_active = FALSE'):
            row = ROWS.get(args[0])
            if row and row['is_active']:
                row['is_active'], self.rowcount = False, 1
        # Anything else (the START countdown's term lookup) answers empty.

    def fetchone(self):
        return self.res

    def fetchall(self):
        return []

    def close(self):
        pass


class Conn(object):
    def cursor(self, cursor_factory=None):
        return Cur()

    def commit(self):
        pass

    def rollback(self):
        STATE['rollbacks'] += 1

    def close(self):
        pass


A.get_db_connection = lambda: Conn()
S.get_db_connection = lambda: Conn()
client = A.app.test_client()


def start(uid):
    STATE['sql'][:] = []
    return client.post('/api/start', headers={'Authorization': 'Bearer ' + uid})


def armed_by_last_call():
    return any(s.startswith('UPDATE user_settings SET is_active = TRUE') for s in STATE['sql'])


# ---------------------------------------------------------------------------
t.section('1. Stripe: unknown prices fail CLOSED to Basic')
os.environ['STRIPE_PRICE_BASIC_ID'] = 'price_basic_a,price_basic_b'
os.environ['STRIPE_PRICE_PRO_ID'] = 'price_pro'
os.environ['STRIPE_PRICE_ID'] = 'price_legacy'
t.eq(A._stripe_price_to_tier('price_basic_b'), 'basic', 'listed Basic price')
t.eq(A._stripe_price_to_tier('price_pro'), 'pro', 'listed Pro price')
t.eq(A._stripe_price_to_tier('price_legacy'), 'pro', 'legacy single-plan price stays Pro')
t.eq(A._stripe_price_to_tier('price_typo'), 'basic', 'UNRECOGNISED price -> Basic (was Pro)')
t.eq(A._stripe_price_to_tier(None), 'basic', 'missing price -> Basic (was Pro)')
os.environ['STRIPE_PRICE_PRO_ID'] = 'price_both'
os.environ['STRIPE_PRICE_BASIC_ID'] = 'price_both'
t.eq(A._stripe_price_to_tier('price_both'), 'basic', 'an id listed under BOTH resolves down')


class Rec(object):
    def __init__(self):
        self.args = None

    def execute(self, sql, args=None):
        self.args = args


for given, want in (('pro', 'pro'), ('basic', 'basic'), ('enterprise', 'basic'), (None, 'basic')):
    rec = Rec()
    A._upsert_pro_subscription(rec, 'u', 'cus', 'sub', None, False, plan_tier=given)
    t.eq(rec.args[4], want, 'subscription upsert plan_tier %r stores %r' % (given, want))

# Checkout: Basic never charges the legacy price.
CREATED = []


class _Sess(object):
    url = 'https://checkout.stripe.test/s'


A.stripe.api_key = 'sk_test_offline'
A.stripe.checkout.Session.create = staticmethod(lambda **kw: CREATED.append(kw) or _Sess())
os.environ['MAX_ACTIVE_USERS'] = '0'                 # capacity is section 3
os.environ.pop('STRIPE_PRICE_BASIC_ID')
os.environ.pop('STRIPE_PRICE_PRO_ID')
os.environ['STRIPE_PRICE_ID'] = 'price_legacy'
account('buyer', plan_tier=None, is_pro=False)


def checkout(uid, plan):
    return client.post('/api/create-checkout-session', json={'plan': plan},
                       headers={'Authorization': 'Bearer ' + uid})


r = checkout('buyer', 'basic')
t.ok(r.status_code == 500 and 'STRIPE_PRICE_BASIC_ID' in r.get_json()['error'] and not CREATED,
     'Basic with only the legacy price set -> clear 500, nothing charged')
r = checkout('buyer', 'pro')
t.ok(r.status_code == 200 and CREATED and CREATED[-1]['line_items'][0]['price'] == 'price_legacy',
     'Pro may still fall back to the legacy price')

# ---------------------------------------------------------------------------
t.section('2. No paid plan: START refused, armed rows switched off')
account('free', plan_tier=None, is_pro=False)
r = start('free')
t.ok(r.status_code == 402 and r.get_json().get('code') == 'plan_required', 'no plan -> 402')
t.ok(not armed_by_last_call() and not ROWS['free']['is_active'], '...and nothing was armed')
r = start('nobody')
t.ok(r.status_code == 402, 'no settings row at all -> 402')
account('legacy', plan_tier=None, is_pro=True)
r = start('legacy')
t.ok(r.status_code == 200 and ROWS['legacy']['is_active'],
     'is_pro with no plan_tier still resolves to a paid plan (Basic) and arms')

account('lapsed', plan_tier='inactive', is_pro=False, is_active=True)
del LOGS[:]
t.ok(S._disarm_unpaid('lapsed') is True and not ROWS['lapsed']['is_active'],
     'scheduler switches off an armed account with no plan')
t.ok(LOGS and LOGS[-1][0] == 'lapsed' and 'no active plan' in LOGS[-1][1],
     '...and tells that user why in the Live Console')
t.ok(S._disarm_unpaid('lapsed') is False, 'a second pass changes nothing')
src = inspect.getsource(S.main)
t.ok("if tier == 'inactive':" in src and '_disarm_unpaid(uid)' in src,
     'main() routes inactive-tier rows to _disarm_unpaid before scheduling them')

# ---------------------------------------------------------------------------
t.section('3. MAX_ACTIVE_USERS caps running scanners')
os.environ['MAX_ACTIVE_USERS'] = ''
t.eq(A._max_active_users(), 10, 'unset -> default 10')
os.environ['MAX_ACTIVE_USERS'] = 'ten'
t.eq(A._max_active_users(), 10, 'garbage -> default 10, not a crash or no cap')
os.environ['MAX_ACTIVE_USERS'] = '0'
t.eq(A._max_active_users(), 0, '0 -> no cap')

os.environ['MAX_ACTIVE_USERS'] = '3'
account('p1')
STATE['running'], STATE['locks'] = 2, 0
r = start('p1')
t.ok(r.status_code == 200 and ROWS['p1']['is_active'], '2 of 3 running -> START arms')
t.eq(STATE['locks'], 1, 'arming took the advisory lock (no two STARTs share the last spot)')

account('p2')
STATE['running'], STATE['rollbacks'] = 3, 0
r = start('p2')
body = r.get_json()
t.ok(r.status_code == 409 and body.get('code') == 'at_capacity' and body.get('cap') == 3,
     '3 of 3 running -> 409 at_capacity')
t.ok('START again later' in body.get('error', ''), '...with a message written for the user')
t.ok(not armed_by_last_call() and not ROWS['p2']['is_active'] and STATE['rollbacks'] == 1,
     '...arms nothing and releases the lock')

account('on', is_active=True)
STATE['locks'] = 0
r = start('on')
t.ok(r.status_code == 200 and STATE['locks'] == 0,
     'a scanner that is already ON is never blocked or counted against itself')

os.environ['MAX_ACTIVE_USERS'] = '0'
account('p3')
STATE['running'] = 500
r = start('p3')
t.ok(r.status_code == 200 and ROWS['p3']['is_active'], 'MAX_ACTIVE_USERS=0 -> no cap')

# Checkout pauses for NEW subscribers only.
os.environ['MAX_ACTIVE_USERS'] = '3'
os.environ['STRIPE_PRICE_PRO_ID'] = 'price_pro'
os.environ['STRIPE_PRICE_BASIC_ID'] = 'price_basic'
STATE['running'] = 3
del CREATED[:]
r = checkout('buyer', 'pro')
t.ok(r.status_code == 409 and r.get_json().get('code') == 'at_capacity' and not CREATED,
     'full -> a NEW subscriber is stopped before Stripe (nothing charged)')
account('upgrader', plan_tier='basic', is_pro=False)
r = checkout('upgrader', 'pro')
t.ok(r.status_code == 200 and CREATED, 'full -> an existing Basic upgrading to Pro is not blocked')
STATE['running'] = 2
del CREATED[:]
r = checkout('buyer', 'basic')
t.ok(r.status_code == 200 and CREATED and CREATED[-1]['line_items'][0]['price'] == 'price_basic',
     'room left -> checkout proceeds at the Basic price')

t.done()
