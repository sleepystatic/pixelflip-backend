"""
Shared Postgres connection pool.

WHY THIS EXISTS
Every helper used to call psycopg2.connect() and close it again. Connecting to
Supabase costs ~287ms of TCP+TLS against ~44ms for the query itself, and once
users are scraped in parallel that pattern also exhausts the pooler: Supabase's
session mode caps at 15 clients, and even transaction mode has limits.

Pooling fixes both — connections are established once and reused, so the
handshake disappears and the client count stays bounded by DB_POOL_MAX rather
than by how many things happen to be running.

USAGE IS UNCHANGED
get_pooled_connection() hands back an object that behaves like a psycopg2
connection, and .close() returns it to the pool instead of dropping it. Every
existing `conn = get_db_connection() ... conn.close()` call site keeps working,
including the try/finally ones.
"""
import os
import threading
from urllib.parse import urlparse

import psycopg2
from psycopg2 import extensions, pool as pgpool

_pool = None
_pool_lock = threading.Lock()


def _pool_size():
    try:
        return max(1, int(os.getenv('DB_POOL_MAX', '10')))
    except ValueError:
        return 10


def _dsn_kwargs():
    url = os.getenv('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL not set')
    p = urlparse(url)
    return dict(
        host=p.hostname,
        port=p.port or 5432,
        database=(p.path or '/postgres')[1:] or 'postgres',
        user=p.username,
        password=p.password,
        sslmode='require',
        connect_timeout=10,
        # Named so a stuck query is identifiable in pg_stat_activity rather than
        # showing up as an anonymous connection nobody can attribute.
        application_name=os.getenv('DB_APP_NAME', 'pixelflip'),
    )


def get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                size = _pool_size()
                _pool = pgpool.ThreadedConnectionPool(1, size, **_dsn_kwargs())
                print(f"[db] connection pool ready (max {size})", flush=True)
    return _pool


class PooledConnection:
    """
    Proxies a pooled connection so .close() returns it instead of closing it.

    The rollback in close() is the important part. A connection handed back mid
    -transaction stays 'idle in transaction' forever: it holds locks, occupies a
    pooler slot, and blocks DDL like the ensure_* migrations. We saw exactly that
    state in pg_stat_activity — a connection idle in transaction for 12 minutes.
    """

    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn
        self._returned = False

    def __getattr__(self, name):
        # Only called for attributes this wrapper does not define, so the real
        # connection's cursor/commit/rollback/etc pass straight through.
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def close(self):
        if self._returned:
            return
        self._returned = True
        broken = False
        try:
            if self._conn.closed:
                broken = True
            elif self._conn.get_transaction_status() != extensions.TRANSACTION_STATUS_IDLE:
                # Includes INTRANS and INERROR: both must be cleared or the next
                # borrower inherits a poisoned session.
                self._conn.rollback()
        except Exception:
            broken = True
        try:
            self._pool.putconn(self._conn, close=broken)
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass


def get_pooled_connection():
    """Borrow a connection. Call .close() to return it (try/finally as before)."""
    p = get_pool()
    try:
        raw = p.getconn()
    except pgpool.PoolError as e:
        # Pool exhausted means more concurrent work than DB_POOL_MAX allows —
        # raise the ceiling or lower SCRAPE_MAX_WORKERS; do not silently retry.
        raise RuntimeError(f'DB pool exhausted (max {_pool_size()}): {e}') from e
    # A pooled connection can have been killed server-side while idle.
    if raw.closed:
        try:
            p.putconn(raw, close=True)
        except Exception:
            pass
        raw = p.getconn()
    return PooledConnection(p, raw)


def close_pool():
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
            finally:
                _pool = None
