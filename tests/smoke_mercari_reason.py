# -*- coding: utf-8 -*-
"""
Mercari says WHY it is not running (2026-10-19).

On a host that cannot mint a token (Render: MERCARI_ALLOW_CAPTURE=0) the user
used to see a generic failure, or nothing at all — Mercari just returned 0 rows.
Now the console names the cause: an expired JWT (with its expiry time), a token
never minted, or an unreadable one. Offline: the stored token is faked.
"""
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import scraper_multi_user as S

t = Suite('mercari-reason')


def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')


def stored(exp):
    tok = 'Bearer %s.%s.sig' % (b64({'alg': 'none'}), b64({'exp': exp}))
    return {'stamp': time.time(), 'ctx': {'headers': {'authorization': tok}}}


class Cur(object):
    def __init__(self, row):
        self.row = row

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self.row

    def close(self):
        pass


class Conn(object):
    def __init__(self, row):
        self.row = row

    def cursor(self, *a, **k):
        return Cur(self.row)

    def close(self):
        pass


t.section('1. The stored token state is read precisely')
exp = time.time() - 3600
S.get_db_connection = lambda: Conn((stored(exp),))
t.eq(S._mercari_token_state()[0], 'expired', 'a JWT past its exp is EXPIRED')
S.get_db_connection = lambda: Conn((stored(time.time() + 86400),))
t.eq(S._mercari_token_state()[0], 'ok', 'a JWT with a day left is OK')
S.get_db_connection = lambda: Conn(None)
t.eq(S._mercari_token_state(), ('absent', None), 'no stored row is ABSENT')
S.get_db_connection = lambda: Conn(({'ctx': {'headers': {'authorization': 'garbage'}}},))
t.eq(S._mercari_token_state(), ('unreadable', None), 'a non-JWT is UNREADABLE')

t.section('2. The reason the user sees')
S.get_db_connection = lambda: Conn((stored(exp),))
msg = S._mercari_unavailable_reason()
t.ok('expired' in msg and 'JWT' in msg, 'expired: says the JWT expired (%r)' % msg[:70])
t.ok(datetime.fromtimestamp(exp, timezone.utc).strftime('%Y-%m-%d %H:%M') in msg,
     '...and when')
S.get_db_connection = lambda: Conn(None)
t.ok('no access token' in S._mercari_unavailable_reason(), 'absent: says none was minted')
S.get_db_connection = lambda: Conn(({'ctx': {'headers': {'authorization': 'garbage'}}},))
t.ok('unreadable' in S._mercari_unavailable_reason(), 'unreadable: says so')

t.section('3. A host that cannot mint returns that reason as the API error')
os.environ['MERCARI_ALLOW_CAPTURE'] = '0'
S.get_db_connection = lambda: Conn((stored(exp),))
S._load_persisted_mercari_ctx = lambda: None
S._MERCARI_API_CTX = None
_, err = S._mercari_fetch_via_api(['ds lite'], 5)
t.ok(err and 'expired' in err and 'JWT' in err, 'the error carries the JWT-expired reason')
os.environ.pop('MERCARI_ALLOW_CAPTURE', None)

t.done()
