# -*- coding: utf-8 -*-
"""
Password-reset hardening (2026-10-19), through the REAL Flask endpoints.

A 6-digit code is only safe with limits around it. This pins the three that
make it safe, using an in-memory stand-in for password_reset_requests (no
database, no email, no Supabase):

  * a second code is not emailed within PW_RESET_EMAIL_THROTTLE_SEC (60s);
  * after PW_RESET_MAX_ATTEMPTS (3) wrong codes the code is dead and the email is
    locked for PW_RESET_LOCK_MINUTES — even the CORRECT code is then refused;
  * throttled and locked requests answer exactly like a normal one, so the
    endpoint does not reveal whether an email exists or is locked.
"""
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import app as A

t = Suite('pwreset')
STORE = {}            # email -> row
SENT = []


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
        if 'too_soon' in s:
            throttle, email = args
            row = STORE.get(email)
            self.res = None if not row else (
                bool(row['locked_until'] and row['locked_until'] > now()),
                row['created_at'] > now() - timedelta(seconds=throttle))
        elif s.startswith('INSERT INTO password_reset_requests'):
            email, code_hash, expires_at = args
            STORE[email] = {'code_hash': code_hash, 'expires_at': expires_at,
                            'created_at': now(), 'attempts': 0, 'locked_until': None}
        elif 'code_hash, attempts' in s:
            row = STORE.get(args[0])
            if not row:
                self.res = None
            else:
                exp = row['expires_at']
                exp = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
                self.res = {'email': args[0], 'code_hash': row['code_hash'],
                            'attempts': row['attempts'], 'is_valid': exp > now(),
                            'is_locked': bool(row['locked_until'] and row['locked_until'] > now())}
        elif 'locked_until = NOW()' in s:
            attempts, lock_min, email = args
            STORE[email].update(attempts=attempts, expires_at=now(),
                                locked_until=now() + timedelta(minutes=lock_min))
        elif s.startswith('UPDATE password_reset_requests SET attempts'):
            STORE[args[1]]['attempts'] = args[0]
        elif s.startswith('DELETE FROM password_reset_requests'):
            STORE.pop(args[0], None)
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


A.get_db_connection = lambda: Conn()
A._send_password_reset_code_email = lambda email, code: SENT.append(email)
client = A.app.test_client()


def request_code(email):
    return client.post('/api/request-password-reset-code', json={'email': email})


def submit(email, code):
    return client.post('/api/reset-password-with-code',
                       json={'email': email, 'code': code, 'new_password': 'newpassword123'})


def seed(email, code):
    STORE[email] = {'code_hash': hashlib.sha256(code.encode()).hexdigest(),
                    'expires_at': now() + timedelta(minutes=10), 'created_at': now(),
                    'attempts': 0, 'locked_until': None}


t.section('1. Email throttle')
r = request_code('a@test.invalid')
t.ok(r.status_code == 200 and r.get_json().get('success'), 'first request succeeds')
t.eq(len(SENT), 1, 'and emails exactly one code')
r = request_code('a@test.invalid')
t.ok(r.get_json().get('success'), 'a second request inside 60s answers the same way')
t.eq(len(SENT), 1, '...but sends NO second email')

t.section('2. Three wrong codes: the code dies and the email locks')
seed('b@test.invalid', '111111')
r = submit('b@test.invalid', '000000')
t.ok(r.status_code == 400 and '2 attempt' in r.get_json()['error'], 'wrong #1 -> 2 attempts left')
r = submit('b@test.invalid', '000000')
t.ok(r.status_code == 400 and '1 attempt' in r.get_json()['error'], 'wrong #2 -> 1 attempt left')
r = submit('b@test.invalid', '000000')
t.ok(r.status_code == 429 and 'no longer valid' in r.get_json()['error'], 'wrong #3 -> 429, code invalidated')
t.ok(STORE['b@test.invalid']['locked_until'] is not None, 'the email is now locked')
r = submit('b@test.invalid', '111111')
t.ok(r.status_code == 429, 'the CORRECT code is refused once locked')

t.section('3. A request during the lock neither sends nor resets it')
del SENT[:]
r = request_code('b@test.invalid')
t.ok(r.get_json().get('success') and not SENT, 'locked: same answer, no email')
t.eq(STORE['b@test.invalid']['attempts'], 3, 'attempt count is not reset by asking again')

t.section('4. The happy path still works')
A._admin_lookup_supabase_user_by_email = lambda email: {'id': 'uid-1'}


class _Ok(object):
    status_code = 200

    def json(self):
        return {}


A.requests.put = lambda *a, **k: _Ok()
seed('c@test.invalid', '222222')
r = submit('c@test.invalid', '222222')
t.ok(r.status_code == 200 and r.get_json().get('success'), 'correct code within the limit resets')
t.ok('c@test.invalid' not in STORE, 'and the code is consumed')

t.section('5. The limits are the ones intended')
t.eq((A.PW_RESET_EMAIL_THROTTLE_SEC, A.PW_RESET_MAX_ATTEMPTS, A.PW_RESET_LOCK_MINUTES),
     (60, 3, 15), '60s email throttle, 3 attempts, 15-minute lock')

t.done()
