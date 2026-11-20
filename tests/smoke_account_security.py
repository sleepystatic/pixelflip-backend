# -*- coding: utf-8 -*-
"""
Account-change hardening (2026-11-12), through the REAL Flask endpoints.

No database, no Supabase, no Mailgun: an in-memory email_change_requests table
and stubbed HTTP. Pins:

  * /api/update-password checks the CURRENT password before changing anything
    (the dashboard always sent it; nothing used to verify it), and a run of
    wrong guesses locks the user out of that endpoint for a while;
  * /api/request-email-change: 60s resend throttle, format check;
  * /api/update-email: 3 wrong codes kill the code and lock, same as password
    reset — and a successful change emails the PREVIOUS address;
  * the unauthenticated reset endpoints never echo exception text.
"""
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import app as A

t = Suite('account-security')
STORE = {}          # user_id -> email_change_requests row
SENT = []           # (kind, to)
CALLS = []          # outbound HTTP, as (method, url)
SECRET_LEAK = 'db-host-should-never-reach-the-browser.internal'


def now():
    return datetime.now(timezone.utc)


class Cur(object):
    def __init__(self):
        self.res = None

    def execute(self, sql, args=None):
        s = ' '.join(sql.split())
        args = args or ()
        if s.startswith('CREATE TABLE') or s.startswith('ALTER TABLE'):
            return
        if 'too_soon' in s and 'email_change_requests' in s:
            throttle, uid = args
            row = STORE.get(uid)
            self.res = None if not row else (
                bool(row['locked_until'] and row['locked_until'] > now()),
                row['created_at'] > now() - timedelta(seconds=throttle))
        elif s.startswith('INSERT INTO email_change_requests'):
            uid, new_email, code_hash, expires_at = args
            STORE[uid] = {'new_email': new_email, 'code_hash': code_hash,
                          'expires_at': expires_at, 'created_at': now(),
                          'attempts': 0, 'locked_until': None}
        elif s.startswith('SELECT new_email, code_hash'):
            row = STORE.get(args[0])
            if not row:
                self.res = None
            else:
                exp = row['expires_at']
                exp = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
                self.res = dict(row, is_valid=exp > now(),
                                is_locked=bool(row['locked_until'] and row['locked_until'] > now()))
        elif s.startswith('UPDATE email_change_requests') and 'locked_until = NOW()' in s:
            attempts, lock_min, uid = args
            STORE[uid].update(attempts=attempts, expires_at=now(),
                              locked_until=now() + timedelta(minutes=lock_min))
        elif s.startswith('UPDATE email_change_requests SET attempts'):
            STORE[args[1]]['attempts'] = args[0]
        elif s.startswith('DELETE FROM email_change_requests'):
            STORE.pop(args[0], None)
        elif 'password_reset_requests' in s:
            # Reset endpoints: blow up with a message that must NOT reach the client.
            raise RuntimeError('could not connect to server at ' + SECRET_LEAK)
        else:
            raise AssertionError('unexpected SQL: ' + s[:100])

    def fetchone(self):
        return self.res

    def close(self):
        pass


class Conn(object):
    def cursor(self, cursor_factory=None):
        return Cur()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class Resp(object):
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = str(self._body)

    def json(self):
        return self._body


# --- fake Supabase -----------------------------------------------------------
ACCOUNT = {'email': 'owner@test.invalid', 'password': 'correct-horse'}


def fake_get(url, headers=None, timeout=None, **k):
    CALLS.append(('GET', url))
    if url.endswith('/auth/v1/user'):
        return Resp(200, {'id': 'u1', 'email': ACCOUNT['email']})
    raise AssertionError('unexpected GET ' + url)


def fake_post(url, headers=None, json=None, timeout=None, **k):
    CALLS.append(('POST', url))
    if 'grant_type=password' in url:
        if json.get('email') == ACCOUNT['email'] and json.get('password') == ACCOUNT['password']:
            return Resp(200, {'access_token': 'probe-token'})
        return Resp(400, {'error_code': 'invalid_credentials', 'msg': 'Invalid login credentials'})
    if '/auth/v1/logout' in url:
        return Resp(204)
    raise AssertionError('unexpected POST ' + url)


def fake_put(url, headers=None, json=None, timeout=None, **k):
    CALLS.append(('PUT', url))
    if 'password' in (json or {}):
        ACCOUNT['password'] = json['password']
    if 'email' in (json or {}):
        ACCOUNT['email'] = json['email']
    return Resp(200, {})


