"""
Console logs in Postgres instead of process memory.

WHY
user_logs was a dict in the Flask process. That works only while the scraper
runs in that same process — the moment the worker is its own service, the API
serves an empty Live Console while the worker does the scraping. It is also why
two accidental app.py processes produced "ghost scrapes": each had its own
buffer, and the dashboard read whichever one won the port.

WRITES ARE BATCHED
A row per log line would be a DB round-trip per line — a 500-line scrape at
~44ms each is 22 seconds of pure logging. Lines are queued in memory and a
flusher thread writes them with execute_values roughly once a second, so the
scrape pays almost nothing and the dashboard (polling every 2s) sees them
effectively live.
"""
import atexit
import os
import threading
import time
from collections import deque

DDL = """
CREATE TABLE IF NOT EXISTS scrape_logs (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT   NOT NULL,
    ts         DOUBLE PRECISION NOT NULL,
    message    TEXT   NOT NULL,
    log_type   TEXT   NOT NULL DEFAULT 'info',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Serves the only read query: newest-first for one user, optionally since a ts.
CREATE INDEX IF NOT EXISTS idx_scrape_logs_user_ts ON scrape_logs (user_id, ts DESC);
-- Serves retention cleanup.
CREATE INDEX IF NOT EXISTS idx_scrape_logs_created ON scrape_logs (created_at);
"""

_pending = deque()
_lock = threading.Lock()
_flusher = None
_flusher_lock = threading.Lock()
_ddl_done = False


def _cfg(name, default, cast=float):
    try:
        return cast(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def ensure_table(conn):
    """Idempotent DDL, once per process (matches the db_schema.py convention)."""
    global _ddl_done
    if _ddl_done:
        return
    cur = conn.cursor()
    try:
        cur.execute(DDL)
        conn.commit()
        _ddl_done = True
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


_ts_lock = threading.Lock()
_last_ts = 0.0

# Minimum spacing between two log timestamps. See _monotonic_ts.
_TS_NUDGE = 0.001


def _monotonic_ts():
    """
    A strictly increasing timestamp.

    time.time() is not fine-grained enough for burst logging: a scrape emitting
    30 "New match" lines writes them all within the same instant, and identical
    ts values break two things — ORDER BY ts becomes non-deterministic, so the
    console renders out of order, and the `since` cursor (ts > last_seen) skips
    every line sharing the newest timestamp.

    The nudge is 1ms, not 1µs. Epoch seconds are ~1.79e9, where a float64's
    precision is only ~4.8e-7, so microsecond steps are not reliably distinct
    after a round-trip through the database — measured: timestamps came back
    equal and the cursor returned a duplicate row. A millisecond is thousands of
    ULPs, so it always survives. Worst case a 500-line burst drifts half a
    second, which is invisible in a console and self-corrects the moment real
    time overtakes it.
    """
    global _last_ts
    with _ts_lock:
        now = time.time()
        if now <= _last_ts:
            now = _last_ts + _TS_NUDGE
        _last_ts = now
        return now


def add_log(user_id, message, log_type='info'):
    """Queue a line. Never raises and never blocks the scrape on the database."""
    if not user_id or message is None:
        return
    _pending.append((str(user_id), _monotonic_ts(), str(message)[:4000], log_type or 'info'))
    _start_flusher()


def flush(force=False):
    """Write queued lines. Returns how many were written."""
    if not _pending:
        return 0
    # Drain under a lock so two flushes cannot write the same rows.
    with _lock:
        batch = []
        while _pending and len(batch) < 500:
            try:
                batch.append(_pending.popleft())
            except IndexError:
                break
        if not batch:
            return 0

        from db_pool import get_pooled_connection
        from psycopg2.extras import execute_values
        conn = None
        cur = None
        try:
            conn = get_pooled_connection()
            ensure_table(conn)
            cur = conn.cursor()
            execute_values(
                cur,
                'INSERT INTO scrape_logs (user_id, ts, message, log_type) VALUES %s',
                batch, page_size=500,
            )
            conn.commit()
            return len(batch)
        except Exception as e:
            # Put them back so a transient DB blip does not lose the console —
            # appendleft in reverse keeps original order.
            for row in reversed(batch):
                _pending.appendleft(row)
            print(f"[scrape_logs] flush failed, {len(batch)} lines requeued: {e}", flush=True)
            return 0
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
            if conn is not None:
                conn.close()


def _flush_loop():
    interval = _cfg('LOG_FLUSH_INTERVAL_SEC', 1.0)
    while True:
        time.sleep(interval)
        try:
            flush()
        except Exception:
            pass


def _start_flusher():
    global _flusher
    if _flusher is not None:
        return
    with _flusher_lock:
        if _flusher is not None:
            return
        _flusher = threading.Thread(target=_flush_loop, daemon=True, name='scrape-log-flusher')
        _flusher.start()
        atexit.register(flush, True)


def get_logs(user_id, since_ts=None, limit=None):
    """
    Lines for one user, oldest-first (the order the console renders).

    `since_ts` returns only newer lines so /api/status can ship a delta rather
    than the whole buffer on every 2s poll.
    """
    limit = int(limit or _cfg('MAX_USER_LOGS', 500, int))
    from db_pool import get_pooled_connection
    conn = None
    cur = None
    try:
        conn = get_pooled_connection()
        ensure_table(conn)
        cur = conn.cursor()
        # `id` breaks ties. Two lines written in the same microsecond would
        # otherwise come back in arbitrary order, and the console would render
        # a scrape's output shuffled.
        if since_ts is not None:
            # Half a nudge of slack. A float64 round-trip through the database
            # lands a hair below the stored value, so a bare `ts > since`
            # re-returned the cursor's own row on every poll — the console would
            # duplicate its last line forever. Rows are guaranteed at least
            # _TS_NUDGE apart, so half of it excludes the cursor row and cannot
            # skip the next one.
            cur.execute(
                'SELECT ts, message, log_type FROM scrape_logs '
                'WHERE user_id = %s AND ts > %s ORDER BY ts ASC, id ASC LIMIT %s',
                (str(user_id), float(since_ts) + (_TS_NUDGE / 2), limit),
            )
            rows = cur.fetchall()
        else:
            # Newest N, then flip: a user with thousands of lines should get the
            # most recent ones, not the oldest.
            cur.execute(
                'SELECT ts, message, log_type FROM scrape_logs '
                'WHERE user_id = %s ORDER BY ts DESC, id DESC LIMIT %s',
                (str(user_id), limit),
            )
            rows = list(reversed(cur.fetchall()))
        out = []
        for ts, message, log_type in rows:
            out.append({
                'ts': float(ts),
                'time': time.strftime('%I:%M:%S %p UTC', time.gmtime(float(ts))),
                'message': message,
                'type': log_type,
            })
        return out
    except Exception as e:
        print(f"[scrape_logs] read failed: {e}", flush=True)
        return []
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            conn.close()


def purge_old(hours=None):
    """Retention. Called from the existing cleanup loop."""
    hours = int(hours or _cfg('LOG_RETENTION_HOURS', 48, int))
    from db_pool import get_pooled_connection
    conn = None
    cur = None
    try:
        conn = get_pooled_connection()
        ensure_table(conn)
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM scrape_logs WHERE created_at < NOW() - (%s * INTERVAL '1 hour')",
            (hours,),
        )
        deleted = cur.rowcount
        conn.commit()
        return deleted
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        print(f"[scrape_logs] purge failed: {e}", flush=True)
        return 0
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            conn.close()
