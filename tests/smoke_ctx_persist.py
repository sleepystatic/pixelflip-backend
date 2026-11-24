# -*- coding: utf-8 -*-
"""
Mercari token store (scraper_state, migration 013), rebuilt 2026-11-20
(original: 23 assertions, lost — and it ran against the REAL database with a
test key; this one uses an in-memory table and never connects anywhere).

The captured bearer is a 7-day JWT kept in Postgres so one capture serves every
instance, and so a box that cannot run Chrome can scrape Mercari all week. Pins:
the token's own `exp` is the gate (the 6-hour age cap applies ONLY when exp is
unreadable — it once turned a 7-day token into a 6-hour one); the renew margin;
structural validation; the off switch; and the state reported to the console.
"""
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import scraper_multi_user as S

t = Suite('ctx-persist')
TABLE = {}                     # key -> stored value (dict, or str to mimic a text column)
SQL = []


class Cur(object):
    def __init__(self):
        self.res = None

    def execute(self, sql, args=None):
        s = ' '.join(sql.split())
        SQL.append((s, args))
        if s.startswith('INSERT INTO scraper_state'):
            key, value = args
            TABLE[key] = json.loads(value)
        elif s.startswith('SELECT value FROM scraper_state'):
            self.res = (TABLE[args[0]],) if args[0] in TABLE else None

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


S.get_db_connection = lambda: Conn()
S._emit = lambda *a, **k: None
os.environ['MERCARI_CTX_STATE_KEY'] = 'test_mercari_ctx'
for k in ('MERCARI_PERSIST_CTX', 'MERCARI_CTX_RENEW_MARGIN_SEC', 'MERCARI_CTX_TTL_SEC'):
    os.environ.pop(k, None)


def jwt(exp):
    seg = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).decode().rstrip('=')
    return 'Bearer h.%s.s' % seg


def ctx(exp=None, **over):
    c = {'headers': {'authorization': jwt(exp) if exp else 'Bearer not-a-jwt'},
         'variables': {'q': 'x'}, 'ext': 'persisted-query-hash', 'cookies': {}}
    c.update(over)
    return c


def store(c, age_sec=0):
    TABLE['test_mercari_ctx'] = {'stamp': time.time() - age_sec, 'ctx': c}


DAY = 86400

t.section('1. Writing')
c = ctx(time.time() + 7 * DAY)
S._persist_mercari_ctx(c)
t.ok('test_mercari_ctx' in TABLE and TABLE['test_mercari_ctx']['ctx'] == c, 'stored under the configured key')
t.ok('mercari_api_ctx' not in TABLE, '...so a test never touches the live key')
t.ok(abs(TABLE['test_mercari_ctx']['stamp'] - time.time()) < 5, 'stamped with the capture time')
os.environ['MERCARI_PERSIST_CTX'] = '0'
TABLE.clear()
S._persist_mercari_ctx(c)
t.eq(TABLE, {}, 'MERCARI_PERSIST_CTX=0 -> nothing written')
store(c)
t.eq(S._load_persisted_mercari_ctx(), None, '...and nothing read')
os.environ.pop('MERCARI_PERSIST_CTX')
S._persist_mercari_ctx(None)
t.ok('test_mercari_ctx' in TABLE, 'an empty context never overwrites a stored one')

t.section('2. The JWT exp is the gate')
store(ctx(time.time() + 7 * DAY), age_sec=7 * 3600)
t.ok(S._load_persisted_mercari_ctx() is not None,
     'a 7-day token stored 7 hours ago is still used (the 2026-09-15 bug: it was rejected)')
store(ctx(time.time() + 120))
t.eq(S._load_persisted_mercari_ctx(), None, 'inside the 300s renew margin -> treated as expired (re-capture)')
store(ctx(time.time() - 60))
t.eq(S._load_persisted_mercari_ctx(), None, 'expired -> None')
os.environ['MERCARI_CTX_RENEW_MARGIN_SEC'] = str(int(4.5 * DAY))
store(ctx(time.time() + 5 * DAY))
t.ok(S._load_persisted_mercari_ctx() is not None, "the workflow's 4.5-day margin: 5 days left -> usable")
store(ctx(time.time() + 4 * DAY))
t.eq(S._load_persisted_mercari_ctx(), None, '...4 days left -> renew now')
os.environ.pop('MERCARI_CTX_RENEW_MARGIN_SEC')

t.section('3. Unreadable expiry falls back to the age cap')
store(ctx(None), age_sec=3600)
t.ok(S._load_persisted_mercari_ctx() is not None, 'no readable exp, 1h old -> used')
store(ctx(None), age_sec=7 * 3600)
t.eq(S._load_persisted_mercari_ctx(), None, 'no readable exp, older than 6h -> re-capture')

t.section('4. Structure is validated')
for broken, label in ((ctx(time.time() + DAY, variables={}), 'no variables'),
                      (ctx(time.time() + DAY, ext=''), 'no persisted-query hash'),
                      (dict(ctx(time.time() + DAY), headers={}), 'no headers')):
    store(broken)
    t.eq(S._load_persisted_mercari_ctx(), None, 'hand-edited row with %s -> ignored' % label)
TABLE['test_mercari_ctx'] = json.dumps({'stamp': time.time(), 'ctx': ctx(time.time() + DAY)})
t.ok(S._load_persisted_mercari_ctx() is not None, 'a text (not jsonb) column is tolerated')
TABLE.clear()
t.eq(S._load_persisted_mercari_ctx(), None, 'no row -> None')
real = S.get_db_connection
S.get_db_connection = lambda: None
t.eq(S._load_persisted_mercari_ctx(), None, 'database down -> None, no exception')
S.get_db_connection = real

t.section('5. What the console is told')
t.eq(S._mercari_token_state(), ('absent', None), 'never minted -> absent')
exp = time.time() + DAY
store(ctx(exp))
state, e = S._mercari_token_state()
t.ok(state == 'ok' and abs(e - exp) < 1, 'valid -> ok, with its expiry')
store(ctx(time.time() - 10))
t.eq(S._mercari_token_state()[0], 'expired', 'expired -> expired (the console says when)')
store(ctx(None))
t.eq(S._mercari_token_state(), ('unreadable', None), 'garbage token -> unreadable')
t.eq(S._mercari_token_expiry({'headers': {'authorization': jwt(1234567890)}}), 1234567890.0, 'exp decoded')
t.eq(S._mercari_token_expiry({}), None, 'no token -> None')

t.done()