A.requests.get = fake_get
A.requests.post = fake_post
A.requests.put = fake_put
A.get_db_connection = lambda: Conn()
A._auth_cache_get = lambda token: token          # Bearer <user_id> signs in as user_id
A._send_email_change_code_email = lambda to, code: SENT.append(('code', to))
A._send_email_changed_notice = lambda old, new: SENT.append(('notice', old))
client = A.app.test_client()
H = {'Authorization': 'Bearer u1'}


def change_pw(old, new='brand-new-pass-1'):
    return client.post('/api/update-password', headers=H,
                       json={'old_password': old, 'new_password': new})


t.section('1. Password change verifies the current password')
r = change_pw('')
t.eq(r.status_code, 400, 'missing current password -> 400')
r = change_pw('correct-horse', new='short')
t.eq(r.status_code, 400, 'new password under 8 chars -> 400')
del CALLS[:]
r = change_pw('wrong-guess')
t.ok(r.status_code == 400 and 'incorrect' in r.get_json()['error'], 'wrong current password -> 400 "incorrect"')
t.ok(not any(m == 'PUT' for m, _ in CALLS), '...and the password is NOT changed')
r = change_pw('correct-horse')
t.ok(r.status_code == 200 and r.get_json().get('success'), 'right current password -> success')
t.eq(ACCOUNT['password'], 'brand-new-pass-1', '...and the new password is set')
t.ok(any('/auth/v1/logout' in u for _, u in CALLS), 'the verification sign-in is revoked afterwards')

t.section('2. Repeated wrong current passwords lock the endpoint')
A._pw_change_clear('u1')
for i in range(A.PW_CHANGE_MAX_ATTEMPTS):
    change_pw('nope-%d' % i)
del CALLS[:]
r = change_pw('brand-new-pass-1', new='another-pass-2')
t.eq(r.status_code, 429, 'after %d misses even the RIGHT password is refused' % A.PW_CHANGE_MAX_ATTEMPTS)
t.ok(not any('grant_type=password' in u for _, u in CALLS), '...without even asking Supabase')
A._pw_change_clear('u1')

t.section('3. Email-change request: format check and 60s throttle')
r = client.post('/api/request-email-change', headers=H, json={'new_email': 'not-an-email'})
t.eq(r.status_code, 400, 'bad format -> 400')
r = client.post('/api/request-email-change', headers=H, json={'new_email': 'new@test.invalid'})
t.ok(r.status_code == 200 and SENT[-1] == ('code', 'new@test.invalid'), 'first request sends a code')
n = len(SENT)
r = client.post('/api/request-email-change', headers=H, json={'new_email': 'other@test.invalid'})
t.eq(r.status_code, 429, 'a second request inside 60s -> 429')
t.eq(len(SENT), n, '...and sends nothing')


def seed(code, email='new@test.invalid'):
    STORE['u1'] = {'new_email': email, 'code_hash': hashlib.sha256(code.encode()).hexdigest(),
                   'expires_at': now() + timedelta(minutes=10),
                   'created_at': now() - timedelta(minutes=5),
                   'attempts': 0, 'locked_until': None}


def verify(code, email='new@test.invalid'):
    return client.post('/api/update-email', headers=H, json={'new_email': email, 'code': code})


t.section('4. Three wrong email codes: the code dies and the account locks')
seed('123456')
r = verify('000000')
t.ok(r.status_code == 400 and '2 attempt' in r.get_json()['error'], 'wrong #1 -> 2 left')
r = verify('000000')
t.ok(r.status_code == 400 and '1 attempt' in r.get_json()['error'], 'wrong #2 -> 1 left')
r = verify('000000')
t.ok(r.status_code == 429 and 'no longer valid' in r.get_json()['error'], 'wrong #3 -> 429, code killed')
r = verify('123456')
t.eq(r.status_code, 429, 'the CORRECT code is refused once locked')
t.eq(ACCOUNT['email'], 'owner@test.invalid', '...and the email never changed')
n = len(SENT)
r = client.post('/api/request-email-change', headers=H, json={'new_email': 'new@test.invalid'})
t.ok(r.status_code == 429 and len(SENT) == n, 'a new code cannot be requested during the lock')
t.eq(STORE['u1']['attempts'], 3, 'and asking again does not reset the count')

