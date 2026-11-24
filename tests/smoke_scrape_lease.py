# -*- coding: utf-8 -*-
"""
Cross-instance scrape leases (migration 016).

The one property that matters: when several instances race for the same due
user, EXACTLY ONE may win. Everything else is housekeeping around that.

Runs against the real database, because the whole mechanism is a Postgres
guarantee — an in-memory fake would prove nothing about the thing under test.
It touches only `scrape_claimed_until` on one existing account, and always
clears it again.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import scraper_multi_user as S
import psycopg2

t = Suite('scrape-lease')

if not os.getenv('DATABASE_URL'):
    t.skip('DATABASE_URL not set')

# An account that already exists. Only scrape_claimed_until is touched.
cx = psycopg2.connect(os.getenv('DATABASE_URL'), connect_timeout=20)
# Autocommit, or this connection's first SELECT leaves a transaction open
# holding ACCESS SHARE on user_settings — which blocks the ALTER TABLE the
# ensure chain is about to run, and the migration dies on a statement
# timeout caused entirely by the test observing it.
cx.autocommit = True
cur = cx.cursor()
cur.execute("SELECT user_id FROM user_settings ORDER BY user_id LIMIT 1")
row = cur.fetchone()
if not row:
    t.skip('no user_settings rows to test against')
UID = row[0]
cur.close()


def claimed_until():
    c = cx.cursor()
    c.execute("SELECT scrape_claimed_until FROM user_settings WHERE user_id = %s", (UID,))
    v = c.fetchone()[0]
    c.close()
    return v


def force_clear():
    c = cx.cursor()
    c.execute("UPDATE user_settings SET scrape_claimed_until = NULL WHERE user_id = %s", (UID,))
    c.close()


def force_set(seconds_from_now):
    c = cx.cursor()
    c.execute("UPDATE user_settings SET scrape_claimed_until = NOW() + (%s * INTERVAL '1 second') "
              "WHERE user_id = %s", (seconds_from_now, UID))
    c.close()


try:
    t.section('1. The column exists (migration 016 is applied)')
    # The harness sets SCHEMA_ENSURE=0, so this no longer runs the ensure chain
    # against production (it applied migration 018 there early on 2026-11-20).
    # The column has been in production since October; just check it is there.
    t.eq(os.getenv('SCHEMA_ENSURE'), '0', 'test runs never apply migrations to a real database')
    conn = S.get_db_connection()
    conn.close()
    c = cx.cursor()
    c.execute("""SELECT column_name FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='user_settings'
                   AND column_name='scrape_claimed_until'""")
    t.ok(c.fetchone() is not None, 'scrape_claimed_until exists on user_settings')
    c.close()

    t.section('2. Claim, double-claim, release')
    force_clear()
    t.eq(S.claim_user_for_scrape(UID), True, 'an unclaimed user can be claimed')
    t.ok(claimed_until() is not None, 'and the claim is written to the row')

    # THE core property. A second instance must be refused.
    t.eq(S.claim_user_for_scrape(UID), False,
         'a SECOND instance is refused while the claim is live')
    t.eq(S.claim_user_for_scrape(UID), False, 'and stays refused on retry')

    S.release_user_scrape_claim(UID)
    t.ok(claimed_until() is None, 'release clears the claim')
    t.eq(S.claim_user_for_scrape(UID), True, 'and the user can be claimed again')
    S.release_user_scrape_claim(UID)

    t.section('3. An expired lease self-heals (the OOM-killed-worker case)')
    # A worker killed mid-scrape never runs its release. A boolean flag would
    # strand the user forever; a timestamp simply lapses.
    force_set(-5)                      # expired 5 seconds ago
    t.ok(claimed_until() is not None, 'row carries a stale claim')
    t.eq(S.claim_user_for_scrape(UID), True, 'an EXPIRED claim does not block anyone')
    S.release_user_scrape_claim(UID)

    force_set(600)                     # firmly held
    t.eq(S.claim_user_for_scrape(UID), False, 'a live claim still blocks')
    force_clear()

    t.section('4. Concurrency: N instances race, exactly ONE wins')
    # This is the assertion the whole migration exists for. A read-then-write
    # implementation passes every test above and fails this one.
    for attempt in range(3):
        force_clear()
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def racer():
            barrier.wait()             # maximise the overlap
            got = S.claim_user_for_scrape(UID)
            with lock:
                results.append(got)

        threads = [threading.Thread(target=racer) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60)
        wins = sum(1 for r in results if r)
        t.eq(len(results), 8, 'round %d: all 8 instances answered' % (attempt + 1))
        t.eq(wins, 1, 'round %d: EXACTLY ONE instance won the claim' % (attempt + 1))
    force_clear()

    t.section('5. Fails open rather than refusing to scrape')
    # A scraper that stops working whenever the lease table is unreachable is
    # worse than one that occasionally double-scrapes — especially since every
    # deployment today runs a single instance.
    orig = S.get_db_connection

    def boom():
        raise RuntimeError('simulated database outage')

    S.get_db_connection = boom
    try:
        t.eq(S.claim_user_for_scrape(UID), True, 'DB unreachable -> claim succeeds (fail open)')
        S.release_user_scrape_claim(UID)       # must not raise
        t.ok(True, 'release survives a DB outage without raising')
    finally:
        S.get_db_connection = orig

    S.get_db_connection = lambda: None
    try:
        t.eq(S.claim_user_for_scrape(UID), True, 'no connection -> claim succeeds')
    finally:
        S.get_db_connection = orig

    t.section('6. Lease duration is sane')
    os.environ.pop('SCRAPE_LEASE_SEC', None)
    t.eq(S.scrape_lease_sec(), 900, 'defaults to 900s')
    # Must exceed the worst observed scrape (247s) or a slow-but-healthy run
    # gets stolen mid-flight and scraped twice.
    t.ok(S.scrape_lease_sec() > 247 * 2, 'comfortably longer than the worst scrape')
    os.environ['SCRAPE_LEASE_SEC'] = '30'
    t.eq(S.scrape_lease_sec(), 60, 'clamped to a 60s floor')
    os.environ['SCRAPE_LEASE_SEC'] = 'nonsense'
    t.eq(S.scrape_lease_sec(), 900, 'garbage falls back to the default')
    os.environ.pop('SCRAPE_LEASE_SEC', None)

finally:
    try:
        force_clear()
        cx.close()
    except Exception:
        pass

t.done()
