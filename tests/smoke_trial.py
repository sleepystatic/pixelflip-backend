# -*- coding: utf-8 -*-
"""
Free trial + repeat-trial guard (2026-11-20), through the REAL Flask endpoints.

No database, no Stripe, no network: an in-memory user_settings / trial_claims /
auth.users and stubbed Stripe objects. Pins:

  * normalisers: Gmail dots and +tags, an IPv6 /64, "123 Main Street Apt #2" =
    "123 MAIN ST UNIT 2", device-id validation; only keyed hashes leave them;
  * BEFORE checkout, an account that used a trial or was ever a customer, or an
    email / IP / device seen in an earlier checkout, gets NO trial offered — and
    the sale still goes ahead; a fresh one gets 3 days, card required, full
    billing address, and the one-trial rule printed above the Stripe button;
  * AFTER checkout, a reused card or billing address, a prepaid card, no card,
    or a device that raced in on two accounts ENDS the trial (charged today);
  * the webhook and the dashboard's return trip settle a subscription ONCE;
  * a trial that cannot be verified fails the webhook (Stripe retries); a paid
    no-trial checkout is never blocked by the guard;
  * a past customer (no trial) cannot come back for one on a new account;
  * 'trialing' is entitled and its end date reaches the dashboard;
  * paying accounts cannot open a second subscription; cancelling a Basic plan
    no longer marks it is_pro.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
os.environ['BOOT_SELF_CHECK'] = '0'
os.environ['TRIAL_HASH_KEY'] = 'test-key'
import app as A
import trial_guard as G

t = Suite('trial')

# ---------------------------------------------------------------------------
t.section('1. Normalisers and hashing')
t.eq(G.normalize_email('J.Doe+deals@GoogleMail.com'), 'jdoe@gmail.com', 'Gmail: dots, +tag, googlemail')
t.eq(G.normalize_email('jdoe@gmail.com'), 'jdoe@gmail.com', '...same person, same key')
t.eq(G.normalize_email('Sam+x@Outlook.com'), 'sam@outlook.com', '+tags stripped everywhere')
t.eq(G.normalize_email('s.am@outlook.com'), 's.am@outlook.com', 'dots only ignored for Gmail')
t.ok(G.normalize_email('nope') is None and G.normalize_email('a@b@c.com') is None, 'non-emails -> None')
t.eq(G.ip_key('203.0.113.9'), '203.0.113.9', 'IPv4 kept whole')
t.eq(G.ip_key('2001:db8:1:2:aaaa::1'), G.ip_key('2001:db8:1:2:bbbb::9'), 'IPv6: same /64 = same key')
t.ok(G.ip_key('2001:db8:1:3::1') != G.ip_key('2001:db8:1:2::1'), '...a different /64 is different')
t.eq(G.ip_key('::ffff:203.0.113.9'), '203.0.113.9', 'IPv4-mapped IPv6 = the IPv4')
t.eq(G.ip_key('garbage'), None, 'garbage IP -> None')
A1 = {'line1': '123 Main Street', 'line2': 'Apt #2', 'city': 'Stockton', 'state': 'CA',
      'postal_code': '95210-1234', 'country': 'US'}
A2 = {'line1': '123 MAIN ST', 'line2': 'UNIT 2', 'city': 'stockton', 'state': 'ca',
      'postal_code': '95210', 'country': 'us'}
A3 = dict(A2, line2='Apt 3')
t.eq(G.normalize_address(A1), G.normalize_address(A2), 'Street=St, #2=Unit 2=Apt 2, ZIP+4=ZIP')
t.ok(G.normalize_address(A1) != G.normalize_address(A3), 'a different flat is a different address')
t.eq(G.normalize_address({'postal_code': '95210', 'country': 'US'}), None, 'no street line -> no signal')
t.ok(G.clean_device_id('3f0c2a8e-1b2c-4d5e-8f90-1234567890ab'), 'a UUID is a device id')
t.ok(G.clean_device_id('<script>') is None and G.clean_device_id('abc') is None, 'junk device ids ignored')
sig = G.pre_checkout_signals('alice@gmail.com', '203.0.113.9', '3f0c2a8e-1b2c-4d5e-8f90-1234567890ab')
t.eq(sorted(sig), ['device', 'email', 'ip'], 'three pre-checkout signals')
t.ok(all(len(v) == 64 and 'alice' not in v and '203' not in v for v in sig.values()),
     'stored as 64-hex keyed hashes, never the raw value')

# ---------------------------------------------------------------------------
# In-memory database
ROWS, CLAIMS, EMAILS = {}, [], {}
CLAIM_COLS = ('email_hash', 'ip_hash', 'device_hash', 'card_fingerprint', 'address_hash')


def account(uid, email=None, **kw):
    row = {'user_id': uid, 'plan_tier': None, 'is_pro': False, 'is_active': False,
           'stripe_customer_id': None, 'stripe_subscription_id': None, 'trial_used_at': None,
           'trial_outcome': None, 'subscription_trial_end': None, 'subscription_status': 'inactive',
           'subscription_cancel_at_period_end': False, 'subscription_current_period_end': None}
    row.update(kw)
    ROWS[uid] = row
    if email:
        EMAILS[uid] = email
    return row


class Cur(object):
    def __init__(self, dict_mode):
        self.dict_mode, self.res, self.many, self.rowcount, self.description = dict_mode, None, [], 0, None

    def _one(self, d):
        self.res = d if self.dict_mode else (tuple(d.values()) if d else None)

    def execute(self, sql, args=None):
        s = ' '.join(sql.split())
        args = list(args or [])
        self.res, self.many, self.rowcount = None, [], 0
        if s.startswith(('SAVEPOINT', 'RELEASE SAVEPOINT', 'ROLLBACK TO SAVEPOINT')) or 'pg_advisory_xact_lock' in s:
            return
        if s.startswith('SELECT plan_tier, is_pro, stripe_customer_id'):
            r = ROWS.get(args[0])
            self.res = (r['plan_tier'], r['is_pro'], r['stripe_customer_id'],
                        r['stripe_subscription_id'], r['trial_used_at']) if r else None
        elif s.startswith('SELECT email FROM auth.users'):
            self.res = (EMAILS[args[0]],) if args[0] in EMAILS else None
        elif s.startswith('SELECT plan_tier, is_pro FROM user_settings'):
            r = ROWS.get(args[0])
            self.res = (r['plan_tier'], r['is_pro']) if r else None
        elif s.startswith('SELECT COUNT(*) FROM user_settings WHERE user_id <>'):
            self.res = (sum(1 for u, r in ROWS.items() if u != args[0]
                            and (r['stripe_subscription_id'] or '').strip()
                            and ((r['plan_tier'] or '') in ('basic', 'pro') or r['is_pro'])),)
        elif s.startswith('SELECT outcome FROM trial_claims'):
            hit = [c for c in CLAIMS if c['subscription_id'] == args[0]]
            self._one({'outcome': hit[0]['outcome']} if hit else None)
        elif s.startswith('SELECT email_hash, ip_hash, device_hash'):
            cols = re.findall(r'(email_hash|ip_hash|device_hash|card_fingerprint|address_hash) = %s', s)
            exclude, values = args[0], [a for a in args[1:] if not isinstance(a, int)]
            out = []
            for c in CLAIMS:
                if c['subscription_id'] == exclude:
                    continue
                if any(c[col] == v for col, v in zip(cols, values)):
                    out.append({k: c[k] for k in CLAIM_COLS} if self.dict_mode
                               else tuple(c[k] for k in CLAIM_COLS))
            self.many = out
        elif s.startswith('INSERT INTO trial_claims'):
            keys = ('user_id', 'subscription_id', 'checkout_session') + CLAIM_COLS + ('outcome',)
            CLAIMS.append(dict(zip(keys, args)))
        elif s.startswith('UPDATE user_settings SET trial_used_at'):
            trialing, outcome, trial_end, uid = args
            r = ROWS[uid]
            if trialing and not r['trial_used_at']:
                r['trial_used_at'] = 'now'
            r['trial_outcome'], r['subscription_trial_end'] = outcome, trial_end
        elif s.startswith('INSERT INTO user_settings'):
            uid, _platforms, _ai, is_pro, tier, cus, sub, pe, ca = args
            r = ROWS.get(uid) or account(uid)
            r.update(is_pro=is_pro, plan_tier=tier, stripe_customer_id=cus, stripe_subscription_id=sub,
                     subscription_status='active', subscription_current_period_end=pe,
                     subscription_cancel_at_period_end=ca)
        elif s.startswith('SELECT user_id FROM user_settings WHERE stripe_customer_id'):
            hit = [u for u, r in ROWS.items() if r['stripe_customer_id'] == args[0]]
            self._one({'user_id': hit[0]} if hit else None)
        elif s.startswith('UPDATE user_settings SET is_pro = %s, subscription_status = %s'):
            is_pro, status, tier, ai, pe, ca, sub_id, trial_end, uid = args
            ROWS[uid].update(is_pro=is_pro, subscription_status=status, plan_tier=tier,
                             subscription_current_period_end=pe, subscription_cancel_at_period_end=ca,
                             subscription_trial_end=trial_end)
        elif s.startswith('SELECT stripe_subscription_id FROM user_settings'):
            r = ROWS.get(args[0])
            self._one({'stripe_subscription_id': r['stripe_subscription_id']} if r else None)
        elif s.startswith('UPDATE user_settings SET subscription_cancel_at_period_end = TRUE'):
            ROWS[args[1]]['subscription_cancel_at_period_end'] = True
        elif s.startswith('SELECT * FROM user_settings WHERE user_id'):
            r = ROWS.get(args[0])
            if r:
                self.description = [(k,) for k in r]
                self.res = r if self.dict_mode else tuple(r.values())
        elif s.startswith('UPDATE user_settings SET is_active = TRUE'):
            r = ROWS[args[0]]
            if not r['is_active']:
                r['is_active'], self.rowcount = True, 1
        # Anything else (START's countdown lookups) answers empty.

    def fetchone(self):
        return self.res

    def fetchall(self):
        return self.many

    def close(self):
        pass


class Conn(object):
    def cursor(self, cursor_factory=None):
        return Cur(cursor_factory is not None)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


A.get_db_connection = lambda: Conn()
A._auth_cache_get = lambda token: token
client = A.app.test_client()

# ---------------------------------------------------------------------------
# Stripe stand-ins
SESSIONS_CREATED, MODIFIED = [], []
SUBS, PMS, SESSIONS = {}, {}, {}


class _Sess(object):
    url = 'https://checkout.stripe.test/s'


def card(fp, funding='credit', addr=None):
    return {'id': 'pm_' + (fp or 'none'), 'card': {'fingerprint': fp, 'funding': funding},
            'billing_details': {'address': addr or {}}}


def make_sub(sub_id, customer, status='trialing', pm=None, price='price_pro'):
    SUBS[sub_id] = {'id': sub_id, 'customer': customer, 'status': status, 'trial_end': 1900000000,
                    'current_period_end': 1900000000, 'cancel_at_period_end': False,
                    'default_payment_method': pm,
                    'items': {'data': [{'price': {'id': price}}]}}
    return SUBS[sub_id]


def _modify(sub_id, **kw):
    MODIFIED.append((sub_id, kw))
    if kw.get('trial_end') == 'now':
        SUBS[sub_id]['status'] = 'active'
    return SUBS[sub_id]


def _retrieve_sub(sub_id, expand=None):
    s = SUBS[sub_id]
    if s.get('_explode'):
        raise RuntimeError('stripe unreachable')
    return dict(s)


A.stripe.api_key = 'sk_test_offline'
A.stripe.checkout.Session.create = staticmethod(lambda **kw: SESSIONS_CREATED.append(kw) or _Sess())
A.stripe.checkout.Session.retrieve = staticmethod(lambda sid, expand=None: SESSIONS[sid])
A.stripe.Subscription.retrieve = staticmethod(_retrieve_sub)
A.stripe.Subscription.modify = staticmethod(_modify)
A.stripe.PaymentMethod.retrieve = staticmethod(lambda pm_id: PMS[pm_id])
A.stripe.Webhook.construct_event = staticmethod(lambda payload, sig, secret: json.loads(payload))
os.environ['STRIPE_WEBHOOK_SECRET'] = 'whsec_test'
os.environ['STRIPE_PRICE_PRO_ID'] = 'price_pro'
os.environ['STRIPE_PRICE_BASIC_ID'] = 'price_basic'
os.environ.pop('STRIPE_PREBETA_COUPON_ID', None)
os.environ['MAX_ACTIVE_USERS'] = '0'

DEV = {'a': '11111111-aaaa-4aaa-8aaa-111111111111', 'b': '22222222-bbbb-4bbb-8bbb-222222222222',
       'c': '33333333-cccc-4ccc-8ccc-333333333333'}


def checkout(uid, ip='203.0.113.10', device=None, plan='pro'):
    body = {'plan': plan}
    if device:
        body['device_id'] = device
    r = client.post('/api/create-checkout-session', json=body,
                    headers={'Authorization': 'Bearer ' + uid, 'CF-Connecting-IP': ip})
    return r, (SESSIONS_CREATED[-1] if SESSIONS_CREATED else None)


def complete_via_webhook(uid, sess_kwargs, sub, session_id):
    """Turn a created session into a completed one and deliver the webhook."""
    SESSIONS[session_id] = {'id': session_id, 'mode': 'subscription', 'client_reference_id': uid,
                            'customer': sub['customer'], 'subscription': sub['id'],
                            'metadata': dict(sess_kwargs['metadata']), 'payment_status':
                            'no_payment_required' if sub['status'] == 'trialing' else 'paid'}
    event = {'type': 'checkout.session.completed', 'data': {'object': SESSIONS[session_id]}}
    return client.post('/api/webhook', data=json.dumps(event), headers={'Stripe-Signature': 't=1'})


# ---------------------------------------------------------------------------
t.section('2. Before checkout: who is offered a trial')
account('alice', email='Alice.Smith@gmail.com')
r, kw = checkout('alice', device=DEV['a'])
body = r.get_json()
t.ok(r.status_code == 200 and body.get('trial_offered') is True and body.get('trial_days') == 3,
     'a brand-new person is offered the 3-day trial')
sd = kw.get('subscription_data') or {}
t.eq(sd.get('trial_period_days'), 3, 'Stripe session: trial_period_days 3')
t.eq(((sd.get('trial_settings') or {}).get('end_behavior') or {}).get('missing_payment_method'), 'cancel',
     '...no card at trial end = cancelled, never free')
t.eq((kw.get('payment_method_collection'), kw.get('billing_address_collection')), ('always', 'required'),
     '...card required, full billing address required')
t.ok('One free trial per person' in ((kw.get('custom_text') or {}).get('submit') or {}).get('message', ''),
     '...the one-trial rule is printed above the Stripe button')
t.eq(kw.get('customer_email'), 'Alice.Smith@gmail.com', '...Stripe customer email = account email')
meta = kw['metadata']
raw = json.dumps({k: v for k, v in meta.items() if k.startswith('sig_')}).lower()
t.ok(all(meta.get(k) for k in ('sig_email', 'sig_ip', 'sig_device')) and 'alice.smith' not in raw
     and '203.0.113.10' not in raw and DEV['a'] not in raw,
     'signal hashes travel in metadata; the raw email, IP and device id do not')
ALICE_KW = kw

account('used', email='used@example.com', trial_used_at='2026-11-01')
r, kw = checkout('used', ip='198.51.100.1', device=DEV['b'])
t.ok(r.get_json().get('trial_offered') is False and 'subscription_data' not in kw,
     'an account that already had a trial: no trial, checkout still goes ahead')
t.ok('billing starts today' in (r.get_json().get('trial_note') or ''), '...and the dashboard is told why, plainly')
account('excustomer', email='ex@example.com', stripe_customer_id='cus_old')
r, kw = checkout('excustomer', ip='198.51.100.2', device=DEV['b'])
t.ok(r.get_json().get('trial_offered') is False, 'a former customer: no trial')

# Complete Alice's checkout so her signals become "seen".
make_sub('sub_alice', 'cus_alice', pm=card('fp_A', addr=A1))
r = complete_via_webhook('alice', ALICE_KW, SUBS['sub_alice'], 'cs_alice')
t.eq(r.status_code, 200, 'Alice completes checkout')

account('alice2', email='alicesmith+again@gmail.com')
r, kw = checkout('alice2', ip='192.0.2.50', device=DEV['c'])
t.ok(r.get_json().get('trial_offered') is False, 'same person, new account, Gmail alias of the email: no trial')
account('sameip', email='other@example.com')
r, kw = checkout('sameip', ip='203.0.113.10', device=DEV['c'])
t.ok(r.get_json().get('trial_offered') is False, 'same IP address as an earlier trial: no trial')
account('samedev', email='third@example.com')
r, kw = checkout('samedev', ip='192.0.2.77', device=DEV['a'])
t.ok(r.get_json().get('trial_offered') is False, 'same browser device id as an earlier trial: no trial')
account('fresh', email='fresh@example.com')
r, kw = checkout('fresh', ip='192.0.2.99', device=DEV['c'])
t.ok(r.get_json().get('trial_offered') is True, 'a genuinely new person on a new network and browser: trial')
FRESH_KW = kw

real_conn = A.get_db_connection
A.get_db_connection = lambda: None
r, kw = checkout('ghost', ip='192.0.2.200')
t.ok(r.status_code == 200 and r.get_json().get('trial_offered') is False and 'subscription_data' not in kw,
     'database unreadable: no trial, but the sale still goes ahead')
A.get_db_connection = real_conn

account('payer', email='payer@example.com', plan_tier='basic', stripe_customer_id='cus_p',
        stripe_subscription_id='sub_p', subscription_status='active')
n = len(SESSIONS_CREATED)
r, _ = checkout('payer', ip='192.0.2.201')
t.ok(r.status_code == 409 and r.get_json().get('code') == 'already_subscribed' and len(SESSIONS_CREATED) == n,
     'a paying account cannot open a SECOND subscription (would bill twice)')

# ---------------------------------------------------------------------------
t.section('3. After checkout: card, address, prepaid, races')
t.eq(ROWS['alice']['trial_outcome'], 'granted', 'Alice: new card + address -> trial granted')
t.eq(ROWS['alice']['subscription_trial_end'], 1900000000, '...trial end stored for the dashboard')
t.ok(ROWS['alice']['trial_used_at'] and not MODIFIED, '...account marked as having used its trial')

SESSIONS['cs_alice']['subscription'] = 'sub_alice'
r = client.post('/api/complete-checkout', json={'session_id': 'cs_alice'},
                headers={'Authorization': 'Bearer alice'})
t.ok(r.status_code == 200 and r.get_json().get('trial_outcome') == 'granted' and not MODIFIED,
     'the dashboard return trip gets the SAME outcome; nothing re-decided')
t.eq(sum(1 for c in CLAIMS if c['subscription_id'] == 'sub_alice'), 1, 'one claim per subscription')


def trial_checkout(uid, fp, funding='credit', addr=None, device=None, ip='192.0.2.150', email=None):
    account(uid, email=email or uid + '@example.com')
    _r, kw = checkout(uid, ip=ip, device=device)
    sub = make_sub('sub_' + uid, 'cus_' + uid, pm=card(fp, funding, addr) if fp is not False else None)
    if fp is False:
        sub['default_payment_method'] = None
    return complete_via_webhook(uid, kw, sub, 'cs_' + uid), kw


del MODIFIED[:]
r, _ = trial_checkout('bob', 'fp_A', addr={'line1': '9 Elm St', 'postal_code': '95211', 'country': 'US'},
                      ip='192.0.2.151', device='44444444-dddd-4ddd-8ddd-444444444444')
t.ok(ROWS['bob']['trial_outcome'] == 'denied:card' and MODIFIED == [('sub_bob', {'trial_end': 'now', 'proration_behavior': 'none'})],
     "Alice's card on a new account -> trial ended at once (billed today)")
t.ok(ROWS['bob']['subscription_trial_end'] is None and ROWS['bob']['trial_used_at'], '...no trial end shown')

del MODIFIED[:]
r, _ = trial_checkout('carol', 'fp_C', addr=A2, ip='192.0.2.152', device='55555555-eeee-4eee-8eee-555555555555')
t.ok(ROWS['carol']['trial_outcome'] == 'denied:address' and MODIFIED,
     "a new card but Alice's billing address (written differently) -> ended")

del MODIFIED[:]
r, _ = trial_checkout('dave', 'fp_D', funding='prepaid', addr={'line1': '1 Oak Ave', 'country': 'US'},
                      ip='192.0.2.153', device='66666666-ffff-4fff-8fff-666666666666')
t.ok(ROWS['dave']['trial_outcome'] == 'denied:prepaid' and MODIFIED, 'a prepaid card -> no trial')

del MODIFIED[:]
r, _ = trial_checkout('erin', False, ip='192.0.2.154', device='77777777-aaaa-4aaa-8aaa-777777777777')
t.ok(ROWS['erin']['trial_outcome'] == 'denied:no_card' and MODIFIED, 'no card on the subscription -> no trial')

# Race: two accounts were OFFERED trials before either finished (same device),
# with different cards and addresses. The second to finish loses its trial.
del MODIFIED[:]
account('race1', email='race1@example.com')
_r, kw1 = checkout('race1', ip='192.0.2.160', device='88888888-bbbb-4bbb-8bbb-888888888888')
account('race2', email='race2@example.com')
_r, kw2 = checkout('race2', ip='192.0.2.161', device='88888888-bbbb-4bbb-8bbb-888888888888')
t.ok(kw1.get('subscription_data') and kw2.get('subscription_data'), 'both were offered (neither had finished)')
complete_via_webhook('race1', kw1, make_sub('sub_race1', 'cus_race1', pm=card('fp_R1', addr={'line1': '2 A St', 'country': 'US'})), 'cs_race1')
complete_via_webhook('race2', kw2, make_sub('sub_race2', 'cus_race2', pm=card('fp_R2', addr={'line1': '3 B St', 'country': 'US'})), 'cs_race2')
t.ok(ROWS['race1']['trial_outcome'] == 'granted' and ROWS['race2']['trial_outcome'] == 'denied:device',
     '...the first keeps it, the second is caught on the device at settle time')

# A trial that cannot be verified must not be granted: the webhook fails and Stripe retries.
account('flaky', email='flaky@example.com')
_r, kwf = checkout('flaky', ip='192.0.2.170', device='99999999-cccc-4ccc-8ccc-999999999999')
make_sub('sub_flaky', 'cus_flaky', pm='pm_missing')        # PaymentMethod.retrieve -> KeyError
n = len(CLAIMS)
r = complete_via_webhook('flaky', kwf, SUBS['sub_flaky'], 'cs_flaky')
t.ok(r.status_code == 500 and len(CLAIMS) == n, 'unverifiable TRIAL -> webhook 500 (Stripe redelivers), nothing recorded')

# A paid, no-trial checkout is never blocked by the guard, and is remembered.
del MODIFIED[:]
account('paid', email='paid@example.com', stripe_customer_id=None, trial_used_at='2026-10-01')
_r, kwp = checkout('paid', ip='192.0.2.180', device='aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa')
make_sub('sub_paid', 'cus_paid', status='active', pm='pm_missing')
r = complete_via_webhook('paid', kwp, SUBS['sub_paid'], 'cs_paid')
t.ok(r.status_code == 200 and ROWS['paid']['plan_tier'] == 'pro' and not MODIFIED,
     'paid checkout whose card cannot be read -> customer still activated')
PMS['pm_paidcard'] = card('fp_PAID', addr={'line1': '7 Pine Rd', 'country': 'US'})
make_sub('sub_paid2', 'cus_paid2', status='active', pm='pm_paidcard')
account('paid2', email='paid2@example.com', trial_used_at='2026-10-01')
_r, kwp2 = checkout('paid2', ip='192.0.2.181', device='bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb')
complete_via_webhook('paid2', kwp2, SUBS['sub_paid2'], 'cs_paid2')
t.eq(ROWS['paid2']['trial_outcome'], 'no_trial', 'a paid checkout is recorded as no_trial')
del MODIFIED[:]
r, _ = trial_checkout('comeback', 'fp_PAID', addr={'line1': '8 Fir Ln', 'country': 'US'},
                      ip='192.0.2.182', device='cccccccc-3333-4333-8333-cccccccccccc')
t.ok(ROWS['comeback']['trial_outcome'] == 'denied:card' and MODIFIED,
     "a former paying customer's card on a new account -> no trial")

r = client.post('/api/complete-checkout', json={'session_id': 'cs_bob'}, headers={'Authorization': 'Bearer bob'})
t.ok('used for a PixelFlip trial before' in (r.get_json().get('trial_message') or ''),
     'the return trip tells a denied user, in plain words, that billing started today')

# ---------------------------------------------------------------------------
t.section('4. Subscription updates: trialing is paid, its end date is shown')


def sub_event(customer, status, trial_end=1900000000, price='price_basic'):
    obj = {'id': 'sub_x', 'customer': customer, 'status': status, 'trial_end': trial_end,
           'current_period_end': 1900000000, 'cancel_at_period_end': False,
           'items': {'data': [{'price': {'id': price}}]}}
    return client.post('/api/webhook', data=json.dumps({'type': 'customer.subscription.updated',
                                                        'data': {'object': obj}}),
                       headers={'Stripe-Signature': 't=1'})


account('t1', stripe_customer_id='cus_t1', stripe_subscription_id='sub_x')
sub_event('cus_t1', 'trialing')
t.ok(ROWS['t1']['subscription_status'] == 'active' and ROWS['t1']['plan_tier'] == 'basic'
     and ROWS['t1']['subscription_trial_end'] == 1900000000, "'trialing' -> entitled, trial end stored")
sub_event('cus_t1', 'active')
t.ok(ROWS['t1']['subscription_trial_end'] is None, 'billing started -> trial end cleared')
sub_event('cus_t1', 'past_due')
t.eq((ROWS['t1']['subscription_status'], ROWS['t1']['plan_tier']), ('inactive', 'inactive'),
     'failed first charge after the trial -> access ends (no grace period, by design)')

t.section('5. Cancel no longer marks a Basic plan as Pro')
account('basic1', plan_tier='basic', is_pro=False, stripe_subscription_id='sub_b1')
SUBS['sub_b1'] = {'id': 'sub_b1', 'status': 'active', 'current_period_end': 1900000000}
r = client.post('/api/cancel-subscription', headers={'Authorization': 'Bearer basic1'})
t.ok(r.status_code == 200 and ROWS['basic1']['subscription_cancel_at_period_end'] is True
     and ROWS['basic1']['is_pro'] is False, 'cancel sets the flag and leaves is_pro alone')

t.section('6. Settings tell the dashboard whether to offer the trial')
t.ok(G.account_eligible({}) and G.account_eligible(None), 'brand-new accounts are eligible')
t.ok(not G.account_eligible({'trial_used_at': 'x'}) and not G.account_eligible({'stripe_customer_id': 'cus'}),
     'used-trial and former-customer accounts are not')

t.done()