t.section('5. Happy path changes the email and tells the OLD address')
seed('654321')
del SENT[:]
r = verify('654321')
t.ok(r.status_code == 200 and r.get_json().get('success'), 'correct code -> success')
t.eq(ACCOUNT['email'], 'new@test.invalid', 'email changed')
t.ok(('notice', 'owner@test.invalid') in SENT, 'notice sent to the PREVIOUS address')
t.ok('u1' not in STORE, 'code consumed')


def boom(old, new):
    raise ValueError('Mailgun send failed (500): down')


A._send_email_changed_notice = boom
ACCOUNT['email'] = 'owner@test.invalid'
seed('111222')
r = verify('111222')
t.ok(r.status_code == 200, 'a failed notice does not fail an already-completed change')

t.section('6. Unauthenticated reset endpoints never echo exception text')
r = client.post('/api/request-password-reset-code', json={'email': 'x@test.invalid'})
body = r.get_data(as_text=True)
t.eq(r.status_code, 500, 'DB failure on reset request -> 500')
t.ok(SECRET_LEAK not in body, '...without the exception text')
r = client.post('/api/reset-password-with-code',
                json={'email': 'x@test.invalid', 'code': '123456', 'new_password': 'longenough1'})
body = r.get_data(as_text=True)
t.ok(r.status_code == 500 and SECRET_LEAK not in body, 'reset-with-code failure is generic too')

t.section('7. Rate limits: token bucket, /api/status, reset-code mailer')
L = A._RateLimiter(3, 1.0)
t.ok(all(L.allow('k', now=100.0)[0] for _ in range(3)), 'a burst up to capacity is allowed')
ok, wait = L.allow('k', now=100.0)
t.ok(not ok and wait >= 1, 'the next one is refused with a Retry-After')
t.ok(L.allow('k', now=101.0)[0], 'one token back after a second')
t.ok(L.allow('other', now=100.0)[0], 'buckets are per key')
t.ok(A._RateLimiter(0, 1.0).allow('k')[0], 'capacity 0 disables the limiter')
B = A._RateLimiter(1, 1.0, max_keys=10)
for i in range(50):
    B.allow('key-%d' % i, now=1.0)
t.ok(len(B._buckets) <= 10, 'bucket table stays bounded')

t.eq((A._status_limiter.capacity, A._status_limiter.refill), (10.0, 2.0),
     '/api/status: burst 10, 2/s per user')
S = A._RateLimiter(10, 2.0)
denied = sum(0 if S.allow('u', now=1000 + i * 0.5)[0] else 1      # 4 tabs = 2 polls/s
             for i in range(240))
t.eq(denied, 0, 'four visible tabs polling every 2s for 2 minutes are never refused')


def no_db():
    raise AssertionError('a rate-limited status poll must not touch the database')


A.get_db_connection = no_db
A._status_limiter._buckets['u-poller'] = [0.0, A.time.monotonic()]
r = client.get('/api/status', headers={'Authorization': 'Bearer u-poller'})
t.ok(r.status_code == 429 and r.headers.get('Retry-After'), 'empty bucket -> 429 + Retry-After, before any DB work')

A.get_db_connection = lambda: Conn()
A._reset_request_limiter._buckets.clear()
codes = [client.post('/api/request-password-reset-code', json={'email': 'spray%d@test.invalid' % i},
                     headers={'X-Forwarded-For': '203.0.113.9'}).status_code for i in range(7)]
t.eq(codes.count(429), 2, 'reset-code requests: 5 per IP, then 429 (got %r)' % codes)
r = client.post('/api/request-password-reset-code', json={'email': 'z@test.invalid'},
                headers={'X-Forwarded-For': '198.51.100.4'})
t.ok(r.status_code != 429, 'a different IP is unaffected')

t.section('8. No 500 response echoes exception text (regression guard)')
import re
src = open(A.__file__, encoding='utf-8').read()
leaks = [m.group(0) for m in re.finditer(r'return jsonify\([^\n]*str\(e\)[^\n]*\),\s*5\d\d', src)]
t.eq(leaks, [], 'every 5xx goes through _server_error (logs the detail, returns generic text)')
validation = re.findall(r'except ValueError as e:\s*\n\s*return jsonify\(\{"error": str\(e\)\}\), 400', src)
t.ok(len(validation) >= 4, 'ValueError validation messages (400) are still shown to the user')

t.done()
