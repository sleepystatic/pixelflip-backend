import time
import tempfile
import shutil
import uuid
import traceback
import os
import sys
import re
import json
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import psycopg2
from psycopg2 import errorcodes
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse, quote, quote_plus, urlencode, unquote, parse_qs
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

print("Multi-user scraper loaded", flush=True)


def _startup_environment_check():
    """
    Fail loudly at import time if the browser deps are missing.

    Without this the failure is silent and deeply misleading: every browser
    scraper returns an empty string, the dashboard reports "0 rows", and it
    looks like the sites are blocking us rather than the process simply
    lacking Playwright. This is what happens when the backend is started with
    a system Python instead of the project venv.
    """
    _sys = sys
    problems = []
    try:
        import playwright  # noqa: F401
    except ImportError:
        problems.append('playwright (Facebook, OfferUp, Mercari fallback)')
    try:
        import patchright  # noqa: F401
    except ImportError:
        problems.append('patchright (Mercari)')

    if problems:
        print('=' * 78, flush=True)
        print('!! SCRAPER DEPENDENCIES MISSING — browser scrapers WILL return 0 results', flush=True)
        print(f'!! python: {_sys.executable}', flush=True)
        for p in problems:
            print(f'!!   missing: {p}', flush=True)
        print('!! Start the backend with the project venv instead:', flush=True)
        print('!!   .venv\\Scripts\\python.exe app.py', flush=True)
        print('=' * 78, flush=True)
    return not problems


_startup_environment_check()

# Users currently in scrape_for_user (same process as Flask when
# ENABLE_SCRAPER_THREAD=1). Maps user_id -> start time so a scrape that hangs
# can't pin the dashboard in "SCRAPING..." forever: entries older than
# SCRAPE_STALE_SECONDS are ignored. `user_id in SCRAPING_USERS` still works
# because this is a dict.
SCRAPING_USERS = {}
_SCRAPING_LOCK = threading.Lock()

try:
    SCRAPE_STALE_SECONDS = int(os.getenv('SCRAPE_STALE_SECONDS', '1800'))
except ValueError:
    SCRAPE_STALE_SECONDS = 1800

# Browser/network failures happen deep inside the fetch helpers, which have no
# log_callback of their own. Printing them only reaches the server terminal, so
# a user watching the dashboard sees "0 rows" with no reason. Stash the active
# callback per-thread (each user's scrape runs on its own thread) so low-level
# errors reach the dashboard too.
_LOG_STATE = threading.local()


def _set_active_log(user_id, log_callback):
    _LOG_STATE.user_id = user_id
    _LOG_STATE.cb = log_callback


def _emit(message, level='error'):
    """Print to the server log AND surface to the dashboard when possible."""
    print(message, flush=True)
    cb = getattr(_LOG_STATE, 'cb', None)
    uid = getattr(_LOG_STATE, 'user_id', None)
    if cb and uid is not None:
        try:
            cb(uid, message, level)
        except Exception:
            pass


def set_user_scraping(user_id, active):
    with _SCRAPING_LOCK:
        if active:
            SCRAPING_USERS[user_id] = time.time()
        else:
            SCRAPING_USERS.pop(user_id, None)


def is_user_scraping(user_id):
    """
    True only while a scrape is genuinely in flight.

    A hung run (a browser that never returns, a killed thread) used to leave
    the flag set permanently, which reads as "stuck on and won't stop" in the
    UI and makes the scheduler skip that user on every later cycle. Treat an
    entry older than SCRAPE_STALE_SECONDS as dead and clear it.
    """
    with _SCRAPING_LOCK:
        started = SCRAPING_USERS.get(user_id)
        if started is None:
            return False
        if time.time() - started > SCRAPE_STALE_SECONDS:
            SCRAPING_USERS.pop(user_id, None)
            print(f"[scrape] clearing stale in-progress flag for {user_id}", flush=True)
            return False
        return True


_ACTIVE_CACHE = {}          # user_id -> (checked_at, is_active)
try:
    _ACTIVE_CACHE_TTL = float(os.getenv('ACTIVE_CHECK_TTL_SEC', '3'))
except ValueError:
    _ACTIVE_CACHE_TTL = 3.0


def _invalidate_active_cache(user_id=None):
    """Call after anything that flips is_active so STOP still feels immediate."""
    if user_id is None:
        _ACTIVE_CACHE.clear()
    else:
        _ACTIVE_CACHE.pop(user_id, None)


def is_user_active(user_id, force=False):
    """
    Live check used for mid-scrape abort support.

    Cached for a few seconds because this is called once PER LISTING in the
    result loop, and each uncached call opens a fresh TLS connection to Supabase
    — measured at ~350ms, so a 295-listing scrape spent ~103 seconds doing
    nothing but asking "are we still on?".

    A few seconds of staleness is harmless here: the worst case is that a scrape
    processes a couple more listings after STOP before noticing, and every
    platform boundary passes force=True so the big waits are never stale.
    """
    now = time.time()
    if not force:
        hit = _ACTIVE_CACHE.get(user_id)
        if hit and (now - hit[0]) < _ACTIVE_CACHE_TTL:
            return hit[1]

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT is_active FROM user_settings WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        active = True if row is None else bool(row[0])
        _ACTIVE_CACHE[user_id] = (now, active)
        return active
    except Exception:
        # Fail open, and cache it: a DB blip must not abort a running scrape,
        # and must not make every subsequent check retry the dead connection.
        _ACTIVE_CACHE[user_id] = (now, True)
        return True
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ===========================
# DATABASE CONNECTION
# ===========================
def get_db_connection():
    """
    Get a database connection, retrying briefly on transient failures.

    Without this, a momentary DNS or network blip reaching Supabase raises on
    the scrape's very first query. The scrape then dies in milliseconds, and
    because the finish stamp is written in a finally block, the user's next-scan
    countdown starts as though a scan had happened — they silently lose an
    interval to a hiccup. A couple of short retries makes that survivable.
    """
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        raise Exception("DATABASE_URL not set")

    try:
        attempts = int(os.getenv('DB_CONNECT_RETRIES', '3'))
    except ValueError:
        attempts = 3

    # Borrowed from the shared pool rather than dialled fresh: the handshake to
    # Supabase costs ~287ms against ~44ms for the query, and once users are
    # scraped in parallel a connection-per-call also blows the pooler's client
    # limit. conn.close() returns it to the pool, so callers are unchanged.
    from db_pool import get_pooled_connection

    last_err = None
    conn = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            conn = get_pooled_connection()
            break
        except (psycopg2.OperationalError, RuntimeError) as e:
            last_err = e
            if attempt >= attempts:
                break
            wait = 2 ** (attempt - 1)  # 1s, 2s
            print(f"[db] connect failed ({str(e).strip()[:90]}) — retry {attempt}/{attempts - 1} in {wait}s",
                  flush=True)
            time.sleep(wait)
    if conn is None:
        raise last_err if last_err else Exception('Database connection failed')
    try:
        from db_schema import (ensure_buyer_delivery_columns, ensure_listing_uniqueness_per_user,
                               ensure_priority_term_columns, ensure_term_interval_column)
        ensure_buyer_delivery_columns(conn)
        # save_listing lives in this module and targets ON CONFLICT (user_id, link),
        # so the matching index has to be guaranteed on the scraper's own
        # connection — test_scraper.py never goes through app.py's.
        ensure_listing_uniqueness_per_user(conn)
        # The scheduler reads is_priority / last_scraped_at on every cycle, and
        # the worker may reach the database before app.py ever does.
        ensure_priority_term_columns(conn)
        # Same reason, for interval_minutes (migration 012). The master cycle
        # selects it on the FIRST cycle, seconds after start-up — long before
        # any HTTP request has caused app.py to run its own ensure chain. Miss
        # this and every cycle dies on UndefinedColumn, which surfaces as a
        # scanner that says "armed" and then never scrapes anything.
        ensure_term_interval_column(conn)
    except Exception as schema_err:
        print(f"Schema ensure (buyer prefs) warning: {schema_err}", flush=True)
    return conn


# ===========================
# USER DATA FUNCTIONS
# ===========================
def get_active_users():
    """
    Get all users with active scraping enabled.

    try/finally is load-bearing, not style: without it a query that raises
    leaks the connection, and Supabase's session-mode pooler allows only 15.
    Fifteen leaks and every request 500s with 'max clients reached'.
    """
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute('''
            SELECT user_id, zip_code, search_radius, platforms,
                   ai_enabled, ai_strictness, check_interval_minutes,
                   plan_tier, is_pro,
                   buyer_include_local, buyer_include_shipping
            FROM user_settings
            WHERE is_active = TRUE
        ''')

        users = []
        for row in cursor.fetchall():
            tier = (row.get('plan_tier') or '').strip().lower()
            if tier not in ('basic', 'pro'):
                tier = 'pro' if row.get('is_pro') else 'inactive'
            # Pro-only AI image pipeline (Basic never runs Vision, regardless of DB flag)
            eff_ai = bool(row.get('ai_enabled')) and tier == 'pro'

            users.append({
                'user_id': row['user_id'],
                'zip_code': row['zip_code'] or '95212',
                'search_radius': row['search_radius'] or 25,
                'platforms': _coerce_platforms_dict(row.get('platforms')),
                'buyer_include_local': bool(row.get('buyer_include_local', True)),
                'buyer_include_shipping': bool(row.get('buyer_include_shipping', True)),
                'ai_enabled': eff_ai,
                'ai_strictness': row['ai_strictness'] or 'balanced',
                'check_interval': row['check_interval_minutes'] or 10,
                'plan_tier': tier,
            })
        return users
    finally:
        cursor.close()
        conn.close()


def get_user_search_terms(user_id):
    """
    Search terms with their price bounds and their own exclusion keywords.

    A NULL bound means "any price" and must stay None — coercing it to 0.0
    would turn an unbounded search into one that matches nothing.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT search_term, min_price, max_price, is_priority, '
                       'interval_minutes, '
                       'EXTRACT(EPOCH FROM last_scraped_at) '
                       'FROM user_search_terms WHERE user_id = %s', (user_id,))
        terms = {
            row[0]: {
                'min': float(row[1]) if row[1] is not None else None,
                'max': float(row[2]) if row[2] is not None else None,
                'exclusions': [],
                # is_priority is derived from the interval now and kept only so
                # a rollback still finds the column populated.
                'is_priority': bool(row[3]),
                'interval_minutes': (int(row[4]) if row[4] is not None
                                     else DEFAULT_TERM_INTERVAL_MINUTES),
                'last_scraped_ts': float(row[5]) if row[5] is not None else 0.0,
            }
            for row in cursor.fetchall()
        }

        cursor.execute(
            'SELECT keyword, search_term FROM user_exclusions WHERE user_id = %s AND search_term IS NOT NULL',
            (user_id,),
        )
        for keyword, term in cursor.fetchall():
            if term in terms:
                terms[term]['exclusions'].append(keyword)
        return terms
    finally:
        cursor.close()
        conn.close()


def get_user_exclusions(user_id):
    """
    Deprecated: exclusions are per search term now and travel inside
    get_user_search_terms(). Kept so the scrape call signature is unchanged.
    """
    return []


def get_seen_listings(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT DISTINCT link FROM listings WHERE user_id = %s', (user_id,))
        return set(row[0] for row in cursor.fetchall())
    finally:
        cursor.close()
        conn.close()


def get_user_notification_prefs(user_id):
    """notification_channels + contact_phone for new-listing alerts (delivery wired separately)."""
    default_ch = {'email': True, 'sms': False, 'push': False}
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute(
                "SELECT notification_channels, contact_phone FROM user_settings WHERE user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()
        if not row:
            return {'notification_channels': default_ch, 'contact_phone': None}
        ch = row.get('notification_channels') or {}
        if isinstance(ch, str):
            try:
                ch = json.loads(ch)
            except Exception:
                ch = {}
        if not isinstance(ch, dict):
            ch = {}
        norm = {
            'email': bool(ch.get('email', True)),
            'sms': bool(ch.get('sms', False)),
            'push': bool(ch.get('push', False)),
        }
        return {'notification_channels': norm, 'contact_phone': row.get('contact_phone')}
    except Exception:
        return {'notification_channels': default_ch, 'contact_phone': None}


def get_user_auth_email(user_id):
    """Supabase auth email for Mailgun alerts (requires DB role to read auth.users)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT email FROM auth.users WHERE id = %s::uuid', (user_id,))
            row = cursor.fetchone()
            return (row[0] or '').strip() if row else None
        finally:
            cursor.close()
            conn.close()
    except Exception:
        return None


def _notify_scrape_digest(user_id, new_listings, prefs, log_callback=None):
    """One email/SMS per scrape with all new matches (avoids inbox flooding)."""
    if not new_listings:
        return
    from listing_notifications import dispatch_scrape_digest_notifications

    ch = prefs.get('notification_channels') or {}
    if not any(ch.get(k) for k in ('email', 'sms', 'push')):
        return
    email = get_user_auth_email(user_id)
    errs = dispatch_scrape_digest_notifications(user_id, new_listings, prefs, user_email=email)
    if errs:
        line = 'Notify issues: ' + '; '.join(errs)
        print(f"📣 user={str(user_id)[:10]}… {line}", flush=True)
        if log_callback:
            log_callback(user_id, line, 'error')
    elif log_callback:
        n = len(new_listings)
        ch_bits = [k for k in ('email', 'sms') if ch.get(k)]
        if ch_bits:
            log_callback(
                user_id,
                f"Sent digest alert ({n} listing{'s' if n != 1 else ''}) via {', '.join(ch_bits)}.",
                'info',
            )


def _title_fingerprint(title):
    cleaned = re.sub(r'[^a-z0-9\s]', ' ', (title or '').lower())
    tokens = [t for t in cleaned.split() if len(t) > 1 and t not in {'the', 'and', 'for', 'with'}]
    if not tokens:
        return ''
    core = ' '.join(sorted(tokens)[:10])
    return hashlib.sha1(core.encode('utf-8')).hexdigest()[:20]


def get_blocked_links_and_fingerprints(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    blocked_links = set()
    blocked_fingerprints = set()
    try:
        cursor.execute(
            "SELECT link, title_fingerprint FROM listings_feedback WHERE user_id = %s",
            (user_id,)
        )
        for link, fp in cursor.fetchall():
            if link:
                blocked_links.add(link)
            if fp:
                blocked_fingerprints.add(fp)
    except Exception:
        pass
    finally:
        cursor.close()
        conn.close()
    return blocked_links, blocked_fingerprints


def get_recent_listing_signatures(user_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute(
            """
            SELECT COALESCE(title_fingerprint, '') AS title_fingerprint, price
            FROM listings
            WHERE user_id = %s
              AND created_at >= NOW() - INTERVAL '14 days'
            """,
            (user_id,)
        )
        rows = cursor.fetchall()
    except Exception:
        rows = []
    finally:
        cursor.close()
        conn.close()
    return rows


def save_listing(user_id, listing):
    """Persist listing; `console_type` column holds the matched search term (legacy name)."""
    # Bound before the try so the finally can reference them even when
    # get_db_connection() itself raises.
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO listings (user_id, title, price, link, platform, console_type, threshold, image_url, location, title_fingerprint, listed_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id, link) DO NOTHING
            ''', (
            user_id, listing['title'], listing['price'], listing['link'], listing['platform'], listing.get('console_type'),
            listing.get('threshold'), listing.get('image_url'), listing.get('location'), listing.get('title_fingerprint'),
            listing.get('listed_at')))
        except psycopg2.ProgrammingError as e:
            conn.rollback()
            if e.pgcode != errorcodes.UNDEFINED_COLUMN and 'listed_at' not in str(e):
                raise
            cursor.execute('''
                INSERT INTO listings (user_id, title, price, link, platform, console_type, threshold, image_url, location, title_fingerprint, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id, link) DO NOTHING
            ''', (
            user_id, listing['title'], listing['price'], listing['link'], listing['platform'], listing.get('console_type'),
            listing.get('threshold'), listing.get('image_url'), listing.get('location'), listing.get('title_fingerprint')))
        inserted = cursor.rowcount > 0
        conn.commit()
        return inserted
    except Exception as e:
        print(f"❌ Save error: {e}", flush=True)
        return False
    finally:
        # Previously closed only on the success path, so any raise leaked a
        # pooler slot — 15 of those and every request 500s.
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_BULK_COLUMNS = ('user_id', 'title', 'price', 'link', 'platform', 'console_type',
                 'threshold', 'image_url', 'location', 'title_fingerprint', 'listed_at')


def save_listings_bulk(user_id, listings):
    """
    Insert many listings in ONE round trip; return the set of links that were new.

    save_listing() opens a fresh connection per row, and connecting to Supabase
    costs ~287ms of TCP+TLS against ~44ms for the insert itself — so a
    295-listing scrape burned roughly 98 seconds on handshakes alone. One
    execute_values call does the same work in ~160ms.

    RETURNING is what makes this safe to swap in: it reports the rows that
    actually landed, so a link that lost the ON CONFLICT race is correctly not
    treated as a new match and cannot trigger a duplicate alert.
    """
    if not listings:
        return set()
    from psycopg2.extras import execute_values

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        rows = [
            (user_id, l['title'], l['price'], l['link'], l['platform'],
             l.get('console_type'), l.get('threshold'), l.get('image_url'),
             l.get('location'), l.get('title_fingerprint'), l.get('listed_at'))
            for l in listings
        ]
        try:
            inserted = execute_values(
                cursor,
                f"""INSERT INTO listings ({', '.join(_BULK_COLUMNS)}, created_at)
                    VALUES %s
                    ON CONFLICT (user_id, link) DO NOTHING
                    RETURNING link""",
                rows,
                template='(' + ','.join(['%s'] * len(_BULK_COLUMNS)) + ',NOW())',
                page_size=200,
                fetch=True,
            )
        except psycopg2.ProgrammingError as e:
            # Same pre-migration fallback save_listing carries: a DB without the
            # listed_at column still works, just without posted dates.
            conn.rollback()
            if e.pgcode != errorcodes.UNDEFINED_COLUMN and 'listed_at' not in str(e):
                raise
            cursor.close()
            cursor = conn.cursor()
            cols = _BULK_COLUMNS[:-1]
            inserted = execute_values(
                cursor,
                f"""INSERT INTO listings ({', '.join(cols)}, created_at)
                    VALUES %s
                    ON CONFLICT (user_id, link) DO NOTHING
                    RETURNING link""",
                [r[:-1] for r in rows],
                template='(' + ','.join(['%s'] * len(cols)) + ',NOW())',
                page_size=200,
                fetch=True,
            )
        conn.commit()
        return {r[0] for r in (inserted or [])}
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ Bulk save error: {e}", flush=True)
        return set()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ===========================
# UTILITY FUNCTIONS
# ===========================
def extract_price(price_text):
    if not price_text: return None
    match = re.search(r'[\$](\d+(?:,\d{3})*(?:\.\d{2})?)', str(price_text))
    if match: return float(match.group(1).replace(',', ''))
    return None


_SHIP_HINT = re.compile(
    r'\b(shipp(?:ed|ing|s)|ships to(?: you)?|\+\s*\$?\s*\d{1,3}\s*(?:ship|delivery)|delivery available|\busps\b|fedex|parcel locker)\b',
    re.I,
)
_LOCAL_HINT = re.compile(
    r'\b(local pickup|pickup only|meet(?:\s*-?ups?)?|porch|c\.o\.r\.|cash only|meet at|meet\s+@\s|pick\s*-\s*up)\b',
    re.I,
)


# How each marketplace actually delivers. This is far more reliable than
# scanning titles for "pickup only": OfferUp removed shipping entirely,
# Craigslist has never had it, and Mercari local pickup is rare enough to treat
# as shipping-only. Facebook is the one that genuinely does both, so it falls
# through to the text hints below.
PLATFORM_DELIVERY = {
    'craigslist': 'local',
    'offerup': 'local',
    'mercari': 'shipping',
    'facebook': 'both',
}


def platform_delivery_mode(platform_name):
    return PLATFORM_DELIVERY.get((platform_name or '').strip().lower(), 'both')


def platform_matches_buyer_delivery_prefs(platform_name, buyer_include_local=True,
                                          buyer_include_shipping=True):
    """
    Whether a whole platform can satisfy the buyer's delivery preference.

    Used to skip a platform before scraping it — a shipping-only buyer has no
    use for Craigslist results, so fetching them wastes a scrape and proxy
    bandwidth as well as producing alerts they didn't want.
    """
    if buyer_include_local and buyer_include_shipping:
        return True
    mode = platform_delivery_mode(platform_name)
    if mode == 'both':
        return True
    if mode == 'local':
        return bool(buyer_include_local)
    return bool(buyer_include_shipping)


def listing_matches_buyer_delivery_prefs(entry, buyer_include_local=True, buyer_include_shipping=True):
    """
    Per-listing safety net. The platform check above does the real work; this
    only refines platforms that carry both kinds (Facebook), using title and
    location phrases. Ambiguous rows are kept so we never over-filter.
    """
    if buyer_include_local and buyer_include_shipping:
        return True

    platform = str(entry.get('platform') or '')
    mode = platform_delivery_mode(platform)
    if mode == 'local':
        return bool(buyer_include_local)
    if mode == 'shipping':
        return bool(buyer_include_shipping)

    # 'both' — fall back to text hints. NOTE: platform name is deliberately not
    # in the blob; matching on it would be circular now that mode is explicit.
    blob = ' '.join([
        str(entry.get('title') or ''),
        str(entry.get('location') or ''),
    ])
    ships = bool(_SHIP_HINT.search(blob))
    local = bool(_LOCAL_HINT.search(blob))
    if not ships and not local:
        return True
    if buyer_include_local and not buyer_include_shipping:
        return not (ships and not local)
    if buyer_include_shipping and not buyer_include_local:
        return not (local and not ships)
    return True


def _extract_lazy_image_url(img_elem):
    """Craigslist/OfferUp often use data-src or srcset instead of a usable src."""
    if not img_elem:
        return None
    for attr in ('data-src', 'data-original', 'data-lazy-src', 'src'):
        v = img_elem.get(attr) if hasattr(img_elem, 'get') else None
        if v and str(v).strip() and not str(v).startswith('data:'):
            return str(v).strip()
    srcset = (img_elem.get('srcset') or '') if hasattr(img_elem, 'get') else ''
    if srcset:
        part = srcset.split(',')[0].strip().split()
        if part:
            return part[0]
    return None


def _normalize_iso8601_tz(s):
    """Craigslist uses e.g. -0700; Python's fromisoformat expects -07:00."""
    if not s:
        return s
    m = re.search(r'([+-])(\d{2})(\d{2})$', str(s).strip())
    if m:
        return str(s).strip()[: m.start()] + f"{m.group(1)}{m.group(2)}:{m.group(3)}"
    return str(s).strip()


def _parse_source_datetime(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, (int, float)):
        x = float(val)
        if x > 1e12:
            x = x / 1000.0
        if x > 1e9:
            try:
                return datetime.fromtimestamp(x, tz=timezone.utc)
            except Exception:
                return None
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        s = _normalize_iso8601_tz(s.replace('Z', '+00:00'))
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _search_term_matches_title(term, title_lower):
    """Relaxed matching: exact substring, compact (ignore spaces/punctuation), or all words (2+ chars)."""
    tl = (term or '').lower().strip()
    if not tl:
        return False
    if tl in title_lower:
        return True
    compact_term = re.sub(r'[^a-z0-9]', '', tl)
    compact_title = re.sub(r'[^a-z0-9]', '', title_lower)
    if len(compact_term) >= 4 and compact_term in compact_title:
        return True
    words = [w for w in re.split(r'\s+', tl) if w]
    if len(words) >= 2:
        def _word_in_title(w):
            if len(w) <= 3:
                return bool(re.search(r'(?<![a-z0-9])' + re.escape(w) + r'(?![a-z0-9])', title_lower))
            return w in title_lower

        return all(_word_in_title(w) for w in words)
    if len(words) == 1:
        w = words[0]
        if len(w) <= 3:
            return bool(re.search(r'(?<![a-z0-9])' + re.escape(w) + r'(?![a-z0-9])', title_lower))
        return w in title_lower
    return False


def check_price_threshold(title, price, search_terms):
    """
    Match a listing title to a search term and its price bounds.

    A bound of None means "unbounded" — a user tracking an item at any price
    should not have to invent a ceiling. Only bounds that are actually set
    filter anything.
    """
    title_lower = title.lower()
    for term, thresholds in sorted(search_terms.items(), key=lambda x: len(x[0]), reverse=True):
        if not _search_term_matches_title(term, title_lower):
            continue
        lo = thresholds.get('min')
        hi = thresholds.get('max')
        if lo is not None and price < lo:
            continue
        if hi is not None and price > hi:
            continue
        return True, term, hi
    return False, None, None


def is_excluded(title, price, exclusions, search_terms=None, matched_term=None):
    """
    Exclusions belong to a single search term: a keyword attached to one term
    must not filter listings found by another — excluding "case" from a console
    search shouldn't also kill a search for phone cases.

    `exclusions` is the retired global list; it is always empty now and is kept
    only so the call signature stays stable.
    """
    title_lower = title.lower()
    # Optional floor (default: off). The old hard-coded $10 rule dropped $0–$9 deals and "free" posts.
    try:
        min_listing = float(os.getenv('MIN_LISTING_PRICE', '0'))
    except ValueError:
        min_listing = 0.0
    if min_listing > 0 and price < min_listing:
        return True
    for keyword in (exclusions or []):
        if keyword and keyword.lower() in title_lower:
            return True
    if search_terms and matched_term:
        per_term = (search_terms.get(matched_term) or {}).get('exclusions') or []
        for keyword in per_term:
            if keyword and keyword.lower() in title_lower:
                return True
    return False


def _is_recent_timestamp(dt_text, max_age_days):
    if not dt_text:
        return True
    try:
        # Craigslist usually emits ISO-like datetimes
        dt = datetime.fromisoformat(_normalize_iso8601_tz(dt_text.replace('Z', '+00:00')))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - dt
        return age.total_seconds() <= max_age_days * 86400
    except Exception:
        return True


def _parse_mercari_item_posted_text(txt):
    """
    Mercari item detail shows Posted as MM/DD/YY (e.g. 04/30/26).
    Returns a UTC datetime at noon on that calendar day for stable DB ordering.
    """
    if not txt:
        return None
    t = str(txt).strip()
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2})$', t)
    if not m:
        return None
    mo, da, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        return datetime(year, mo, da, 12, 0, 0, tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_flexible_marketplace_date(val):
    """ISO / epoch-ish values plus Mercari-style MM/DD/YY."""
    if val is None or val == '':
        return None
    dt = _parse_source_datetime(val)
    if dt:
        return dt
    if isinstance(val, str):
        return _parse_mercari_item_posted_text(val)
    return None



def _env_flag(name, default=False):
    raw = (os.getenv(name) or '').strip().lower()
    if not raw:
        return default
    return raw in ('1', 'true', 'yes', 'on')



def _mercari_listed_at_from_item_page(link, headers, polite_delay_sec):
    """
    Optional posted-date enrichment. Off by default.
    Enable with MERCARI_FETCH_ITEM_POSTED=true (direct HTTP first; set MERCARI_ITEM_PAGE_PLAYWRIGHT=true for Playwright fallback).
    """
    if not _env_flag('MERCARI_FETCH_ITEM_POSTED', False):
        return None
    if not link or '/item/' not in link:
        return None
    u = link if link.startswith('http') else f'https://www.mercari.com{link}'
    out = None
    try:
        r = requests.get(u, headers=headers, timeout=12)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text or '', 'html.parser')
            el = soup.select_one('[data-testid="ItemDetailsPosted"]')
            if el:
                out = _parse_mercari_item_posted_text(el.get_text(' ', strip=True))
        if out is None and _env_flag('MERCARI_ITEM_PAGE_PLAYWRIGHT', False):
            html = _mercari_fetch_with_playwright(u)
            if html:
                soup = BeautifulSoup(html, 'html.parser')
                el = soup.select_one('[data-testid="ItemDetailsPosted"]')
                if el:
                    out = _parse_mercari_item_posted_text(el.get_text(' ', strip=True))
    except Exception:
        return None
    finally:
        time.sleep(max(0.0, polite_delay_sec))
    return out


# ===========================
# OPTIONAL AI IMAGE FILTER (GOOGLE VISION)
# ===========================
GOOGLE_VISION_API_KEY = os.getenv('GOOGLE_VISION_API_KEY')


def _ai_allowed_for_tier(tier):
    """
    Vision image filtering is a Pro feature.

    app.py forces ai_enabled False for non-Pro when settings are SAVED, but the
    stored flag outlives a downgrade — a user who was Pro keeps a stale
    ai_enabled=True in the row until they touch settings again. Re-check the
    tier at scrape time so entitlement follows the current plan.
    """
    return (tier or '').strip().lower() == 'pro'


def _ai_enabled_for_platform(platform_name):
    """Comma-separated platforms (lowercase) that may use Vision when configured. Empty = none."""
    raw = os.getenv('AI_IMAGE_FILTER_PLATFORMS', '').lower()
    allowed = {p.strip() for p in raw.split(',') if p.strip()}
    return platform_name.lower() in allowed


def _vision_label_substrings_from_env(var_name):
    raw = os.getenv(var_name, '').strip()
    if not raw:
        return []
    return [x.strip().lower() for x in raw.split(',') if x.strip()]


def _vision_blob_matches_substrings(blob_items, substrings):
    """Each substring may match anywhere in any label/object string (Vision returns short phrases)."""
    if not substrings:
        return False
    for item in blob_items:
        for frag in substrings:
            if frag in item:
                return True
    return False


def check_image_with_ai(image_url, ai_enabled, ai_strictness, debug=False, log_callback=None,
                        user_id=None, platform_name='', matched_term=None, term_exclusions=None):
    """
    Vision-based image filter, driven by the user's own search term and
    exclusions rather than a global label list.

    Measured behaviour that shapes the rules below: for a real DS Lite photo
    Vision returned ['Video game console', 'Handheld game console',
    'Nintendo DS', 'Gadget', ...] and a best guess of 'nintendo ds'. The search
    term "ds lite" matched NONE of it.

    So the two directions are not symmetric:
      * exclusions make good NEGATIVES — words like case/cable/box line up with
        Vision's vocabulary, and a hit is strong evidence of a wrong item.
      * search terms make poor POSITIVES — Vision names categories and brands,
        not model variants, so requiring a match would bin correct listings.
        Positives are therefore advisory: they can only reject under 'strict',
        and only when the term shares no token at all with what Vision saw.
    """
    if not _ai_enabled_for_platform(platform_name or ''):
        return True
    if not ai_enabled or not GOOGLE_VISION_API_KEY or not image_url:
        return True

    # Per-term config first; env lists stay as a global fallback.
    negatives = [w.lower() for w in (term_exclusions or []) if w]
    if not negatives:
        negatives = _vision_label_substrings_from_env('AI_IMAGE_NEGATIVE_LABELS')

    positives = []
    if matched_term:
        # Drop 1-2 char tokens ("sp", "ds") — too short to match safely.
        positives = [t for t in re.split(r'\W+', matched_term.lower()) if len(t) > 2]
    if not positives:
        positives = _vision_label_substrings_from_env('AI_IMAGE_POSITIVE_LABELS')

    if not positives and not negatives:
        return True

    try:
        url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"
        # WEB_DETECTION is what surfaces product-ish names ('nintendo ds') that
        # a user's search term can actually match; LABEL/OBJECT alone only give
        # generic categories.
        payload = {"requests": [{"image": {"source": {"imageUri": image_url}},
                                 "features": [{"type": "LABEL_DETECTION", "maxResults": 15},
                                              {"type": "OBJECT_LOCALIZATION", "maxResults": 8},
                                              {"type": "WEB_DETECTION", "maxResults": 10}]}]}
        response = requests.post(url, json=payload, timeout=10)

        if response.status_code != 200:
            return True

        result = response.json()
        if 'responses' not in result or not result['responses']:
            return True

        data = result['responses'][0]
        labels = [label['description'].lower() for label in data.get('labelAnnotations', [])]
        objects = [obj['name'].lower() for obj in data.get('localizedObjectAnnotations', [])]
        web = data.get('webDetection', {}) or {}
        web_terms = [e['description'].lower() for e in web.get('webEntities', []) if e.get('description')]
        web_terms += [b['label'].lower() for b in web.get('bestGuessLabels', []) if b.get('label')]
        all_detected = labels + objects + web_terms

        has_pos = _vision_blob_matches_substrings(all_detected, positives) if positives else True
        has_neg = _vision_blob_matches_substrings(all_detected, negatives) if negatives else False

        if ai_strictness == 'strict':
            passed = (has_pos if positives else True) and not has_neg
        elif ai_strictness == 'balanced':
            passed = (not has_neg) if not positives else (has_pos or not has_neg)
        else:
            passed = not has_neg

        if not passed and log_callback and user_id and all_detected:
            log_callback(user_id, f"AI image filter: labels {all_detected[:5]}", "error")

        return passed
    except Exception:
        return True


# ===========================
# CRAIGSLIST SCRAPER
# ===========================
def _best_craigslist_gallery_image(soup):
    """Listing detail pages include images.craigslist.org URLs; tiles may use lazy attrs instead of src."""
    best = None
    best_area = -1
    for img in soup.find_all('img'):
        src = _extract_lazy_image_url(img)
        if not src or 'images.craigslist.org' not in src:
            continue
        if src.startswith('//'):
            src = 'https:' + src
        m = re.search(r'(\d+)x(\d+)', src)
        area = int(m.group(1)) * int(m.group(2)) if m else 0
        if area > best_area:
            best_area = area
            best = src
    if best:
        return best
    og = soup.find('meta', attrs={'property': 'og:image'})
    if not og:
        og = soup.find('meta', attrs={'name': 'twitter:image'})
    if og and og.get('content'):
        c = (og.get('content') or '').strip()
        if 'images.craigslist.org' in c:
            if c.startswith('//'):
                return 'https:' + c
            return c
    return None


def _craigslist_image_from_data_ids(node):
    """
    Craigslist search tiles often include data-ids like:
    "1:00A0A_f5VJoaEzY8A_0ak07K,2:..."
    Build an image URL even when <img> tags are omitted.
    """
    if not node:
        return None
    raw = (node.get('data-ids') or node.get('data_ids') or '').strip()
    if not raw:
        return None
    first = raw.split(',')[0].strip()
    if ':' in first:
        first = first.split(':', 1)[1]
    first = first.strip()
    if not first:
        return None
    return f"https://images.craigslist.org/{first}_600x450.jpg"


def _is_craigslist_placeholder_price(price):
    """CL search (and some ads) use placeholder prices like $1,234."""
    if price is None:
        return False
    try:
        v = float(price)
    except (TypeError, ValueError):
        return False
    raw = os.getenv('CRAIGSLIST_PLACEHOLDER_PRICES', '1234')
    for part in raw.split(','):
        p = part.strip()
        if not p:
            continue
        try:
            if abs(v - float(p)) < 0.01:
                return True
        except ValueError:
            continue
    return False


def _extract_craigslist_price_from_listing_soup(soup):
    """Real asking price from a listing detail page."""
    for sel in ('span.price', 'h1.price', '.price'):
        el = soup.select_one(sel)
        if el:
            p = extract_price(el.get_text(' ', strip=True))
            if p is not None and not _is_craigslist_placeholder_price(p):
                return p
    return None


_CL_LOCAL = threading.local()


def _craigslist_session():
    """
    One requests.Session per thread.

    Sessions are not thread-safe, and the detail fetches now run in a pool.
    Sharing one across workers corrupts the connection pool under load.
    """
    s = getattr(_CL_LOCAL, 'session', None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        _CL_LOCAL.session = s
    return s


def _craigslist_title_match(title, search_terms):
    """
    The matched term for this title, or None — using ONLY the title.

    Split out of check_price_threshold so it can run BEFORE the detail fetch.
    Measured 2026-08-12 across 659 tiles: 45% fail the title filter, and every
    one of them was costing a page GET plus a polite-delay sleep before being
    discarded. Price bounds still run after enrichment, because 5.5% of tiles
    have no price until the detail page supplies one.
    """
    tl = (title or '').lower()
    if not tl:
        return None
    for term in sorted(search_terms.keys(), key=len, reverse=True):
        if _search_term_matches_title(term, tl):
            return term
    return None


def _enrich_craigslist_from_detail(session, listing, max_age_days, polite_delay_sec):
    """GET the listing detail page for posted time, gallery image, and price when the tile omits it."""
    try:
        r = session.get(listing['link'], timeout=22)
        time.sleep(max(0.0, polite_delay_sec))
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            return True
        soup = BeautifulSoup(r.content, 'html.parser')
        time_el = soup.select_one('time[datetime]')
        if not time_el:
            time_el = soup.find('time', class_='date')
        dt_text = time_el.get('datetime') if time_el else None
        if dt_text:
            if not _is_recent_timestamp(dt_text, max_age_days):
                return False
            listing['listed_at'] = _parse_source_datetime(dt_text)
        detail_price = _extract_craigslist_price_from_listing_soup(soup)
        if detail_price is not None:
            if listing.get('price') is None or _is_craigslist_placeholder_price(listing.get('price')):
                listing['price'] = detail_price
        img = _best_craigslist_gallery_image(soup)
        if img:
            listing['image_url'] = img
        return True
    except Exception:
        return True


def scrape_craigslist_for_user(user_id, zip_code, search_radius, search_terms, exclusions, ai_enabled, ai_strictness,
                               debug=False, log_callback=None, known_links=None):
    listings = []
    # One subdomain is enough: Craigslist's static search is scoped by the
    # postal + search_distance params, NOT by the subdomain. Measured — stockton,
    # sacramento, sfbay and modesto returned byte-identical result sets (27/27
    # overlap), so listing extra sites here just multiplies requests and creates
    # duplicates for the dedup layer to strip.
    sites_env = os.getenv('CRAIGSLIST_SITES', 'stockton')
    subdomains = [s.strip() for s in sites_env.split(',') if s.strip()]
    # `sss` = general for-sale; narrow category codes exclude many real listings — override with CRAIGSLIST_SEARCH_CAT if needed.
    cl_cat = os.getenv('CRAIGSLIST_SEARCH_CAT', 3 * 's').strip() or (3 * 's')
    max_age_days = int(os.getenv('MAX_LISTING_AGE_DAYS', '7'))
    fetch_detail = os.getenv('CRAIGSLIST_FETCH_DETAIL', '1').strip().lower() not in ('0', 'false', 'no')
    # Per-request politeness. Lower than the old 0.35 because the fetches are no
    # longer serial — with a pool of N, the effective rate is delay/N, so keeping
    # 0.35 alongside parallelism would have been a real rate increase. 0.25/6 is
    # gentler per-connection than 0.35 sequential was.
    try:
        detail_delay = float(os.getenv('CRAIGSLIST_DETAIL_DELAY_SEC', '0.25'))
    except ValueError:
        detail_delay = 0.25
    # Craigslist was the ONLY platform without a row cap. Facebook caps at 40 and
    # Mercari at 30, but a broad Craigslist term returns everything: measured
    # 2026-08-12, "road bike" gave 340 tiles and "nintendo switch" 300, each
    # costing a detail page. That is the whole reason Craigslist dominated the
    # 10-user stress test at 1,374s. Results come back sort=date, so the first N
    # are the newest — exactly what a deal alert wants.
    try:
        cl_max_rows = int(os.getenv('CRAIGSLIST_MAX_ROWS_PER_TERM', '40'))
    except ValueError:
        cl_max_rows = 40
    try:
        cl_workers = max(1, int(os.getenv('CRAIGSLIST_DETAIL_WORKERS', '6')))
    except ValueError:
        cl_workers = 6
    # Grows on each 403 and resets on the first 200, so a throttled run slows
    # down instead of hammering harder.
    cl_throttle_backoff = 2.0

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    })

    if log_callback:
        log_callback(user_id, "Waking up Craigslist scraper...", "info")
        if fetch_detail and debug:
            log_callback(user_id, "Craigslist: fetching each listing page for photos + posted time (search tiles omit them).", "info")

    for subdomain in subdomains:
        if not is_user_active(user_id):
            if debug and log_callback:
                log_callback(user_id, "Craigslist stopped by user.", "info")
            return listings
        for term in search_terms.keys():
            if not is_user_active(user_id):
                if debug and log_callback:
                    log_callback(user_id, "Craigslist stopped by user.", "info")
                return listings
            url = f"https://{subdomain}.craigslist.org/search/{cl_cat}?query={term.replace(' ', '+')}&sort=date&postal={zip_code}&search_distance={search_radius}"
            try:
                response = session.get(url, timeout=15)
                _record_bytes('craigslist', len(response.content or b''))

                # A throttled Craigslist answers 403 with an empty body, which
                # parsed to zero tiles and was reported as "scanned 0 rows" —
                # identical to a genuinely empty search. Measured 2026-08-12
                # while load-testing: two 403s then a 200, so it is rate limiting
                # rather than a ban, and it is recoverable by backing off.
                # Saying so matters because "0 results" sent debugging toward
                # search terms instead of request volume.
                if response.status_code != 200:
                    if log_callback:
                        log_callback(
                            user_id,
                            f"Craigslist returned HTTP {response.status_code} for "
                            f"'{term}' — rate limited, not empty. Backing off; "
                            "lower CRAIGSLIST_DETAIL_WORKERS if this persists.",
                            'error',
                        )
                    time.sleep(min(30.0, max(1.0, cl_throttle_backoff)))
                    cl_throttle_backoff = min(30.0, cl_throttle_backoff * 2)
                    continue
                cl_throttle_backoff = 2.0

                soup = BeautifulSoup(response.content, 'html.parser')
                items = soup.find_all('li', class_='cl-static-search-result')
                if debug and log_callback:
                    log_callback(user_id, f"Craigslist {subdomain} '{term}': scanned {len(items)} rows", "info")

                # PASS 1 — everything decidable without a network call.
                # Each survivor costs a detail page, so every filter that can run
                # on tile data alone belongs here rather than after the fetch.
                candidates = []
                skipped_title = 0
                for item in items[:max(1, cl_max_rows)]:
                    if not is_user_active(user_id):
                        if debug and log_callback:
                            log_callback(user_id, "Craigslist stopped by user.", "info")
                        return listings
                    try:
                        title_elem = item.find('div', class_='title')
                        title = title_elem.text.strip() if title_elem else None
                        if not title:
                            continue

                        time_elem = item.find('time')
                        dt_text = time_elem.get('datetime') if time_elem else None
                        if dt_text and not _is_recent_timestamp(dt_text, max_age_days):
                            continue

                        link_elem = item.find('a')
                        link = link_elem['href'] if link_elem else None
                        if link and not link.startswith('http'):
                            link = f'https://{subdomain}.craigslist.org' + link
                        if not link:
                            continue

                        price_elem = item.find('div', class_='price')
                        price = extract_price(price_elem.text if price_elem else None)
                        if not price:
                            price = extract_price(item.get_text(" ", strip=True))
                        if _is_craigslist_placeholder_price(price):
                            price = None

                        # Placed before the detail fetch, not just before Vision:
                        # a listing we already hold costs a page GET plus a
                        # polite-delay sleep here, and it would be discarded by
                        # the dedup pass minutes later regardless.
                        if known_links and link in known_links:
                            continue

                        # Title match on tile data. 45% of tiles fail this, and
                        # each one used to buy a detail page first.
                        matched_term = _craigslist_title_match(title, search_terms)
                        if not matched_term:
                            skipped_title += 1
                            continue
                        # Price bounds too, when the tile already knows the price
                        # (94.5% of the time). The 5.5% without one fall through
                        # and get checked after enrichment.
                        if price is not None:
                            meets, _mt, _mx = check_price_threshold(title, price, search_terms)
                            if not meets:
                                continue

                        candidates.append({
                            'entry': {
                                'title': title,
                                'price': price,
                                'link': link,
                                'platform': 'Craigslist',
                                'console_type': None,
                                'threshold': None,
                                'image_url': None,
                                'listed_at': _parse_source_datetime(dt_text) if dt_text else None,
                                'location': f'Craigslist ({subdomain}) · {zip_code} ({search_radius} mi)',
                            },
                            'item': item,
                        })
                    except Exception:
                        continue

                if debug and log_callback and skipped_title:
                    log_callback(
                        user_id,
                        f"Craigslist {subdomain} '{term}': {len(candidates)} to enrich "
                        f"({skipped_title} skipped on title before any page fetch)",
                        'info',
                    )

                # PASS 2 — enrich the survivors in parallel. Craigslist is plain
                # HTTP with no proxy, so this is bounded by latency, not by
                # anything expensive: serially it was ~1s per listing.
                if fetch_detail and candidates:
                    def _enrich(c):
                        try:
                            keep = _enrich_craigslist_from_detail(
                                _craigslist_session(), c['entry'], max_age_days, detail_delay)
                        except Exception:
                            keep = True
                        return c, keep

                    enriched = []
                    with ThreadPoolExecutor(max_workers=min(cl_workers, len(candidates)),
                                            thread_name_prefix='cl-detail') as pool:
                        for c, keep in pool.map(_enrich, candidates):
                            if keep:
                                enriched.append(c)
                    candidates = enriched

                # PASS 3 — filters that need the enriched fields.
                for c in candidates:
                    if not is_user_active(user_id):
                        return listings
                    try:
                        entry, item = c['entry'], c['item']
                        if not entry.get('image_url'):
                            entry['image_url'] = _craigslist_image_from_data_ids(item)
                        if not fetch_detail and not entry.get('image_url'):
                            try:
                                entry['image_url'] = _extract_lazy_image_url(item.find('img'))
                            except Exception:
                                pass

                        if entry.get('price') is None or _is_craigslist_placeholder_price(entry.get('price')):
                            continue

                        title = entry['title']
                        meets_threshold, matched_term, max_price = check_price_threshold(
                            title, entry['price'], search_terms)
                        if not meets_threshold:
                            continue
                        if is_excluded(title, entry['price'], exclusions, search_terms, matched_term):
                            continue
                        entry['console_type'] = matched_term
                        entry['threshold'] = max_price

                        if not check_image_with_ai(
                            entry['image_url'], ai_enabled, ai_strictness, debug, log_callback, user_id, platform_name='craigslist',
                        matched_term=matched_term,
                        term_exclusions=(search_terms.get(matched_term) or {}).get('exclusions')):
                            continue

                        listings.append(entry)
                    except Exception:
                        continue
                time.sleep(1)
            except Exception:
                pass
    if debug and log_callback:
        log_callback(user_id, f"Craigslist complete: {len(listings)} candidate matches", "info")
    return listings


def _mercari_items_from_next_data(html_text):
    """Pull listing cards from Next.js __NEXT_DATA__ when present (SSR/hydration)."""
    out = []
    try:
        soup = BeautifulSoup(html_text, 'html.parser')
        tag = soup.find('script', id='__NEXT_DATA__')
        if not tag or not tag.string:
            return out
        data = json.loads(tag.string)

        def walk(obj):
            if isinstance(obj, dict):
                # Mercari __NEXT_DATA__ shape changes frequently; be permissive on keys.
                name = (obj.get('name') or obj.get('title') or obj.get('displayName') or '').strip()
                pid = str(
                    obj.get('id')
                    or obj.get('item_id')
                    or obj.get('itemId')
                    or obj.get('listingId')
                    or ''
                ).strip()
                link = (
                    obj.get('url')
                    or obj.get('href')
                    or obj.get('path')
                    or ''
                )
                if link and isinstance(link, str) and '/item/' in link and not link.startswith('http'):
                    link = f"https://www.mercari.com{link}"
                if not link and pid:
                    link = f"https://www.mercari.com/us/item/{pid}/"

                price_obj = (
                    obj.get('price')
                    if 'price' in obj
                    else obj.get('itemPrice')
                    or obj.get('listingPrice')
                    or obj.get('price_amount')
                )
                if isinstance(price_obj, dict):
                    amt = (
                        price_obj.get('amount')
                        or price_obj.get('value')
                        or price_obj.get('displayValue')
                        or price_obj.get('price')
                    )
                else:
                    amt = price_obj
                if amt is None:
                    amt = extract_price(
                        str(obj.get('displayPrice') or obj.get('formattedPrice') or obj.get('priceText') or '')
                    )

                price = None
                if amt is not None:
                    try:
                        # Treat integral values as dollars by default; false cent conversion
                        # was filtering real listings out by dropping price 100x.
                        price = float(amt)
                    except (TypeError, ValueError):
                        price = extract_price(str(amt))

                if name and link and price:
                    thumb = None
                    photos = obj.get('photos') or obj.get('thumbnails') or obj.get('images') or []
                    if photos:
                        if isinstance(photos[0], dict):
                            thumb = photos[0].get('url') or photos[0].get('imageUrl') or photos[0].get('thumbnailUrl')
                        elif isinstance(photos[0], str):
                            thumb = photos[0]
                    listed_dt = None
                    for lk in ('created', 'created_at', 'item_created_at', 'listed_at', 'updated', 'created_time', 'posted_at'):
                        if lk in obj and obj[lk] is not None:
                            listed_dt = _parse_source_datetime(obj.get(lk))
                            if listed_dt:
                                break
                    out.append({
                        'title': str(name).strip(),
                        'price': price,
                        'link': str(link).strip(),
                        'image_url': thumb,
                        'listed_at': listed_dt,
                    })
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(data)
        # Last-resort fallback: extract inline item links + nearby price text from serialized JSON.
        if not out:
            blob = tag.string
            for m in re.finditer(r'"/(?:us/)?item/[^"]+"', blob):
                try:
                    frag = m.group(0).strip('"')
                    link = f"https://www.mercari.com{frag}"
                    window = blob[max(0, m.start() - 220): m.end() + 220]
                    p = extract_price(window)
                    if not p:
                        continue
                    out.append({
                        'title': 'Mercari listing',
                        'price': p,
                        'link': link,
                        'image_url': None,
                        'listed_at': None,
                    })
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _mercari_dedupe_raw(raw_items):
    seen_links = set()
    deduped = []
    for r in raw_items:
        lk = r.get('link')
        if not lk or lk in seen_links:
            continue
        seen_links.add(lk)
        deduped.append(r)
    return deduped


def _mercari_collect_from_html(html_text):
    raw_items = _mercari_items_from_next_data(html_text)
    lower_html = (html_text or '').lower()
    if not raw_items:
        soup = BeautifulSoup(html_text, 'html.parser')
        # Current Mercari grid cards (observed): a[data-testid='ProductThumbWrapper'] + p[data-testid='ItemPrice']
        product_cards = soup.select("a[data-testid='ProductThumbWrapper'][href*='/item/']")
        for a in product_cards[:120]:
            try:
                href = a.get('href') or ''
                if not href or '/item/' not in href:
                    continue
                link = href if href.startswith('http') else f"https://www.mercari.com{href}"
                img = a.find('img')
                title = ((img.get('alt') if img else None) or a.get('aria-label') or '').strip()
                if not title:
                    title = (a.get_text(" ", strip=True) or '').strip()
                price_el = a.select_one("[data-testid='ItemPrice']")
                price = extract_price(price_el.get_text(" ", strip=True) if price_el else a.get_text(" ", strip=True))
                text_blob = a.get_text(" ", strip=True).lower()
                if 'sold' in text_blob or 'item sold' in text_blob or 'soldlisting' in text_blob:
                    continue
                if title and price:
                    raw_items.append({
                        'title': title,
                        'price': price,
                        'link': link,
                        'image_url': _extract_lazy_image_url(img) if img else None,
                    })
            except Exception:
                continue

        anchors = soup.select("a[href*='/item/']")
        for a in anchors[:80]:
            try:
                href = a.get('href') or ''
                if not href or '/item/' not in href:
                    continue
                link = href if href.startswith('http') else f"https://www.mercari.com{href}"
                title = (a.get('aria-label') or a.get_text(" ", strip=True) or '').strip()
                full_text = a.get_text(" ", strip=True)
                lower_text = full_text.lower()
                if 'sold' in lower_text or 'item sold' in lower_text or 'soldlisting' in lower_text:
                    continue
                price = extract_price(full_text)
                if title and price:
                    _img = a.find('img')
                    raw_items.append({
                        'title': title,
                        'price': price,
                        'link': link,
                        'image_url': _extract_lazy_image_url(_img) if _img else None,
                    })
            except Exception:
                continue
    if 'item sold' in lower_html or 'data-testid="soldlisting"' in lower_html:
        raw_items = [r for r in raw_items if 'sold' not in (r.get('title') or '').lower()]
    return _mercari_dedupe_raw(raw_items)


def _mercari_items_from_json_ld(html_text):
    out = []
    try:
        soup = BeautifulSoup(html_text, 'html.parser')
        tags = soup.find_all('script', attrs={'type': 'application/ld+json'})
        for tag in tags:
            raw = (tag.string or tag.get_text() or '').strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                # Support ItemList and Product-like nodes.
                if str(node.get('@type', '')).lower() == 'itemlist':
                    for li in node.get('itemListElement') or []:
                        item = li.get('item') if isinstance(li, dict) else None
                        if isinstance(item, dict):
                            nodes.append(item)
                    continue
                name = (node.get('name') or '').strip()
                url = (node.get('url') or '').strip()
                img = node.get('image')
                if isinstance(img, list):
                    img = img[0] if img else None
                if isinstance(img, dict):
                    img = img.get('url')
                offers = node.get('offers') if isinstance(node.get('offers'), dict) else {}
                availability = str(offers.get('availability') or node.get('availability') or '').lower()
                if 'soldout' in availability or 'outofstock' in availability:
                    continue
                price_raw = offers.get('price') or node.get('price')
                try:
                    price = float(price_raw)
                except (TypeError, ValueError):
                    price = extract_price(str(price_raw))
                listed_at = _parse_source_datetime(node.get('datePosted') or node.get('dateCreated'))
                if name and url and price:
                    out.append({
                        'title': name,
                        'price': price,
                        'link': url if url.startswith('http') else f"https://www.mercari.com{url}",
                        'image_url': img if isinstance(img, str) else None,
                        'listed_at': listed_at,
                    })
    except Exception:
        pass
    return out


def _mercari_extract_items_from_script_blobs(html_text):
    """Last-resort parser for embedded JSON blobs with listing card fields."""
    out = []
    if not html_text:
        return out
    try:
        blobs = re.findall(r'\{[^{}]*"id"[^{}]*"name"[^{}]*"price"[^{}]*\}', html_text)
    except Exception:
        blobs = []
    for b in blobs[:300]:
        try:
            obj = json.loads(b)
            name = (obj.get('name') or obj.get('title') or '').strip()
            pid = str(obj.get('id') or obj.get('item_id') or '').strip()
            if not (name and pid):
                continue
            price_raw = obj.get('price')
            price = float(price_raw) if isinstance(price_raw, (int, float, str)) else None
            if price is None:
                continue
            out.append({
                'title': name,
                'price': price,
                'link': f"https://www.mercari.com/us/item/{pid}/",
                'image_url': None,
                'listed_at': _parse_source_datetime(obj.get('created_at') or obj.get('created')),
            })
        except Exception:
            continue
    return out


def _normalize_proxy(p):
    """
    Normalize a proxy string into http://user:pass@host:port format.
    Handles multiple common export formats:
      host:port                    → http://host:port
      host:port:user:pass          → http://user:pass@host:port  (ProxyScrape / Webshare export)
      user:pass:host:port          → http://user:pass@host:port  (some providers)
      http://user:pass@host:port   → unchanged
    """
    p = (p or '').strip()
    if not p:
        return None
    if '://' in p:
        return p
    parts = p.split(':')
    if len(parts) == 4:
        # Determine format by checking if part[0] looks like an IP or hostname
        # host:port:user:pass → parts[0] is IP-like (digits/dots) or hostname
        # user:pass:host:port → parts[2] is IP-like
        import re as _re
        if _re.match(r'^[\d.]+$', parts[0]) or '.' in parts[0]:
            # host:port:user:pass
            host, port, user, pw = parts
        else:
            # user:pass:host:port
            user, pw, host, port = parts
        return f'http://{user}:{pw}@{host}:{port}'
    if len(parts) == 2:
        return f'http://{p}'
    return f'http://{p}'


def _pick_from_proxy_list(raw):
    """Pick one proxy at random from a comma/newline-separated string."""
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        import random
        parts = [p.strip() for p in re.split(r'[,\n]', raw)]
        proxies = [_normalize_proxy(p) for p in parts if p and not p.startswith('#')]
        proxies = [p for p in proxies if p]
        return random.choice(proxies) if proxies else None
    except Exception:
        return None


def _pick_from_proxy_file(path):
    """Pick one proxy at random from a newline-delimited proxy file."""
    path = (path or '').strip()
    if not path or not os.path.isfile(path):
        return None
    try:
        import random
        with open(path, 'r') as f:
            proxies = [
                _normalize_proxy(ln) for ln in f
                if ln.strip() and not ln.lstrip().startswith('#')
            ]
        proxies = [p for p in proxies if p]
        return random.choice(proxies) if proxies else None
    except Exception:
        return None


def _get_proxy(platform=None):
    """
    Return the proxy URL to use for one fetch, routed per platform.

    Facebook is deliberately kept OFF the rotating pool. An authenticated
    session that hops between IPs looks like account takeover, so Facebook
    only ever uses a single pinned proxy (or none at all):
      FB_NO_PROXY=true  — force the real egress IP, ignoring FB_PROXY_URL
      FB_PROXY_URL      — one sticky-session endpoint (use on Render, where
                          egress is a datacenter IP); unset locally so the
                          scrape runs from your own residential IP
    Facebook never falls through to the shared pool below.

    Every platform supports the same two overrides, so paying for residential
    bandwidth on a site that doesn't need it is always avoidable:
      <PLATFORM>_NO_PROXY=true  — force the real egress IP for that platform
      <PLATFORM>_PROXY_URL      — a dedicated endpoint for that platform
    e.g. OFFERUP_NO_PROXY=true (OfferUp does not IP-filter, so the shared pool
    is wasted spend) or MERCARI_PROXY_URL=<sticky> (Cloudflare binds its
    clearance cookie to one IP, so rotation actively hurts there).

    Anything without an override falls through to the shared pool, where
    rotation is pure upside:
      PROXY_URL   — single proxy, used as-is
      PROXY_FILE  — path to a TXT file, one proxy per line (ProxyScrape export)
      PROXY_LIST  — comma-separated proxies in the env var itself
    """
    plat = (platform or '').strip().lower()
    # 'facebook' -> FB_* for backwards compatibility with existing env/Render config.
    prefix = 'FB' if plat == 'facebook' else plat.upper()

    if prefix:
        if _env_flag(f'{prefix}_NO_PROXY', False):
            return None
        dedicated = _normalize_proxy(os.getenv(f'{prefix}_PROXY_URL') or '')
        if dedicated:
            return dedicated
        # A per-platform pool, so a platform that only needs cheap datacenter
        # IPs never falls through to metered residential. The *_LIST form is
        # what production uses: a proxy file can't be committed (credentials),
        # so on Render the pool has to arrive as an env var.
        picked = _pick_from_proxy_list(os.getenv(f'{prefix}_PROXY_LIST') or '')
        if picked:
            return picked
        picked = _pick_from_proxy_file(os.getenv(f'{prefix}_PROXY_FILE') or '')
        if picked:
            return picked

    # Facebook never falls through to the rotating pool: an authenticated
    # session hopping between IPs looks like account takeover.
    if plat == 'facebook':
        return None

    single = _normalize_proxy(os.getenv('PROXY_URL') or '')
    if single:
        return single

    proxy_file = (os.getenv('PROXY_FILE') or '').strip()
    if proxy_file and os.path.isfile(proxy_file):
        try:
            import random
            with open(proxy_file, 'r') as f:
                proxies = [_normalize_proxy(ln) for ln in f if ln.strip() and not ln.lstrip().startswith('#')]
            proxies = [p for p in proxies if p]
            if proxies:
                return random.choice(proxies)
        except Exception:
            pass

    proxy_list_raw = (os.getenv('PROXY_LIST') or '').strip()
    if proxy_list_raw:
        import random
        proxies = [_normalize_proxy(p) for p in proxy_list_raw.split(',')]
        proxies = [p for p in proxies if p]
        if proxies:
            return random.choice(proxies)

    return None


def _mercari_collect_from_search_urls(urls, headers, log_callback=None, user_id=None, term=None, debug=False):
    """Try direct HTTP, then Playwright across Mercari search URL variants."""
    raw_items = []
    saw_403 = False
    skip_direct = (os.getenv('MERCARI_SKIP_DIRECT', '') or '').strip().lower() in ('1', 'true', 'yes')

    if not skip_direct:
        for url in urls:
            try:
                response = requests.get(url, headers=headers, timeout=20)
            except requests.RequestException as exc:
                if debug and log_callback and user_id is not None:
                    log_callback(
                        user_id,
                        f"Mercari {term!r}: direct fetch failed ({exc.__class__.__name__})",
                        'info',
                    )
                continue
            if response.status_code != 200:
                if response.status_code == 403:
                    saw_403 = True
                continue
            html = response.text or ''
            raw_items = _mercari_collect_from_html(html)
            if not raw_items:
                raw_items = _mercari_items_from_json_ld(html)
            if not raw_items:
                raw_items = _mercari_extract_items_from_script_blobs(html)
            if raw_items:
                return raw_items, 'direct'

    if raw_items:
        return raw_items, 'empty'

    if debug and log_callback and user_id is not None:
        log_callback(
            user_id,
            f"Mercari {term!r}: direct fetch {'blocked (403)' if saw_403 else 'empty'} — trying Playwright…",
            'info',
        )

    pw_url = urls[0]

    # patchright first: it is the only path measured to clear Cloudflare and
    # get /v1/api to return 200 instead of 403. Plain Playwright is kept as a
    # fallback only for environments where patchright/Chrome isn't available.
    if _env_flag('MERCARI_USE_PATCHRIGHT', True):
        html = _mercari_fetch_with_patchright(pw_url)
        if html:
            raw_items = (_mercari_collect_from_html(html)
                         or _mercari_items_from_json_ld(html)
                         or _mercari_extract_items_from_script_blobs(html))
            if raw_items:
                return raw_items, 'patchright'

    html = _mercari_fetch_with_playwright(pw_url)
    if html:
        raw_items = _mercari_collect_from_html(html) or _mercari_items_from_json_ld(html)
        if not raw_items:
            raw_items = _mercari_extract_items_from_script_blobs(html)
        if raw_items:
            return raw_items, 'playwright'

    return [], 'playwright_empty'


# ===========================
# PLAYWRIGHT FETCH HELPERS (OfferUp, Mercari, Facebook)
# ===========================
_OFFERUP_STEALTH_INIT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'}]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    Object.defineProperty(navigator, 'permissions', {
        get: () => ({query: () => Promise.resolve({state: 'prompt'})})
    });
    Object.defineProperty(screen, 'colorDepth', {get: () => 24});
"""


def _offerup_is_geo_blocked(html):
    """
    OfferUp serves a ~45KB "we weren't able to determine your location / turn
    off your proxy" page when it identifies the exit IP as a proxy or VPN.

    This is distinct from rate limiting: no amount of retrying helps, and
    switching to another proxy from the same provider usually doesn't either,
    since they share ASNs. The only fix is a different kind of IP.
    """
    if not html:
        return False
    low = html.lower()
    return (
        "weren't able to determine your location" in low
        or 'were not able to determine your location' in low
        or ('only available in the us' in low and 'proxy' in low)
    )


def _offerup_html_is_blocked(html):
    """
    A page is only 'blocked' if it carries no listings.

    Do NOT treat the substring 'captcha' as a block signal: OfferUp ships
    captcha-related JS on perfectly good pages, so that check fired on every
    successful 273KB/22-listing fetch and forced a pointless stealth refetch
    each time. Real blocks show up as the ~215KB listing-less shell, which the
    item-link count already catches.
    """
    if not html or len(html) < 5000:
        return True
    lower = html.lower()
    if '/unavailable/blk' in lower:
        return True
    if lower.count('/item/detail/') < 2 and html.count('/item/') < 2:
        return True
    return False


_PW_STEALTH_INIT = _OFFERUP_STEALTH_INIT

_PW_LAUNCH_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-extensions',
]

# Extra flags for small containers (Render Starter is a hard 512MB, shared with
# Flask and the scraper thread — an OOM kills the whole service mid-scrape).
#
# None of these change WHAT is scraped: no fewer pages, terms or platforms.
# They cut what Chrome allocates per page.
#
#   --single-process        renderer in the browser process, so no second
#                           ~150MB process per tab. The largest single saving,
#                           and the least stable flag here — hence its own
#                           toggle below.
#   --renderer-process-limit=1  caps renderers when not single-process.
#   --js-flags=--max-old-space-size=192  bounds V8's heap; marketplace pages
#                           are DOM-heavy, not JS-heavy, so this is headroom
#                           we are not using.
#   --disable-software-rasterizer / --disable-background-* / --mute-audio
#                           drop subsystems a headless scrape never touches.
_PW_LOW_MEMORY_ARGS = [
    '--renderer-process-limit=1',
    '--js-flags=--max-old-space-size=192',
    '--disable-software-rasterizer',
    '--disable-background-networking',
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-client-side-phishing-detection',
    '--disable-component-extensions-with-background-pages',
    '--disable-default-apps',
    '--disable-sync',
    '--no-first-run',
    '--mute-audio',
    '--metrics-recording-only',
]


def _pw_memory_args():
    """Low-memory Chrome flags. PW_LOW_MEMORY=0 disables; on by default."""
    if not _env_flag('PW_LOW_MEMORY', True):
        return []
    args = list(_PW_LOW_MEMORY_ARGS)
    # Off by default: --single-process saves the most but is the flag most
    # likely to destabilise a page. Turn it on only if OOMs continue.
    if _env_flag('PW_SINGLE_PROCESS', False):
        args.append('--single-process')
    return args


# ===========================
# BANDWIDTH ACCOUNTING
#
# Residential proxy bandwidth is the dominant marginal cost of this product and
# the number nobody had. Facebook and Mercari use the expensive pool; OfferUp
# goes direct via GraphQL and Craigslist is plain requests, so both are free.
#
# Counted per platform per scrape so a real day of running yields a real
# monthly projection instead of an extrapolation from test traffic.
# ===========================
_BYTES = {}
_BYTES_LOCK = threading.Lock()

# Which platforms spend residential bandwidth. Used only for reporting.
_PROXIED_PLATFORMS = {'facebook', 'mercari'}


def _record_bytes(platform, n):
    if not n or n <= 0:
        return
    with _BYTES_LOCK:
        _BYTES[platform] = _BYTES.get(platform, 0) + int(n)


def _reset_bytes():
    with _BYTES_LOCK:
        _BYTES.clear()


def _bytes_snapshot():
    with _BYTES_LOCK:
        return dict(_BYTES)


def _attach_byte_counter(page, platform):
    """
    Sum response sizes for one page.

    'requestfinished' rather than 'response' because request.sizes() is only
    populated once the transfer completes — reading it earlier reports zero.
    Wrapped in try/except throughout: byte accounting must never be able to
    break a scrape.
    """
    def on_finished(request):
        try:
            sizes = request.sizes()
            total = (sizes.get('responseBodySize') or 0) + (sizes.get('responseHeadersSize') or 0)
            _record_bytes(platform, total)
        except Exception:
            pass

    try:
        page.on('requestfinished', on_finished)
    except Exception:
        pass


def _format_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if abs(n) < 1024 or unit == 'GB':
            return f"{n:.1f}{unit}" if unit != 'B' else f"{int(n)}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def _log_bandwidth(log_callback=None, user_id=None):
    """Report this scrape's traffic, and what it implies at 5-minute intervals."""
    snap = _bytes_snapshot()
    if not snap:
        return
    total = sum(snap.values())
    proxied = sum(v for k, v in snap.items() if k in _PROXIED_PLATFORMS)
    parts = ', '.join(f"{k} {_format_bytes(v)}" for k, v in sorted(snap.items(), key=lambda x: -x[1]))

    # 288 scans/day at a 5-minute interval, 30 days.
    monthly_gb = (proxied * 288 * 30) / (1024 ** 3)
    msg = (f"Bandwidth this scan: {_format_bytes(total)} total ({parts}). "
           f"Residential (Facebook+Mercari): {_format_bytes(proxied)} "
           f"-> ~{monthly_gb:.1f}GB/month per user at 5-minute scans.")
    print(f"📊 {msg}", flush=True)
    if log_callback and _env_flag('LOG_BANDWIDTH', False):
        log_callback(user_id, msg, 'info')


def _process_tree_rss_mb():
    """
    Resident memory of this process AND its children, in MB, or None off Linux.

    Children matter more than the parent here: Chrome's renderers are separate
    processes, and Render's OOM killer counts the whole container. Reading only
    our own RSS would show a comfortable 150MB right up until the instance dies.
    """
    try:
        pids = [os.getpid()]
        children = os.path.join('/proc', str(os.getpid()), 'task')
        # Walk /proc for anything whose parent chain reaches us. Cheap enough at
        # the handful of processes a scrape creates.
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            try:
                with open(f'/proc/{entry}/stat', 'r') as f:
                    ppid = int(f.read().split(') ')[1].split()[1])
                if ppid in pids or ppid == os.getpid():
                    pids.append(int(entry))
            except Exception:
                continue
        total_kb = 0
        for pid in set(pids):
            try:
                with open(f'/proc/{pid}/status', 'r') as f:
                    for line in f:
                        if line.startswith('VmRSS:'):
                            total_kb += int(line.split()[1])
                            break
            except Exception:
                continue
        return round(total_kb / 1024, 1) if total_kb else None
    except Exception:
        return None


def _log_memory(label, log_callback=None, user_id=None):
    """Report container memory at a platform boundary, when it is worth seeing."""
    mb = _process_tree_rss_mb()
    if mb is None:
        return
    try:
        limit = int(os.getenv('MEMORY_LIMIT_MB', '512'))
    except ValueError:
        limit = 512
    pct = (mb / limit) * 100 if limit else 0
    msg = f"Memory after {label}: {mb:.0f}MB of {limit}MB ({pct:.0f}%)"
    if pct >= 80:
        print(f"⚠️  {msg}", flush=True)
        if log_callback:
            log_callback(user_id, msg + " — close to the container limit", 'error')
    else:
        print(msg, flush=True)
        if log_callback and _env_flag('LOG_MEMORY_ALWAYS', False):
            log_callback(user_id, msg, 'info')


def _pw_viewport():
    """
    Viewport drives raster memory: it is width x height x 4 bytes per surface,
    so 1280x800 costs roughly 2.6x what 800x600 does.

    Kept at 1280 wide by default because marketplace grids drop to fewer columns
    on narrow viewports and lazy-loading yields fewer tiles — a smaller window
    would quietly return fewer listings, which is the one trade we are not
    making. PW_VIEWPORT_WIDTH/HEIGHT exist if you decide otherwise.
    """
    try:
        w = int(os.getenv('PW_VIEWPORT_WIDTH', '1280'))
        h = int(os.getenv('PW_VIEWPORT_HEIGHT', '800'))
    except ValueError:
        w, h = 1280, 800
    return {'width': w, 'height': h}


def _pw_proxy_dict(proxy_url):
    """
    Playwright requires proxy credentials split out from the server URL.
    Converts http://user:pass@host:port into {'server': 'http://host:port', 'username': ..., 'password': ...}.
    """
    from urllib.parse import urlparse
    parsed = urlparse(proxy_url)
    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    d = {'server': server}
    if parsed.username:
        d['username'] = parsed.username
    if parsed.password:
        d['password'] = parsed.password
    return d


# Analytics / telemetry hosts that cost residential bandwidth and return nothing
# we parse. Blocking them also shrinks the fingerprinting surface.
_BLOCKED_HOSTS = (
    'google-analytics.com', 'googletagmanager.com', 'doubleclick.net',
    'facebook.net', 'segment.io', 'segment.com', 'amplitude.com',
    'mixpanel.com', 'branch.io', 'bugsnag.com', 'sentry.io',
    'hotjar.com', 'newrelic.com', 'nr-data.net', 'optimizely.com',
    'scorecardresearch.com', 'quantserve.com', 'adsrvr.org',
)


def _pw_block_heavy_requests(ctx, block_scripts=False):
    """
    Drop request types whose bytes we never use. Image *URLs* survive this —
    they live in the <img src> attribute, and the DOM keeps them whether or not
    Chromium downloads the pixels. Google Vision fetches images server-side by
    URI, and the dashboard embeds URLs for the end user's browser to load, so
    nothing downstream needs the bytes on our side.

    First-party JS is deliberately left alone: lazy-loaded listings and the
    antibot checks depend on it.

    `block_scripts` is the exception, used only by Facebook's lean fetch. There
    the listings are server-rendered as JSON inside the HTML document, so the
    1.9MB of JS renders a DOM we would only parse back into the data we already
    had. Measured 2026-08-11: 1,651KB -> 111KB with identical listings. It stays
    opt-in because dropping JS is exactly what breaks scrapers that DO need it.
    """
    blocked_types = {'image', 'media', 'font'}
    if _env_flag('PW_BLOCK_CSS', True):
        blocked_types.add('stylesheet')
    if block_scripts:
        blocked_types.add('script')

    def _route(route, request):
        try:
            if request.resource_type in blocked_types:
                return route.abort()
            host = (urlparse(request.url).hostname or '').lower()
            if any(host == b or host.endswith('.' + b) for b in _BLOCKED_HOSTS):
                return route.abort()
            return route.continue_()
        except Exception:
            try:
                return route.continue_()
            except Exception:
                return

    try:
        ctx.route('**/*', _route)
    except Exception:
        pass


def _pw_launch_kwargs(platform=None):
    """
    Launch args. The proxy is deliberately NOT set here: headless Chromium
    fails authenticated launch-level proxies with ERR_PROXY_AUTH_UNSUPPORTED.
    Credentials only work on the context (see _pw_new_context).
    """
    launch_kwargs = dict(headless=True, args=_PW_LAUNCH_ARGS + _pw_memory_args())
    chromium_path = os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH') or None
    if chromium_path:
        launch_kwargs['executable_path'] = chromium_path
    return launch_kwargs


def _pw_new_context(browser, stealth=True, extra_headers=None, platform=None, storage_state=None,
                    force_no_proxy=False, proxy_override=None, block_scripts=False):
    """
    New browser context with per-platform proxy routing (see _get_proxy).

    `proxy_override` bypasses that routing for a single context. It exists for
    retrying on a different sticky session after a block — see
    _rotate_sticky_session.

    `block_scripts` additionally drops JS. Only Facebook's lean fetch sets it —
    see _pw_block_heavy_requests.
    """
    proxy_url = None if force_no_proxy else (proxy_override or _get_proxy(platform))
    ctx_kwargs = dict(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        viewport=_pw_viewport(),
        locale='en-US',
        extra_http_headers={'Accept-Language': 'en-US,en;q=0.9', **(extra_headers or {})},
    )
    if proxy_url:
        ctx_kwargs['proxy'] = _pw_proxy_dict(proxy_url)
    if storage_state:
        ctx_kwargs['storage_state'] = storage_state
    ctx = browser.new_context(**ctx_kwargs)
    if stealth:
        ctx.add_init_script(_PW_STEALTH_INIT)
    if _env_flag('PW_BLOCK_HEAVY_REQUESTS', True):
        _pw_block_heavy_requests(ctx, block_scripts=block_scripts)
    elif block_scripts:
        # The lean fetch's saving IS the script block, so honour it even when
        # the general heavy-request router is turned off.
        _pw_block_heavy_requests(ctx, block_scripts=True)
    return ctx


# ---------------------------------------------------------------------------
# OfferUp via GraphQL
#
# OfferUp ignores every location URL param (postal_code / zip / lat / lon) and
# every client-side override (geolocation, cookies, localStorage) — measured.
# Search results are geolocated from the exit IP, which meant every user got
# listings near whatever the proxy happened to be, not near themselves.
#
# Their own page calls GetModularFeed with lat/lon in searchParams, and that
# DOES control location. Calling it directly fixes location and drops the
# payload from ~400KB of HTML to ~30KB of JSON (~93% less proxy bandwidth).
#
# The call must be issued from inside the page: a plain requests POST is
# refused with 403 "Request has been Blocked" (TLS fingerprint / session).
# ---------------------------------------------------------------------------

_OFFERUP_QUERY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'offerup_modularfeed.graphql')
_OFFERUP_QUERY_CACHE = None

_OFFERUP_GQL_JS = """
async ([query, term, lat, lon, limit]) => {
  const params = [
    {key:'q', value: term},
    {key:'platform', value:'web'},
    {key:'lon', value: String(lon)},
    {key:'lat', value: String(lat)},
  ];
  if (limit) params.push({key:'limit', value: String(limit)});
  const res = await fetch('/api/graphql', {
    method:'POST',
    headers:{'content-type':'application/json'},
    credentials:'include',
    body: JSON.stringify({operationName:'GetModularFeed',
                          variables:{debug:false, searchParams: params},
                          query: query})
  });
  return {status: res.status, text: await res.text()};
}
"""


def _offerup_query(force_reload=False):
    """The GetModularFeed document, read from disk once per process."""
    global _OFFERUP_QUERY_CACHE
    if _OFFERUP_QUERY_CACHE and not force_reload:
        return _OFFERUP_QUERY_CACHE
    try:
        with open(_OFFERUP_QUERY_PATH, 'r', encoding='utf-8') as f:
            _OFFERUP_QUERY_CACHE = f.read().strip()
    except Exception:
        _OFFERUP_QUERY_CACHE = None
    return _OFFERUP_QUERY_CACHE


def _offerup_capture_query(page):
    """
    Re-capture GetModularFeed from a live page load and persist it.

    This is the self-healing path: when OfferUp changes their schema the stored
    document starts erroring, and rather than needing a code release we grab the
    current one straight off their own page and carry on.
    """
    captured = {}

    def on_req(r):
        if '/api/graphql' in r.url and r.method == 'POST' and not captured:
            try:
                body = json.loads(r.post_data or '{}')
                if body.get('operationName') == 'GetModularFeed' and body.get('query'):
                    captured['query'] = body['query']
            except Exception:
                pass

    page.on('request', on_req)
    try:
        page.goto('https://offerup.com/search/?q=laptop', wait_until='domcontentloaded', timeout=40000)
        page.wait_for_timeout(6000)
        for _ in range(3):
            if captured:
                break
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2500)
    except Exception as e:
        _emit(f"[offerup] query re-capture navigation failed: {e}")
    finally:
        try:
            page.remove_listener('request', on_req)
        except Exception:
            pass

    q = captured.get('query')
    if q:
        global _OFFERUP_QUERY_CACHE
        _OFFERUP_QUERY_CACHE = q
        try:
            with open(_OFFERUP_QUERY_PATH, 'w', encoding='utf-8') as f:
                f.write(q)
            _emit("[offerup] refreshed GetModularFeed query from live page", 'info')
        except Exception:
            pass
    return q


def _offerup_tiles_to_items(payload):
    """ModularFeed tiles -> the listing dicts the rest of the pipeline expects."""
    out = []
    try:
        tiles = (payload.get('data', {}).get('modularFeed', {}) or {}).get('looseTiles') or []
    except Exception:
        return out
    for t in tiles:
        try:
            if t.get('__typename') != 'ModularFeedTileListing':
                continue          # skip ads and seller promos
            l = t.get('listing') or {}
            lid, title = l.get('listingId'), (l.get('title') or '').strip()
            # GraphQL returns a bare number ("45"); extract_price expects a
            # currency symbol and would return None for every listing.
            try:
                price = float(str(l.get('price') or '').replace(',', '').strip() or 'nan')
            except ValueError:
                price = None
            if not (lid and title and price and price == price and price > 0):
                continue
            flags = l.get('flags') or []
            out.append({
                'title': title,
                'price': price,
                'link': f'https://offerup.com/item/detail/{lid}',
                'image_url': (l.get('image') or {}).get('url'),
                'city': l.get('locationName'),
                # OfferUp states delivery outright — no title-phrase guessing.
                'is_local': 'LOCAL_PICKUP' in flags,
                'is_shipping': any('SHIP' in str(f).upper() for f in flags),
            })
        except Exception:
            continue
    return out


def _offerup_gql_call(page, query, term, lat, lon):
    """One GetModularFeed call on an already-warmed page. Returns (items, err)."""
    res = page.evaluate(_OFFERUP_GQL_JS, [query, term, lat, lon, 60])
    if res.get('status') != 200:
        return None, f"graphql HTTP {res.get('status')}"
    try:
        payload = json.loads(res.get('text') or '{}')
    except Exception:
        return None, 'unparseable graphql response'
    if payload.get('errors'):
        return None, 'graphql errors: ' + json.dumps(payload['errors'])[:160]
    return _offerup_tiles_to_items(payload), None


def _offerup_lean_attempt(browser, query, term, lat, lon, force_no_proxy):
    """
    GetModularFeed behind the cheapest warm-up that still carries a session.

    The API refuses a plain requests POST, but what it is actually checking is
    the session cookie, the origin and Chromium's TLS fingerprint — none of
    which come from OfferUp's own JavaScript. So the warm-up does not have to be
    the homepage, and does not have to run scripts.

    Measured 2026-08-11, identical 5 items from all three:

        homepage,   scripts on   14,860KB   <- what this used to do
        homepage,   scripts off      91KB
        robots.txt, scripts off       2.5KB  <- this

    The homepage was fetching ~13MB of feed XHR we then threw away. Returns
    (items, err); any error sends the caller to the full path.
    """
    ctx = _pw_new_context(browser, stealth=True, platform='offerup',
                          force_no_proxy=force_no_proxy, block_scripts=True)
    try:
        page = ctx.new_page()
        _attach_byte_counter(page, 'offerup')
        warm_url = os.getenv('OFFERUP_WARMUP_URL', 'https://offerup.com/robots.txt')
        try:
            page.goto(warm_url, wait_until='domcontentloaded', timeout=40000)
            page.wait_for_timeout(1500)
        except Exception as e:
            return None, f'lean warm-up navigation failed: {e}'
        try:
            return _offerup_gql_call(page, query, term, lat, lon)
        except Exception as e:
            return None, f'lean evaluate failed: {type(e).__name__}: {e}'
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def _offerup_fetch_via_graphql(term, lat, lon, force_no_proxy=False):
    """Search OfferUp at explicit coordinates. Returns (items, error_or_None)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return [], f'playwright missing ({e})'

    query = _offerup_query()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(**_pw_launch_kwargs('offerup'))
        except Exception as e:
            return [], f'launch failed: {e}'
        try:
            # Lean warm-up needs a stored query document: re-capturing one means
            # intercepting the request OfferUp's own JS issues, and lean mode
            # does not load that JS. No document -> go straight to the full path,
            # which can self-heal.
            if query and _env_flag('OFFERUP_LEAN_WARMUP', True):
                items, err = _offerup_lean_attempt(browser, query, term, lat, lon,
                                                   force_no_proxy)
                if not err:
                    return items, None
                _emit(f"[offerup] lean warm-up failed ({err}) — "
                      "retrying with a full page load")

            ctx = _pw_new_context(browser, stealth=True, platform='offerup',
                                  force_no_proxy=force_no_proxy)
            page = ctx.new_page()
            _attach_byte_counter(page, 'offerup')
            # One real page load first: the API rejects requests that don't
            # carry a browser session.
            try:
                page.goto('https://offerup.com/', wait_until='domcontentloaded', timeout=40000)
                page.wait_for_timeout(3000)
            except Exception as e:
                _emit(f"[offerup] warm-up navigation failed: {e}")

            if not query:
                query = _offerup_capture_query(page)
                if not query:
                    return [], 'no GetModularFeed query available'

            for attempt in (1, 2):
                items, err = _offerup_gql_call(page, query, term, lat, lon)
                if not err:
                    return items, None
                if 'unparseable' in err:
                    return [], err
                if attempt == 1:
                    # Most likely our stored document drifted from theirs.
                    query = _offerup_capture_query(page) or query
                    continue
                return [], err
            return [], 'graphql retry exhausted'
        finally:
            try:
                browser.close()
            except Exception:
                pass


def _offerup_fetch_with_playwright(url, stealth=False, force_no_proxy=False):
    """Fetch OfferUp search page using Playwright headless Chromium."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError as e:
        _emit(f"[offerup] Playwright not importable ({e}). "
              f"THIS BACKEND IS RUNNING: {sys.executable} — "
              "it must be <project>/.venv/Scripts/python.exe")
        return ''
    try:
        nav_wait_ms = int(os.getenv('OFFERUP_PLAYWRIGHT_WAIT_MS', '8000'))
    except ValueError:
        nav_wait_ms = 8000
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(**_pw_launch_kwargs('offerup'))
        except Exception as e:
            _emit(f"[offerup] chromium launch failed: {e}")
            return ''
        ctx = _pw_new_context(browser, stealth=stealth, platform='offerup',
                              force_no_proxy=force_no_proxy)
        page = ctx.new_page()
        _attach_byte_counter(page, 'offerup')
        try:
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                _emit(f"[offerup nav warning] {e}")
            try:
                page.wait_for_selector("a[href*='/item/']", timeout=nav_wait_ms)
            except PWTimeout:
                pass

            # OfferUp lazy-loads tiles on scroll. Without this a page settles at
            # only a handful of listings (measured 5 rows / 229KB) while the same
            # search scrolled reaches 31+ rows / 290KB. Stop early once the page
            # stops growing so we don't scroll a short result set pointlessly.
            try:
                scroll_count = int(os.getenv('OFFERUP_SCROLL_COUNT', '4'))
            except ValueError:
                scroll_count = 4
            last_height = 0
            for _ in range(max(0, scroll_count)):
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)
                    height = page.evaluate("document.body.scrollHeight") or 0
                    if height <= last_height:
                        break
                    last_height = height
                except Exception:
                    break

            page.wait_for_timeout(1500)
            return page.content()
        except Exception as e:
            _emit(f"[offerup playwright error] {e}")
            try:
                return page.content()
            except Exception:
                return ''
        finally:
            browser.close()


_PATCHRIGHT_UA_CACHE = None
_PATCHRIGHT_UA_LOCK = threading.Lock()


def _patchright_clean_ua():
    """
    Chrome's headless mode puts the literal string 'HeadlessChrome' in the
    User-Agent, which is all Cloudflare needs to block us. Read the browser's
    own UA once and swap that token for 'Chrome'.

    Deriving it (rather than hardcoding) keeps the version in lockstep with the
    Sec-CH-UA client hints Chrome sends — a UA/client-hint version mismatch is
    itself a bot signal. Cached: the discovery launch happens once per process.
    """
    global _PATCHRIGHT_UA_CACHE
    if _PATCHRIGHT_UA_CACHE is not None:
        return _PATCHRIGHT_UA_CACHE
    override = (os.getenv('MERCARI_UA') or '').strip()
    if override:
        _PATCHRIGHT_UA_CACHE = override
        return _PATCHRIGHT_UA_CACHE

    # Serialised, and on a private directory. Measured with 10 concurrent
    # scrapes: every worker saw an empty cache, all ten launched Chrome on the
    # SAME probe profile, Chrome's SingletonLock rejected most of them, and they
    # all fell back to the static UA below — which pins Chrome/150 while the real
    # browser is 151. A UA/client-hint version mismatch is exactly the signal
    # this function exists to avoid, so under load the evasion silently undid
    # itself. Double-checked so the winner's result is reused, not re-probed.
    with _PATCHRIGHT_UA_LOCK:
        if _PATCHRIGHT_UA_CACHE is not None:
            return _PATCHRIGHT_UA_CACHE
        prof = None
        try:
            from patchright.sync_api import sync_playwright
            prof = tempfile.mkdtemp(prefix='pixelflip_ua_probe_')
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    prof, headless=True,
                    no_viewport=True,
                    **_mercari_chrome_kwargs(),
                )
                ua = ctx.new_page().evaluate('navigator.userAgent')
                ctx.close()
            _PATCHRIGHT_UA_CACHE = (ua or '').replace('HeadlessChrome', 'Chrome')
        except Exception as e:
            print(f"[mercari] UA probe failed ({e}); falling back to static UA", flush=True)
            _PATCHRIGHT_UA_CACHE = (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36'
            )
        finally:
            if prof:
                try:
                    shutil.rmtree(prof, ignore_errors=True)
                except Exception:
                    pass
    return _PATCHRIGHT_UA_CACHE


# Chrome flags that shrink the capture render's process tree. Each one removes
# a helper process or a buffer we never read; none of them changes what the page
# renders, so listing counts are unaffected. Enabled with MERCARI_LOWMEM=1.
_MERCARI_LOWMEM_ARGS = [
    '--renderer-process-limit=1',      # one renderer instead of one per frame
    '--disable-gpu',                   # drops the GPU process entirely
    '--disable-software-rasterizer',
    '--disable-extensions',
    '--disable-background-networking',
    '--disable-sync',
    '--mute-audio',
    # We already block image REQUESTS; this also stops Blink allocating decode
    # buffers for anything that slips through (data: URIs, CSS backgrounds).
    '--blink-settings=imagesEnabled=false',
    '--js-flags=--max-old-space-size=128',
]


def _mercari_chrome_kwargs(bundled=False):
    """
    How to reach real Chrome: by channel, or by explicit path.

    `channel='chrome'` asks Playwright to find a system-installed Chrome, which
    on Render's native runtime does not exist — installing it needs apt and
    root. MERCARI_CHROME_PATH points straight at a binary instead, so Chrome can
    be unpacked from its .deb with `dpkg-deb -x` (no root) during the build.

    The two are mutually exclusive in Playwright: passing both raises.

    `bundled=True` returns neither, so patchright uses its OWN Chromium. That is
    viable for the search-API path — measured 2026-08-12, all three combinations
    against the same term:

        patchright + real Chrome        28 items
        patchright + bundled Chromium   28 items
        playwright + bundled Chromium   HTTP 403

    So patchright is the load-bearing part, not the Chrome binary. This matters
    because the rootless `dpkg-deb -x` unpack cannot install Chrome's shared
    library dependencies, and a Chrome that starts (about:blank works) but
    cannot do TLS looks exactly like Cloudflare blocking us.
    """
    if bundled or _env_flag('MERCARI_BUNDLED_CHROMIUM', False):
        return {}
    explicit = (os.getenv('MERCARI_CHROME_PATH') or '').strip()
    if explicit:
        if not os.path.isfile(explicit):
            _emit(f"[mercari] MERCARI_CHROME_PATH set but no file at {explicit} — falling back to channel")
        else:
            return {'executable_path': explicit}
    return {'channel': os.getenv('MERCARI_CHROME_CHANNEL', 'chrome')}


# ---------------------------------------------------------------------------
# Mercari via its own search API (searchFacetQuery)
#
# Measured 2026-08-11, direct from a residential IP:
#
#   full search page render      4,251 KB   (script 3,225, image 468, font 123)
#   searchFacetQuery replay          4.5 KB wire  (54 KB uncompressed)
#
# 945x less per term. The page downloads 3.2MB of JavaScript whose entire job is
# to issue this one API call and render its result, which we then parse back out
# of the DOM. Same shape as the OfferUp and Facebook work.
#
# Three things make it possible:
#   * The call must come from INSIDE the page. Mercari 403s /v1/api for a plain
#     request — it is checking the session cookie, the Cloudflare clearance and
#     Chromium's TLS fingerprint, none of which a `requests` call has.
#   * The persistent profile already holds cf_clearance, so the bootstrap can be
#     robots.txt (0.8 KB) rather than a search page.
#   * `authorization: Bearer <jwt>` is NOT in any cookie or localStorage — it
#     comes from Mercari's app JS. So it has to be captured from one real page
#     load and reused, and re-captured when it expires.
#
# Two response-shrinking parameters, both measured:
#   facetTypes: []  200KB -> 138KB   (category/brand/price filter UI we never read)
#   length: 40      138KB ->  55KB   (MERCARI_MAX_ROWS_PER_TERM is 30)
#
# The token used to be kept in memory ONLY, on the reasoning that a restart
# needs a fresh capture anyway because it expires. MEASURED 2026-08-13 and that
# is wrong: a context written to disk and reloaded in a FRESH process returned
# the same 56 listings for two terms at 7.9 KB and 6.3s, against 3,063 KB and
# 15.9s for the same run when it had to capture. The capture render IS Mercari's
# bandwidth — everything else is already 4 KB per term.
#
# It is still a live session credential on disk, which is why it is TTL-bounded
# and written 0600 to the temp dir rather than beside the source. That is the
# same tradeoff already accepted for the persistent Chrome profile, which holds
# cf_clearance — an equivalent credential — for exactly the same reason.
# Staleness is free: a 401/403 triggers the existing single re-capture.
# ---------------------------------------------------------------------------

_MERCARI_API_CTX = None            # {'headers': {...}, 'variables': {...}, 'ext': str}
_MERCARI_API_LOCK = threading.Lock()
# Set when a capture fails, so a broken environment costs one attempt per
# cooldown rather than one per scrape. Without this, an environment where
# mercari.com is unreachable (observed on Render 2026-08-12) burns the full
# navigation timeout every single cycle BEFORE falling back — which is how a
# broken platform ends up delaying every working one behind it.
_MERCARI_API_COOLDOWN_UNTIL = 0.0


def _mercari_ctx_cache_path():
    import tempfile
    return os.path.join(tempfile.gettempdir(), 'pixelflip_mercari_ctx.json')


def _persist_mercari_ctx(ctx):
    """Save the captured search context so a restart skips the capture render."""
    if not _env_flag('MERCARI_PERSIST_CTX', True) or not ctx:
        return
    path = _mercari_ctx_cache_path()
    try:
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w') as f:
            json.dump({'stamp': time.time(), 'ctx': ctx}, f)
        # Atomic: a concurrent worker must never read half a file, which is the
        # bug that silently emptied the city cache (see _save_city_cache).
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"[mercari] could not persist search context: {e}", flush=True)


def _load_persisted_mercari_ctx():
    """A recently captured context from disk, or None if absent/stale/unusable."""
    if not _env_flag('MERCARI_PERSIST_CTX', True):
        return None
    try:
        ttl = int(os.getenv('MERCARI_CTX_TTL_SEC', '1800'))
    except ValueError:
        ttl = 1800
    try:
        path = _mercari_ctx_cache_path()
        if not os.path.isfile(path):
            return None
        with open(path) as f:
            payload = json.load(f)
        age = time.time() - float(payload.get('stamp') or 0)
        if age > max(0, ttl):
            return None
        ctx = payload.get('ctx') or None
        # A truncated or half-written file must not become a broken context that
        # fails every replay until the TTL expires.
        if not (isinstance(ctx, dict) and ctx.get('headers') and ctx.get('variables')
                and ctx.get('ext')):
            return None
        _emit(f"[mercari] reusing persisted search context ({int(age)}s old) — "
              "skipping the capture render", 'info')
        return ctx
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Cross-user batching
#
# Mercari's search API is user-independent: no per-user session, no location, no
# auth — only the query string changes. So thirty users tracking "gameboy
# advance sp" is thirty identical requests, and one answer serves all of them.
#
# This matters most for MEMORY, which is what actually caps concurrency.
# Measured 2026-08-12 with 10 concurrent users: 6,439MB peak, ~644MB each,
# because every user opened its own real-Chrome persistent context. Batching
# collapses that to ONE browser per cycle regardless of user count, so Mercari
# stops being the reason SCRAPE_MAX_WORKERS has to stay small.
#
# Only the FETCH is shared. Price bounds, exclusions, distance, delivery prefs,
# known_links and Vision all still run per user on the fan-out, so no user sees
# another user's filtering.
# ---------------------------------------------------------------------------
_MERCARI_CYCLE = {'stamp': 0.0, 'items': {}}
_MERCARI_CYCLE_LOCK = threading.Lock()


def _mercari_cycle_ttl():
    """
    How long a shared fetch stays usable.

    Bounded below the 5-minute priority floor so a term is never served from a
    cache older than its own scan interval — otherwise a Pro user's 5-minute
    term could be answered with 12-minute-old listings and the speed promise
    quietly stops being true.
    """
    try:
        return max(0, int(os.getenv('MERCARI_SHARED_TTL_SEC', '240')))
    except ValueError:
        return 240


def mercari_prefetch_cycle(terms, max_rows=None):
    """
    Fetch every unique term once for this cycle. Returns (n_terms, error).

    Called from the scheduler before users fan out to the thread pool, so the
    one browser this opens is the only one Mercari needs for the whole cycle.
    """
    global _MERCARI_CYCLE
    terms = sorted({(t or '').strip() for t in (terms or []) if (t or '').strip()})
    if not terms:
        return 0, None
    if not (_env_flag('MERCARI_USE_API', True) and _env_flag('MERCARI_SHARED_FETCH', True)):
        return 0, None
    if max_rows is None:
        try:
            max_rows = int(os.getenv('MERCARI_MAX_ROWS_PER_TERM', '30'))
        except ValueError:
            max_rows = 30

    items, err = _mercari_fetch_via_api(terms, max_rows)
    if items:
        with _MERCARI_CYCLE_LOCK:
            _MERCARI_CYCLE = {'stamp': time.time(), 'items': items}
    return len(items or {}), err


def _mercari_in_cooldown():
    """Seconds left on the Mercari back-off, or 0 if it is clear to try."""
    if _MERCARI_API_CTX is not None:
        return 0
    left = _MERCARI_API_COOLDOWN_UNTIL - time.time()
    return int(left) if left > 0 else 0


def _mercari_cycle_items(term):
    """This cycle's listings for `term`, or None if not fetched / too old."""
    with _MERCARI_CYCLE_LOCK:
        stamp = _MERCARI_CYCLE.get('stamp') or 0.0
        if not stamp or (time.time() - stamp) > _mercari_cycle_ttl():
            return None
        return _MERCARI_CYCLE.get('items', {}).get(term)


def _mercari_profile_dir():
    """
    Chrome profile directory for THIS worker.

    Chrome takes a SingletonLock on its profile directory, so concurrent scrapes
    sharing one path collide: the second launch fails or corrupts the profile,
    and what gets lost is the cf_clearance cookie the profile exists to retain —
    so the symptom is Cloudflare challenges returning, not an obvious crash.

    Keyed on the thread name because ThreadPoolExecutor reuses a bounded set of
    threads (scrape_0 .. scrape_N-1, see main()), so this yields exactly one
    profile per worker SLOT rather than one per scrape — profiles stay warm and
    their count is capped by SCRAPE_MAX_WORKERS instead of growing forever.
    """
    base = (os.getenv('PATCHRIGHT_PROFILE_DIR') or '').strip() or \
        os.path.join(tempfile.gettempdir(), 'pixelflip_mercari_profile')
    worker = re.sub(r'[^A-Za-z0-9_.-]', '_', threading.current_thread().name)
    return f'{base}_{worker}'

_MERCARI_REPLAY_JS = """
async ([url, headers]) => {
  const res = await fetch(url, {method: 'GET', headers, credentials: 'include'});
  const text = await res.text();
  return {status: res.status, text: text};
}
"""


def _mercari_api_capture(page, term='ds lite'):
    """
    Capture searchFacetQuery's headers and variables from one real page load.

    Returns the context dict, or None. This is the expensive path (a full search
    render) and exists only to mint a bearer token, so it runs once per process
    and again only when the token stops working.
    """
    captured = {}

    def on_request(r):
        if 'searchFacetQuery' in r.url and not captured:
            try:
                qs = parse_qs(urlparse(r.url).query)
                captured['variables'] = json.loads(unquote(qs['variables'][0]))
                captured['ext'] = unquote(qs['extensions'][0])
                # Drop per-request tracing headers: replaying a stale
                # sentry-trace/baggage is pointless and marks us as a replay.
                captured['headers'] = {
                    k: v for k, v in r.headers.items()
                    if k.lower() not in ('host', 'content-length', 'baggage',
                                         'sentry-trace', 'mercari-client-request-id')
                }
            except Exception:
                pass

    # searchFacetQuery fires during HYDRATION, which is CPU-bound: Mercari ships
    # ~1.9MB of app JS and `domcontentloaded` returns long before any of it has
    # run. This budget was a hardcoded 10s — generous on a laptop, short on a
    # 0.5-CPU container that needs 15-20s just to launch the browser (measured
    # on Render 2026-08-13, where a trivial example.com load took 22-24s).
    #
    # Raising the ceiling is free on the happy path: the loop exits the moment
    # the request arrives, so a fast environment never waits longer than it did.
    # It only spends the extra time when capture is already failing.
    try:
        budget_ms = int(os.getenv('MERCARI_CAPTURE_WAIT_MS', '30000'))
    except ValueError:
        budget_ms = 30000

    page.on('request', on_request)
    nav_ok = True
    try:
        page.goto(f'https://www.mercari.com/search/?keyword={quote(term, safe="")}',
                  wait_until='domcontentloaded',
                  timeout=int(os.getenv('MERCARI_NAV_TIMEOUT_MS', '45000')))
        deadline = time.time() + max(1.0, budget_ms / 1000.0)
        while time.time() < deadline:
            if captured:
                break
            page.wait_for_timeout(500)
    except Exception as e:
        nav_ok = False
        _emit(f"[mercari] api capture navigation failed: {e}")
    finally:
        try:
            page.remove_listener('request', on_request)
        except Exception:
            pass

    if captured.get('headers') and captured.get('variables'):
        _emit("[mercari] captured searchFacetQuery context", 'info')
        return captured

    # Exhausting the budget raises nothing, so this returned None in total
    # silence: the caller reported 'could not capture searchFacetQuery context'
    # with no way to tell slow hydration from a Cloudflare block, and the one
    # message the docs tell you to grep for — 'api capture navigation failed' —
    # is only ever printed when the NAVIGATION itself throws. Say which it was.
    if nav_ok:
        try:
            title = (page.title() or '')[:80]
        except Exception:
            title = '<unavailable>'
        low = title.lower()
        blocked = 'moment' in low or 'attention' in low or 'checking' in low
        _emit(f"[mercari] searchFacetQuery never fired within {budget_ms}ms — "
              f"page title {title!r} "
              + ('(Cloudflare challenge did not clear — this is a BLOCK)'
                 if blocked else
                 '(page loaded but hydration never reached the search call — '
                 'try raising MERCARI_CAPTURE_WAIT_MS)'))
    return None


def _mercari_api_url(ctx, term, length):
    """searchFacetQuery URL for one term, with the payload trimmed to what we use."""
    variables = json.loads(json.dumps(ctx['variables']))     # deep copy
    criteria = variables.get('criteria')
    if not isinstance(criteria, dict):
        return None
    criteria['query'] = term
    criteria['length'] = max(1, int(length))
    criteria['offset'] = 0
    # Filter-UI payload we never read — worth ~30% of the response on its own.
    criteria['facetTypes'] = []
    return ('https://www.mercari.com/v1/api?operationName=searchFacetQuery'
            f'&variables={quote(json.dumps(variables))}'
            f'&extensions={quote(ctx["ext"])}')


def _mercari_items_from_api(payload):
    """searchFacetQuery itemsList -> the listing dicts the pipeline expects."""
    out = []
    try:
        items = ((payload.get('data') or {}).get('search') or {}).get('itemsList') or []
    except Exception:
        return out
    for it in items:
        try:
            lid = str(it.get('id') or '').strip()
            title = (it.get('name') or '').strip()
            if not (lid and title):
                continue
            # Sold and closed listings are not actionable; alerting on one is
            # worse than missing it.
            if str(it.get('status') or '').lower() not in ('on_sale', ''):
                continue
            # PRICE IS IN CENTS. Verified 2026-08-11 against the DOM parser on
            # the same page: 10/10 items matched at price/100, 0/10 as-is.
            # Getting this backwards silently multiplies every price by 100 and
            # every user's max-price filter stops matching anything.
            try:
                price = float(it.get('price')) / 100.0
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            photos = it.get('photos') or []
            image_url = None
            if photos and isinstance(photos[0], dict):
                image_url = photos[0].get('imageUrl') or photos[0].get('thumbnail')
            out.append({
                'title': title,
                'price': price,
                'link': f'https://www.mercari.com/us/item/{lid}/',
                'image_url': image_url,
                'listed_at': None,
            })
        except Exception:
            continue
    return out


def _mercari_fetch_via_api(terms, max_rows):
    """
    Search Mercari for several terms, retrying on bundled Chromium if needed.

    The retry was written for a Render failure mode that has since been measured
    and DISPROVEN: the theory was that the rootless Chrome unpack leaves shared
    libraries missing, so real Chrome launches and then hangs on HTTPS. Running
    diagnose_mercari.py on Render on 2026-08-13 showed `ldd` clean, and real
    Chrome loading both example.com and mercari.com successfully. Do not
    reintroduce that explanation.

    The fallback is still worth keeping — patchright's own Chromium is
    measurably sufficient for this path (see _mercari_chrome_kwargs), so trying
    it costs one extra attempt and can only turn a failure into a success — but
    it is no longer evidence of anything about shared libraries.
    """
    global _MERCARI_API_COOLDOWN_UNTIL

    # Check the cooldown HERE and return without touching it. Doing this inside
    # the session made "in cooldown" look like a fresh failure: the caller then
    # ran the bundled retry (also in cooldown) and re-armed the timer for another
    # 900s, so the cooldown extended itself on every scrape and could never
    # expire. Observed in production 2026-08-12.
    if _MERCARI_API_CTX is None and time.time() < _MERCARI_API_COOLDOWN_UNTIL:
        wait = int(_MERCARI_API_COOLDOWN_UNTIL - time.time())
        return {}, f'search API in cooldown for another {wait}s'

    items, err = _mercari_api_session(terms, max_rows, bundled=False)
    if not err:
        return items, None

    already_bundled = _env_flag('MERCARI_BUNDLED_CHROMIUM', False)
    if _env_flag('MERCARI_CHROMIUM_FALLBACK', True) and not already_bundled:
        _emit(f"[mercari] real Chrome path failed ({err}) — retrying on bundled Chromium")
        items2, err2 = _mercari_api_session(terms, max_rows, bundled=True)
        if not err2:
            _emit("[mercari] bundled Chromium worked. Set MERCARI_BUNDLED_CHROMIUM=1 "
                  "to skip the failing attempt entirely.", 'info')
            return items2, None
        items, err = items2, f'{err} (bundled retry: {err2})'

    # Both engines failed. Back off so a broken environment costs one attempt
    # per cooldown instead of one per scrape, blocking every other platform.
    try:
        cool = int(os.getenv('MERCARI_API_COOLDOWN_SEC', '900'))
    except ValueError:
        cool = 900
    _MERCARI_API_COOLDOWN_UNTIL = time.time() + max(0, cool)
    return items, f'{err} — not retrying for {cool}s'


def _mercari_api_session(terms, max_rows, bundled=False):
    """
    Search Mercari for several terms over ONE browser session.

    Returns {term: [items]} for whatever succeeded, plus an error string or None.
    Terms are batched deliberately: the browser launch and the bootstrap are the
    only meaningful costs left, so paying them once for many terms is the point.
    """
    global _MERCARI_API_CTX, _MERCARI_API_COOLDOWN_UNTIL
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as e:
        return {}, f'patchright missing ({e})'

    results = {}
    # Separate profiles per engine: a profile carries the cf_clearance bound to
    # the browser that earned it, so reusing one across engines invites a
    # challenge instead of avoiding one.
    profile_dir = _mercari_profile_dir() + ('_chromium' if bundled else '')

    clean_ua = _patchright_clean_ua()
    args = ['--window-size=1920,1080']
    if _env_flag('MERCARI_HEADLESS', True):
        args.insert(0, '--headless=new')
    if _env_flag('MERCARI_CONTAINER_FLAGS', not sys.platform.startswith('win')):
        args += ['--no-sandbox', '--disable-dev-shm-usage']
    # Mercari's cost is MEMORY, not bandwidth — the search API is already 4.1KB
    # per term, but the capture render spawns a full Chrome process tree that
    # measured 1,125MB peak against a 512MB instance. These collapse it.
    # --single-process is deliberately NOT here: it is the biggest single saving
    # and also a well-known automation signal, so it belongs in
    # MERCARI_CHROME_ARGS where it is an explicit, measured choice.
    if _env_flag('MERCARI_LOWMEM', False):
        args += _MERCARI_LOWMEM_ARGS
    # Free-form escape hatch, space separated, so a box can be tuned from the
    # environment without a deploy.
    extra = (os.getenv('MERCARI_CHROME_ARGS') or '').strip()
    if extra:
        args += [a for a in extra.split() if a]
    args.append(f'--user-agent={clean_ua}')

    launch_kwargs = dict(headless=False, no_viewport=True, args=args,
                         user_agent=clean_ua, **_mercari_chrome_kwargs(bundled=bundled))
    proxy_url = _get_proxy('mercari')
    if proxy_url:
        launch_kwargs['proxy'] = _pw_proxy_dict(proxy_url)

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(profile_dir, **launch_kwargs)
        except Exception as e:
            return {}, f'patchright launch failed: {e}'
        try:
            # Mercari is the one platform that never blocked heavy requests, so
            # a page render also pulled 468KB of images, 123KB of fonts and 46KB
            # of CSS. Only the capture render loads a real page now, but that
            # render is the expensive one, so it is worth blocking there too.
            if _env_flag('PW_BLOCK_HEAVY_REQUESTS', True):
                _pw_block_heavy_requests(ctx)
            page = ctx.new_page()
            _attach_byte_counter(page, 'mercari')

            # Capture under the lock, double-checked. Measured with 10 concurrent
            # scrapes: all ten found the cache empty and each paid its own ~3.4MB
            # capture render — a thundering herd that also drove peak memory to
            # 5.4GB because ten heavyweight renders overlapped. Serialising it
            # means one render per process instead of one per worker, and the
            # nine that wait are cheaper than the nine that rendered.
            api_ctx = _MERCARI_API_CTX
            captured_here = False
            if api_ctx is None:
                with _MERCARI_API_LOCK:
                    api_ctx = _MERCARI_API_CTX
                    if api_ctx is None:
                        # Disk before browser. A process restart otherwise pays a
                        # full capture render, and on a small instance restarts
                        # are routine — that render is 3MB and the entire reason
                        # Mercari costs more than 4KB per term.
                        api_ctx = _load_persisted_mercari_ctx()
                        if api_ctx is None:
                            api_ctx = _mercari_api_capture(page)
                            if not api_ctx:
                                # Cooldown is the CALLER's decision — setting it
                                # here would make a real-Chrome failure block the
                                # bundled Chromium retry meant to rescue it.
                                return {}, 'could not capture searchFacetQuery context'
                            captured_here = True
                            _persist_mercari_ctx(api_ctx)
                        _MERCARI_API_CTX = api_ctx

            if not captured_here:
                # Capturing already left this page on a Mercari search URL. If we
                # reused a cached context, the page is still blank, so put it on
                # the origin cheaply — fetch() needs the page's cookies and
                # origin, not its content. Same trick as the OfferUp warm-up.
                try:
                    page.goto('https://www.mercari.com/robots.txt',
                              wait_until='domcontentloaded', timeout=30000)
                except Exception as e:
                    _emit(f"[mercari] bootstrap navigation failed: {e}")

            for term in terms:
                url = _mercari_api_url(api_ctx, term, max_rows)
                if not url:
                    return results, 'unexpected variables shape'
                for attempt in (1, 2):
                    try:
                        res = page.evaluate(_MERCARI_REPLAY_JS, [url, api_ctx['headers']])
                    except Exception as e:
                        return results, f'replay failed: {type(e).__name__}: {e}'
                    if res.get('status') == 200:
                        try:
                            payload = json.loads(res.get('text') or '{}')
                        except Exception:
                            break
                        if not payload.get('errors'):
                            results[term] = _mercari_items_from_api(payload)
                            break
                    # A dead token (401/403) or a rotated persisted-query hash
                    # both land here. Re-capture once — that is the whole
                    # self-healing story, and it costs one page render.
                    if attempt == 1:
                        _emit(f"[mercari] api replay returned {res.get('status')} — "
                              "re-capturing the search context")
                        fresh = _mercari_api_capture(page, term)
                        if not fresh:
                            return results, 'api replay failed and re-capture failed'
                        api_ctx = fresh
                        with _MERCARI_API_LOCK:
                            _MERCARI_API_CTX = fresh
                        # Re-persist, or every process would keep reloading the
                        # dead token from disk and re-capturing it in turn.
                        _persist_mercari_ctx(fresh)
                        url = _mercari_api_url(api_ctx, term, max_rows) or url
                        continue
                    return results, f"api replay HTTP {res.get('status')}"
            return results, None
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def _mercari_fetch_with_patchright(url):
    """
    Fetch Mercari via patchright (an undetected Playwright fork).

    Mercari sits behind Cloudflare AND rejects its own /v1/api with 403 for
    automated browsers, so a blocked page renders a genuine "0 results" rather
    than a visible block. Standard Playwright cannot get past this at all.

    Measured requirements — dropping any one returns 'Just a moment...':
      * patchright rather than playwright  (patches the CDP leaks CF detects)
      * channel='chrome'    — real Chrome, not bundled Chromium
      * persistent context  — retains the cf_clearance cookie between runs
      * a UA without 'HeadlessChrome'      (see _patchright_clean_ua)
      * '--headless=new' passed as an arg with headless=False, so patchright
        does not also inject the old-headless flags

    Headless IS viable with the UA masked, so no virtual display is needed.
    A sticky proxy is strongly preferred (MERCARI_PROXY_URL): Cloudflare binds
    clearance to one IP, so a rotating pool discards it on every request.
    """
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        _emit("[mercari] patchright not installed in this process — "
              "run: .venv/Scripts/python.exe -m pip install patchright")
        return ''

    try:
        settle_ms = int(os.getenv('MERCARI_SETTLE_MS', '4000'))
    except ValueError:
        settle_ms = 4000
    try:
        cf_wait_s = int(os.getenv('MERCARI_CF_WAIT_SEC', '45'))
    except ValueError:
        cf_wait_s = 45

    # Per-worker, for the same SingletonLock reason as the API path.
    profile_dir = _mercari_profile_dir()

    # Headless is requested via the Chrome flag rather than Playwright's
    # headless=True, so patchright doesn't layer old-headless flags on top.
    args = ['--window-size=1920,1080']
    if _env_flag('MERCARI_HEADLESS', True):
        args.insert(0, '--headless=new')
    # Container flags. Render has no root (so Chrome's sandbox cannot initialise)
    # and a 64MB /dev/shm (so Chrome's default shared-memory use fails). Without
    # these the browser LAUNCHES but its renderers never start, and page.goto
    # then hangs until the timeout — which reads exactly like Cloudflare blocking
    # us, and sent debugging toward proxies and stealth instead of flags.
    # render-build.sh verifies the unpacked Chrome with --no-sandbox for this
    # same reason; the runtime simply was not passing it.
    #
    # Safe for the four-part config: neither flag is observable from page JS, so
    # neither changes what Cloudflare fingerprints. Defaults on everywhere except
    # Windows, where the sandbox works and the local setup is known good.
    if _env_flag('MERCARI_CONTAINER_FLAGS', not sys.platform.startswith('win')):
        args += ['--no-sandbox', '--disable-dev-shm-usage']
    clean_ua = _patchright_clean_ua()
    args.append(f'--user-agent={clean_ua}')

    launch_kwargs = dict(
        headless=False,
        no_viewport=True,
        args=args,
        user_agent=clean_ua,
        **_mercari_chrome_kwargs(),
    )
    proxy_url = _get_proxy('mercari')
    if proxy_url:
        launch_kwargs['proxy'] = _pw_proxy_dict(proxy_url)

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(profile_dir, **launch_kwargs)
        except Exception as e:
            _emit(f"[mercari patchright launch error] {e}")
            return ''
        page = ctx.new_page()
        _attach_byte_counter(page, 'mercari')
        try:
            try:
                nav_timeout_ms = int(os.getenv('MERCARI_NAV_TIMEOUT_MS', '45000'))
            except ValueError:
                nav_timeout_ms = 45000

            navigated = True
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=nav_timeout_ms)
            except Exception as e:
                navigated = False
                _emit(f"[mercari nav warning] {e}")

            if not navigated:
                # Waiting on Cloudflare cannot help when navigation never
                # completed — there is no page to clear. This path used to cost
                # nav_timeout + cf_wait + settle = ~94s PER TERM to return
                # nothing, making a broken Mercari more expensive than every
                # working platform combined and delaying Facebook behind it.
                _emit("[mercari] navigation never completed — skipping the "
                      "Cloudflare wait and settle")
                return ''

            # Cloudflare's interstitial clears itself for an undetected browser;
            # poll the title until it stops saying "Just a moment...".
            deadline = time.time() + max(5, cf_wait_s)
            while time.time() < deadline:
                try:
                    if 'moment' not in (page.title() or '').lower():
                        break
                except Exception:
                    pass
                page.wait_for_timeout(3000)
            else:
                print("[mercari] Cloudflare challenge did not clear", flush=True)

            page.wait_for_timeout(settle_ms)
            return page.content()
        except Exception as e:
            _emit(f"[mercari patchright error] {e}")
            try:
                return page.content()
            except Exception:
                return ''
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def _mercari_fetch_with_playwright(url):
    """Fetch Mercari search page using Playwright headless Chromium."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError as e:
        _emit(f"[mercari] Playwright not importable ({e}). "
              f"THIS BACKEND IS RUNNING: {sys.executable} — "
              "it must be <project>/.venv/Scripts/python.exe")
        return ''
    try:
        nav_wait_ms = int(os.getenv('MERCARI_PLAYWRIGHT_WAIT_MS', '6000'))
    except ValueError:
        nav_wait_ms = 6000
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(**_pw_launch_kwargs('mercari'))
        except Exception as e:
            _emit(f"[mercari] chromium launch failed: {e}")
            return ''
        ctx = _pw_new_context(browser, stealth=True, platform='mercari')
        page = ctx.new_page()
        _attach_byte_counter(page, 'mercari')
        try:
            # Mercari holds long-lived connections open, so 'networkidle' never
            # fires and the nav times out even though the page rendered fine.
            # Wait for the DOM instead, then for the listing anchors to appear.
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                _emit(f"[mercari nav warning] {e}")
            try:
                page.wait_for_selector("a[href*='/item/']", timeout=nav_wait_ms)
            except PWTimeout:
                pass
            # Listings hydrate client-side; give React a beat to render.
            page.wait_for_timeout(2500)
            return page.content()
        except Exception as e:
            _emit(f"[mercari playwright error] {e}")
            try:
                return page.content()
            except Exception:
                return ''
        finally:
            browser.close()


def _offerup_collect_from_html(html_text):
    out = []
    if not html_text:
        return out
    try:
        soup = BeautifulSoup(html_text, 'html.parser')
        for a in soup.select("a[href*='/item/']")[:200]:
            try:
                href = (a.get('href') or '').strip()
                if not href or '/item/' not in href:
                    continue
                link = href if href.startswith('http') else f"https://offerup.com{href}"
                aria = (a.get('aria-label') or '').strip()
                # aria-label ends with " $XX  in City, ST " — strip that suffix for the title
                clean_aria = re.sub(r'\s+\$[\d,.]+\s.*$', '', aria).strip()
                title = clean_aria or (a.get_text(" ", strip=True) or '').strip()
                text_blob = aria or (a.get_text(" ", strip=True) or '')
                price = extract_price(text_blob)
                if not (title and price):
                    continue
                image_url = None
                img = a.find('img')
                if img:
                    image_url = img.get('src') or img.get('data-src')
                out.append({
                    'title': title,
                    'price': price,
                    'link': link,
                    'image_url': image_url,
                })
            except Exception:
                continue
    except Exception:
        pass
    return out


def _parse_offerup_last_updated_ago(text):
    """
    OfferUp item page shows e.g. 'Last updated 10 days ago' (relative, not true posted time).
    Returns an approximate UTC datetime = now - delta for sorting / age gates.
    """
    if not text:
        return None
    blob = re.sub(r'\s+', ' ', str(text).strip())
    m = re.search(
        r'last\s+updated\s+(\d+)\s+'
        r'(second|seconds|minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago',
        blob,
        re.IGNORECASE,
    )
    if not m:
        return None
    n = int(m.group(1))
    u = m.group(2).lower()
    if u.startswith('second'):
        delta = timedelta(seconds=n)
    elif u.startswith('minute'):
        delta = timedelta(minutes=n)
    elif u.startswith('hour'):
        delta = timedelta(hours=n)
    elif u.startswith('day'):
        delta = timedelta(days=n)
    elif u.startswith('week'):
        delta = timedelta(days=7 * n)
    elif u.startswith('month'):
        delta = timedelta(days=30 * n)
    elif u.startswith('year'):
        delta = timedelta(days=365 * n)
    else:
        return None
    return datetime.now(timezone.utc) - delta


def _offerup_listed_at_from_item_page(link, polite_delay_sec):
    """Off by default — fetches each item page for posted date. Enable with OFFERUP_FETCH_ITEM_POSTED=true."""
    if not _env_flag('OFFERUP_FETCH_ITEM_POSTED', False):
        return None
    if not link or '/item/' not in link:
        return None
    try:
        html = _offerup_fetch_with_playwright(link, stealth=False)
        time.sleep(max(0.0, polite_delay_sec))
        if not html:
            return None
        soup = BeautifulSoup(html, 'html.parser')
        for node in soup.find_all(string=re.compile(r'last\s+updated', re.I)):
            parent = getattr(node, 'parent', None)
            block = parent
            for _ in range(5):
                if block is None:
                    break
                t = block.get_text(' ', strip=True)
                dt = _parse_offerup_last_updated_ago(t)
                if dt:
                    return dt
                block = getattr(block, 'parent', None)
        m = re.search(
            r'last\s+updated\s+(\d+)\s+'
            r'(second|seconds|minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago',
            html,
            re.IGNORECASE,
        )
        if m:
            return _parse_offerup_last_updated_ago(m.group(0))
    except Exception:
        return None
    return None


def scrape_mercari_for_user(user_id, zip_code, search_radius, search_terms, exclusions, ai_enabled, ai_strictness,
                            debug=False, log_callback=None, known_links=None):
    listings = []
    _set_active_log(user_id, log_callback)
    if log_callback:
        log_callback(user_id, "Waking up Mercari scraper...", "info")

    # If the API capture just failed, the browser cannot reach Mercari — and the
    # page-rendering fallback below uses that SAME browser, only far more slowly
    # (a 45s navigation timeout per URL variant per term). Falling back then
    # burns minutes to reach a conclusion we already have.
    #
    # That mattered far more than it looks: listings are saved once, AFTER every
    # platform finishes, so a Mercari stall discarded everything Craigslist,
    # OfferUp and Facebook had already found. Observed in production 2026-08-12
    # as a scan that "froze" and saved nothing. Skipping costs Mercari for one
    # cooldown; not skipping cost the entire scrape.
    cooling = _mercari_in_cooldown()
    if cooling:
        if log_callback:
            mins, secs = divmod(cooling, 60)
            log_callback(
                user_id,
                f"Mercari is unreachable from this server — skipping it for "
                f"{mins}m {secs:02d}s so it cannot delay your other marketplaces.",
                'info',
            )
        return listings

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    max_age_days = int(os.getenv('MAX_LISTING_AGE_DAYS', '7'))
    try:
        mercari_detail_delay = float(os.getenv('MERCARI_POSTED_PAGE_DELAY_SEC', '0.35'))
    except ValueError:
        mercari_detail_delay = 0.35
    try:
        mercari_max_rows = int(os.getenv('MERCARI_MAX_ROWS_PER_TERM', '30'))
    except ValueError:
        mercari_max_rows = 30

    # One browser session for every term, hitting the search API directly
    # instead of rendering a 4.2MB page per term. See _mercari_fetch_via_api.
    # Defaults ON. Any failure falls through to the page path below, so the
    # downside is bounded — and the page path is what returns 0 on Render today,
    # so this cannot regress production. Set MERCARI_USE_API=0 to force the old
    # path. (The cost of a FAILED api attempt is a capture render plus the
    # fallback, i.e. roughly double; the cost of a successful one is 4KB against
    # 963KB, which is the trade being made.)
    api_items_by_term = {}
    if _env_flag('MERCARI_USE_API', True):
        # Terms the scheduler already fetched this cycle for somebody — usually
        # everything, since it prefetches the union of all due users' terms.
        # A hit costs no browser at all.
        pending = []
        shared_hits = 0
        for t in search_terms.keys():
            cached = _mercari_cycle_items(t)
            if cached is not None:
                api_items_by_term[t] = cached
                shared_hits += 1
            else:
                pending.append(t)
        if shared_hits and debug and log_callback:
            log_callback(user_id,
                         f"Mercari: {shared_hits}/{len(search_terms)} terms served "
                         "from this cycle's shared fetch", 'info')

        api_err = None
        if pending:
            fetched, api_err = _mercari_fetch_via_api(pending, mercari_max_rows)
            api_items_by_term.update(fetched or {})
        if api_err:
            _emit(f"[mercari] search API unavailable ({api_err}) — "
                  "falling back to page rendering")
            if debug and log_callback:
                log_callback(user_id,
                             f"Mercari: search API unavailable ({api_err[:80]}) — "
                             "using the slower page path.", 'info')
        elif debug and log_callback:
            total = sum(len(v) for v in api_items_by_term.values())
            log_callback(user_id,
                         f"Mercari: {total} rows across {len(api_items_by_term)} "
                         "terms via search API", 'info')

    for term in search_terms.keys():
        if not is_user_active(user_id):
            if debug and log_callback:
                log_callback(user_id, "Mercari stopped by user.", "info")
            return listings
        q = quote(term, safe='')
        urls = [
            f"https://www.mercari.com/search/?keyword={q}",
            f"https://www.mercari.com/search/?keyword={q}&sortBy=2",
        ]
        try:
            if term in api_items_by_term:
                raw_items, fetch_mode = api_items_by_term[term], 'search API'
            else:
                raw_items, fetch_mode = _mercari_collect_from_search_urls(
                    urls, headers, log_callback=log_callback, user_id=user_id, term=term, debug=debug,
                )
            if debug and log_callback:
                log_callback(
                    user_id,
                    f"Mercari '{term}': scanned {len(raw_items)} rows ({fetch_mode})",
                    'info',
                )

            for row in raw_items[:max(1, mercari_max_rows)]:
                try:
                    title = row['title']
                    price = row['price']
                    link = row['link']
                    image_url = row.get('image_url')

                    # Skip before any per-item network cost: the dedup pass would
                    # drop this later anyway, after we had paid for Vision.
                    if known_links and link in known_links:
                        continue

                    listed_at = row.get('listed_at')
                    if listed_at is None:
                        listed_at = _mercari_listed_at_from_item_page(
                            link, headers, mercari_detail_delay
                        )
                    listed_for_age = listed_at.isoformat() if isinstance(listed_at, datetime) else listed_at
                    if listed_for_age and not _is_recent_timestamp(listed_for_age, max_age_days):
                        continue
                    meets_threshold, matched_term, max_price = check_price_threshold(title, price, search_terms)
                    if not meets_threshold:
                        continue
                    if is_excluded(title, price, exclusions, search_terms, matched_term):
                        continue

                    if not check_image_with_ai(
                        image_url, ai_enabled, ai_strictness, debug, log_callback, user_id, platform_name='mercari',
                        matched_term=matched_term,
                        term_exclusions=(search_terms.get(matched_term) or {}).get('exclusions')):
                        continue

                    listings.append({
                        'title': title,
                        'price': price,
                        'link': link,
                        'platform': 'Mercari',
                        'console_type': matched_term,
                        'threshold': max_price,
                        'image_url': image_url,
                        'listed_at': listed_at,
                        'location': f'Mercari · {zip_code} ({search_radius} mi)'
                    })
                except Exception:
                    continue
            time.sleep(1)
        except Exception:
            pass
    if debug and log_callback:
        log_callback(user_id, f"Mercari complete: {len(listings)} candidate matches", "info")
    return listings


def _tier_from_db_row(row):
    t = (row.get('plan_tier') or '').strip().lower()
    if t in ('basic', 'pro'):
        return t
    return 'pro' if row.get('is_pro') else 'inactive'


def _coerce_platforms_dict(raw):
    """user_settings.platforms is jsonb (dict) or occasionally a JSON string — always return a dict."""
    if raw is None:
        return {'craigslist': True}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {'craigslist': True}
    if not isinstance(raw, dict) or not raw:
        return {'craigslist': True}
    return raw


# Scan cadence is a plan feature, not a per-platform limit. Facebook used to
# carry its own hourly floor because scrapes were billed per API call; the
# no-login Playwright path costs nothing per scrape, so that floor is gone and
# Facebook now follows the same tier cadence as every other marketplace.
PLAN_INTERVAL_FLOOR_MINUTES = {'pro': 5, 'basic': 10}
PLAN_INTERVAL_OPTIONS = {
    'pro': [5, 10, 15, 30, 60],
    'basic': [10, 15, 30, 60],
}
DEFAULT_INTERVAL_FLOOR_MINUTES = 10


def interval_floor_for_tier(tier):
    return PLAN_INTERVAL_FLOOR_MINUTES.get(
        (tier or '').strip().lower(), DEFAULT_INTERVAL_FLOOR_MINUTES
    )


def _effective_check_interval_minutes_for_user(row_or_cfg):
    """Clamp the user's chosen interval up to their plan's floor."""
    try:
        stored = int(row_or_cfg.get('check_interval_minutes') or DEFAULT_INTERVAL_FLOOR_MINUTES)
    except (TypeError, ValueError):
        stored = DEFAULT_INTERVAL_FLOOR_MINUTES
    tier = (row_or_cfg.get('plan_tier') or _tier_from_db_row(row_or_cfg)).strip().lower()
    return max(stored, interval_floor_for_tier(tier))


# Priority terms. Keep in sync with app.py and frontend/src/App.js.
#
# Cadence is per TERM now (user_search_terms.interval_minutes, migration 012),
# not one interval per user. A term runs at whatever rate the user picked for
# it, clamped up to the fastest rate their plan allows.
# PLAN_MAX_PRIORITY_TERMS is how many terms may sit at that fastest tier.
PLAN_MAX_PRIORITY_TERMS = {'pro': 3, 'basic': 0}

# Floor for a term that is NOT at the plan's fastest tier. 10 everywhere now: a
# Pro user's ordinary terms run at the same 10 minutes a Basic user's do, and
# the only thing priority buys is the right to put 5 on a few of them. This was
# 15 for Pro, which meant the paid plan's default cadence was SLOWER than the
# cheaper one's.
PLAN_STANDARD_FLOOR_MINUTES = {'pro': 10}

# What a term scans at when it carries no interval of its own: a row written
# before migration 012, or one saved by a client that does not send the field.
DEFAULT_TERM_INTERVAL_MINUTES = 10


def max_priority_for_tier(tier):
    return PLAN_MAX_PRIORITY_TERMS.get((tier or '').strip().lower(), 0)


def standard_floor_for_tier(tier):
    """
    Floor for a term that is not at the plan's fastest tier.

    Tiers with no priority feature fall through to their ordinary floor, which
    is the same 10 minutes — so this only ever differs for a tier that sells a
    faster rate than its own default.
    """
    t = (tier or '').strip().lower()
    return PLAN_STANDARD_FLOOR_MINUTES.get(t, interval_floor_for_tier(t))


def term_interval_minutes(term_cfg, user_cfg):
    """
    How often THIS term should scan, in minutes.

    Reads the term's own interval_minutes (migration 012). The user's choice
    wins whenever it is slower — someone who picked 30 wants 30, not 5. The
    floor only stops a term going FASTER than its plan allows.

    Only the tier floor is applied here. How MANY terms may sit at the fastest
    rate is capped on save in app.py, which is the one place that can see the
    whole term set and can keep the terms already at that rate rather than
    demoting an arbitrary one. This mirrors how is_priority worked: the scraper
    clamps by tier, the write path counts.
    """
    tier = (user_cfg.get('plan_tier') or _tier_from_db_row(user_cfg) or '').strip().lower()
    try:
        every = int((term_cfg or {}).get('interval_minutes')
                    or DEFAULT_TERM_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        every = DEFAULT_TERM_INTERVAL_MINUTES
    # A tier that sells the fast rate may reach it; one that does not is held at
    # its standard floor, so a forged 5 on Basic still scans at 10.
    floor = (interval_floor_for_tier(tier) if max_priority_for_tier(tier) > 0
             else standard_floor_for_tier(tier))
    return max(every, floor)


def due_terms_for_user(search_terms, user_cfg, now=None):
    """
    Split a user's terms into (due_now, next_due_epoch).

    The scheduler used to ask one question per user. With priority terms a user
    can be due for their 5-minute terms and not their 15-minute ones, so the
    question moves to the term and the user is due when ANY term is.

    Returns the terms to scrape this cycle and, when nothing is due, the epoch
    of the soonest one, so the caller can report a real countdown instead of
    going quiet.
    """
    now = now or time.time()
    due = {}
    soonest = None
    for term, cfg in (search_terms or {}).items():
        interval_s = term_interval_minutes(cfg, user_cfg) * 60
        last = float((cfg or {}).get('last_scraped_ts') or 0.0)
        ready_at = last + interval_s
        if now >= ready_at:
            due[term] = cfg
        elif soonest is None or ready_at < soonest:
            soonest = ready_at
    return due, soonest


def stamp_terms_scraped(user_id, terms):
    """Mark these terms scanned. Best-effort: a failure must not fail the scrape."""
    if not user_id or not terms:
        return
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_search_terms SET last_scraped_at = NOW() "
            "WHERE user_id = %s AND search_term = ANY(%s)",
            (user_id, list(terms)),
        )
        conn.commit()
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        print(f"[terms] could not stamp last_scraped_at for {str(user_id)[:8]}: {e}", flush=True)
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            conn.close()


_FB_REGIONS = None
_FB_ZIP_CACHE = {}


def _fb_regions():
    """
    Facebook's valid marketplace region slugs with coordinates, built by
    scripts/build_fb_regions.py from Facebook's own directory + region pages.

    Slugs are irregular ('la', 'sac', 'philly', 'bowling-green-ky') and cannot
    be derived from a city name — a guessed slug silently redirects to whatever
    region Facebook infers from the exit IP, which is how scans ended up
    returning listings from the wrong state.
    """
    global _FB_REGIONS
    if _FB_REGIONS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fb_regions.json')
        try:
            with open(path, 'r', encoding='utf-8') as f:
                _FB_REGIONS = [r for r in json.load(f) if r.get('lat') is not None]
        except Exception as e:
            print(f"[facebook] region table unavailable ({e}); "
                  "falling back to IP-inferred location", flush=True)
            _FB_REGIONS = []
    return _FB_REGIONS


def _zip_to_latlon(zip_code):
    """Zip -> (lat, lon) via zippopotam.us. Cached; None when unavailable."""
    z = re.sub(r'\D', '', str(zip_code or ''))[:5]
    if len(z) != 5:
        return None
    if z in _FB_ZIP_CACHE:
        return _FB_ZIP_CACHE[z]
    try:
        r = requests.get(f'http://api.zippopotam.us/us/{z}', timeout=12)
        if r.ok:
            p = r.json()['places'][0]
            _FB_ZIP_CACHE[z] = (float(p['latitude']), float(p['longitude']))
        else:
            _FB_ZIP_CACHE[z] = None
    except Exception:
        _FB_ZIP_CACHE[z] = None
    return _FB_ZIP_CACHE[z]


def _haversine_miles(lat1, lon1, lat2, lon2):
    import math
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


_CITY_CACHE = {}
_CITY_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'city_coords_cache.json')
_CITY_CACHE_LOADED = False
# Guards both the dict and the file. Every concurrent scrape geocodes cities, so
# above one worker this is genuinely contended.
_CITY_CACHE_LOCK = threading.RLock()


def _load_city_cache():
    global _CITY_CACHE, _CITY_CACHE_LOADED
    with _CITY_CACHE_LOCK:
        if _CITY_CACHE_LOADED:
            return
        _CITY_CACHE_LOADED = True
        try:
            if os.path.isfile(_CITY_CACHE_PATH):
                with open(_CITY_CACHE_PATH, 'r', encoding='utf-8') as f:
                    _CITY_CACHE = {k: tuple(v) if v else None for k, v in json.load(f).items()}
        except Exception:
            _CITY_CACHE = {}


_CITY_CACHE_DIRTY = False
_CITY_CACHE_LAST_SAVE = 0.0


def _save_city_cache(force=False):
    """
    Persist the geocode cache. Safe to call from several scrapes at once.

    Debounced. This used to rewrite the whole file on EVERY cache miss, so a
    term returning 49 listings in unfamiliar cities did 49 full serialise +
    write + rename cycles on top of 49 Nominatim round trips. Now it writes at
    most once every CITY_CACHE_SAVE_INTERVAL_SEC, and the data is only a cache —
    losing the last few seconds of it costs one re-geocode, not correctness.

    Two separate hazards above one worker, both fixed here:
      * open(path,'w') truncates before writing, so a second writer starting
        mid-write leaves a half-file that _load_city_cache then discards — the
        cache silently empties and every city gets re-geocoded at 1.05s each.
      * json.dump iterates the dict; another thread inserting during that
        iteration raises "dictionary changed size during iteration".
    Snapshot under the lock, then write to a temp file and rename, so a reader
    only ever sees a complete file.
    """
    global _CITY_CACHE_DIRTY, _CITY_CACHE_LAST_SAVE
    with _CITY_CACHE_LOCK:
        _CITY_CACHE_DIRTY = True
        try:
            interval = float(os.getenv('CITY_CACHE_SAVE_INTERVAL_SEC', '20'))
        except ValueError:
            interval = 20.0
        if not force and (time.time() - _CITY_CACHE_LAST_SAVE) < interval:
            return
        _CITY_CACHE_LAST_SAVE = time.time()
        _CITY_CACHE_DIRTY = False

        snapshot = {k: list(v) if v else None for k, v in _CITY_CACHE.items()}
        tmp = f'{_CITY_CACHE_PATH}.{os.getpid()}.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(snapshot, f, indent=0)
            os.replace(tmp, _CITY_CACHE_PATH)      # atomic on POSIX and Windows
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


_GEOCODE_LOCAL = threading.local()


def reset_geocode_budget():
    """Called at the start of each user's scrape."""
    _GEOCODE_LOCAL.spent = 0


def _geocode_budget_spent():
    """True once this scrape has used its allowance of new-city lookups."""
    try:
        budget = int(os.getenv('GEOCODE_MAX_PER_SCRAPE', '12'))
    except ValueError:
        budget = 12
    if budget <= 0:
        return False                      # 0 disables the cap
    spent = getattr(_GEOCODE_LOCAL, 'spent', 0)
    if spent >= budget:
        return True
    _GEOCODE_LOCAL.spent = spent + 1
    return False


def city_to_latlon(city_state):
    """
    'Sacramento, CA' -> (lat, lon), cached to disk.

    Cities repeat constantly inside one metro, so after the first scrape this is
    almost always a cache hit and costs nothing. A miss returns None and the
    caller keeps the listing — never drop a listing just because geocoding failed.
    """
    key = (city_state or '').strip().lower()
    if not key or ',' not in key:
        return None
    _load_city_cache()
    if key in _CITY_CACHE:
        return _CITY_CACHE[key]

    # Budget the misses. Nominatim's courtesy limit is 1 request/second, so each
    # unknown city costs ~1.05s of pure sleep — and a broad term like "iphone 14"
    # returns ~50 listings across ~40 unfamiliar cities. Measured in production
    # 2026-08-12: two such terms added 44s and 40s to a single OfferUp run,
    # making geocoding the largest cost on the platform.
    #
    # A miss returns None and the CALLER KEEPS THE LISTING (unknown city is
    # never a reason to drop one), so exhausting the budget degrades distance
    # filtering slightly for this scrape rather than losing results. The cache is
    # permanent, so each scrape resolves a few more and the budget stops binding
    # once a user's metro is covered.
    if _geocode_budget_spent():
        return None
    coords = None
    try:
        r = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': f'{city_state}, USA', 'format': 'json', 'limit': 1, 'countrycodes': 'us'},
            headers={'User-Agent': 'PixelFlip/1.0 (support@pixelflip.app)'},
            timeout=15,
        )
        if r.ok:
            js = r.json()
            if js:
                coords = (float(js[0]['lat']), float(js[0]['lon']))
        time.sleep(1.05)   # Nominatim courtesy limit
    except Exception:
        coords = None
    _CITY_CACHE[key] = coords
    _save_city_cache()
    return coords


_FB_CARD_LOC = re.compile(r',\s*([A-Za-z][A-Za-z .\'\-]{1,28},\s?[A-Z]{2})\s*,\s*listing\b')
_FB_ALT_LOC = re.compile(r'\bin\s+([A-Za-z][A-Za-z .\'\-]{1,28},\s?[A-Z]{2})\s*$')


def _fb_location_from_card(anchor):
    """
    Pull 'City, ST' off a marketplace card.

    Facebook exposes it three ways and they don't always all appear:
      aria-label : 'Ds lite, $60, Sacramento, CA, listing 1944693606186398'
      img alt    : 'Ds lite in Sacramento, CA'
      card text  : '... | Ds lite | Sacramento, CA'
    """
    aria = (anchor.get('aria-label') or '').strip()
    m = _FB_CARD_LOC.search(aria)
    if m:
        return m.group(1).strip()
    img = anchor.find('img')
    alt = ((img.get('alt') if img else '') or '').strip()
    m = _FB_ALT_LOC.search(alt)
    if m:
        return m.group(1).strip()
    parts = [p.strip() for p in (anchor.get_text('|', strip=True) or '').split('|') if p.strip()]
    if parts and re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,28},\s?[A-Z]{2}", parts[-1]):
        return parts[-1]
    return None


def fb_region_for_zip(zip_code):
    """Nearest Facebook marketplace region to a zip. Returns (slug, name, miles)."""
    regions = _fb_regions()
    if not regions:
        return None
    coords = _zip_to_latlon(zip_code)
    if not coords:
        return None
    lat, lon = coords
    best = min(regions, key=lambda r: _haversine_miles(lat, lon, r['lat'], r['lon']))
    return best['slug'], best.get('name', best['slug']), _haversine_miles(lat, lon, best['lat'], best['lon'])


# ===========================
# FACEBOOK MARKETPLACE (Playwright — free, proxy-supported)
#
# Optional env vars:
#   FB_COOKIES_FILE         — path to a JSON session file (see capture_fb_cookies.py)
#   FB_PROXY_URL            — single sticky proxy for FB only; NEVER the rotating pool
#   FB_NO_PROXY             — force the real egress IP, ignoring FB_PROXY_URL
#   FB_MAX_ROWS_PER_TERM    — listings to parse per keyword (default 40)
#   FB_MAX_LISTING_AGE_DAYS — age gate in days (default 45)
#   FB_PLAYWRIGHT_WAIT_MS   — selector wait after navigation (default 8000)
#   FB_SCROLL_COUNT         — scroll-to-load passes before parsing (default 4);
#                             ignored by the lean path, which loads no JS
#   FB_LEAN_FETCH           — 1 to block JS and parse the server-rendered JSON
#                             instead of the DOM. ~93% less proxy bandwidth for
#                             the same listings. Default off. See
#                             _fb_collect_from_json.
#   FB_LEAN_FALLBACK        — on a lean fetch that finds no listing JSON, spend
#                             one full render to distinguish a payload change
#                             from a genuinely empty result (default on)
#   FB_LEAN_SETTLE_MS       — pause after navigation before reading the document
#                             (default 0; measured unnecessary — see the function)
#
# No-login path: leave FB_COOKIES_FILE unset. Logged-out Marketplace shows a
# login modal over the results; the fetcher dismisses it and scrapes what
# renders behind. Works from a residential IP; server/datacenter IPs hit a
# harder wall, where FB_COOKIES_FILE (authenticated) becomes necessary.
# ===========================

_STICKY_SESSION_RE = re.compile(r'(_session-)([A-Za-z0-9]+)')


def _rotate_sticky_session(proxy_url):
    """
    Return `proxy_url` with a fresh sticky-session token, or None if it has none.

    IPRoyal pins the exit IP to a `_session-XXXX` token in the proxy username.
    Their US "residential" pool contains datacenter ASNs: draw AS11798 (Ace Data
    Centers) and Facebook serves a hard wall, draw AS6079 (RCN) and the same
    request succeeds. Identical code and credentials, different outcome purely
    by which exit you landed on — so a block is worth exactly one retry on a
    new session rather than being reported as a scraper failure.

    One retry, not a loop: if two independent exits both fail, the cause is
    almost certainly not the IP, and hammering the pool makes things worse.
    """
    if not proxy_url:
        return None
    import random  # imported locally, matching the proxy helpers above
    token = ''.join(random.choices('0123456789abcdef', k=8))
    rotated, replaced = _STICKY_SESSION_RE.subn(rf'\g<1>{token}', proxy_url)
    return rotated if replaced else None


def _fb_html_is_blocked(html, lean=False):
    # The presence of login text is NOT itself a block: logged-out Marketplace
    # renders listings behind a login modal, and _fb_fetch_with_playwright
    # dismisses it. Only the absence of actual item links means we got nothing
    # usable (hard wall, checkpoint, or empty result set).
    if not html or len(html) < 3000:
        _emit(f"[facebook debug] page size {len(html or '')} chars — too small, treating as blocked", "info")
        return True
    # A lean page has no rendered anchors by construction — React never ran — so
    # the marker is the JSON key the server streamed. Testing for item links here
    # would report every successful lean fetch as a block.
    marker = _FB_JSON_TITLE_KEY if lean else 'marketplace/item/'
    if marker.lower() not in html.lower():
        _emit(f"[facebook debug] page size {len(html):,} chars — no "
              f"{'listing JSON' if lean else 'item links'} found", "info")
        return True
    return False


def _fb_clean_title(blob, price):
    """
    FB Marketplace card text runs together as
    "{title}, ${price}, {city}, {state}, ...". Take the part before the price
    as the title; fall back to stripping the price token out of the blob.
    """
    blob = (blob or '').strip()
    if not blob:
        return ''
    # Everything up to the first '$' is the title in the observed card layout.
    idx = blob.find('$')
    candidate = blob[:idx] if idx > 0 else blob
    candidate = candidate.rstrip(' ,').strip()
    if candidate:
        return candidate
    # Fallback: drop the formatted price substring and any location tail.
    if price is not None:
        candidate = re.sub(r'\$\s*[\d,]+(?:\.\d{2})?', '', blob).strip(' ,')
    return candidate or blob


def _fb_collect_from_html(html_text):
    out = []
    if not html_text:
        return out
    try:
        soup = BeautifulSoup(html_text, 'html.parser')
        for a in soup.select("a[href*='/marketplace/item/']")[:200]:
            try:
                href = (a.get('href') or '').strip()
                if not href or '/marketplace/item/' not in href:
                    continue
                link = href if href.startswith('http') else f"https://www.facebook.com{href}"
                # Prefer explicit aria-label; else the concatenated card text.
                aria = (a.get('aria-label') or '').strip()
                text_blob = a.get_text(" ", strip=True) or ''
                price = extract_price(text_blob) or extract_price(aria)
                title = _fb_clean_title(aria or text_blob, price)
                if not (title and price):
                    continue
                img = a.find('img')
                image_url = (img.get('src') or img.get('data-src')) if img else None
                out.append({
                    'title': title, 'price': price, 'link': link, 'image_url': image_url,
                    'city': _fb_location_from_card(a),
                })
            except Exception:
                continue
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Facebook lean path: parse the server-rendered JSON, don't render the page.
#
# Measured 2026-08-11 against zip 95210 / 10001, three terms:
#
#   scripts on, 4 scrolls   1,651KB   21 listings   (production today)
#   scripts blocked           111KB   21 listings   (this path)
#
# 93% less residential bandwidth for identical output. Facebook streams the
# search results into the HTML document as Relay JSON — every listing is a
# GroupCommerceProductItem object carrying marketplace_listing_title,
# listing_price, location.reverse_geocode, delivery_types and creation_time.
# The 1.9MB of JS exists to turn that JSON into a DOM, which _fb_collect_from_html
# then turns back into the same fields. Skipping the round trip is free.
#
# Checked first: the logged-out page issues NO /api/graphql POSTs at all and
# exposes no fb_dtsg token, so the OfferUp-style API replay this was originally
# scoped as is not available on the no-login path. This is the better answer
# anyway — no CSRF token to keep fresh and no doc_id to self-heal.
# ---------------------------------------------------------------------------

# The DOM path stores the anchor's href verbatim, tracking params and all, and
# `listings` is UNIQUE (user_id, link) with dedup on exact string match. The lean
# path must emit the SAME string or every listing a user already has reads as new
# on their first lean scan — one burst of duplicate alerts each. These params are
# constant across every card Facebook renders; if they ever change, the DOM path's
# links change with them, so the two stay in step.
_FB_ITEM_LINK_SUFFIX = '/?ref=search&referral_code=null&referral_story_type=post&__tn__=!%3AD'

_FB_JSON_TITLE_KEY = '"marketplace_listing_title"'


def _fb_json_object_at(text, key_idx):
    """
    Smallest balanced {...} containing the character at key_idx.

    Facebook ships these payloads as several streamed chunks whose envelopes
    differ between page loads, so anchoring on the enclosing object of a known
    key is far more durable than walking a fixed path from the document root.
    """
    start = key_idx
    depth = 0
    while start >= 0:
        c = text[start]
        if c == '}':
            depth += 1
        elif c == '{':
            if depth == 0:
                break
            depth -= 1
        start -= 1
    if start < 0:
        return None

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _fb_collect_from_json(html_text):
    """
    Listings from the inline Relay JSON, in the shape _fb_collect_from_html returns.

    Adds three fields the DOM has no way to provide:
      * creation_time  — makes FB_MAX_LISTING_AGE_DAYS actually enforceable
                         (it is currently read and then never used, because a
                         rendered card carries no timestamp).
      * delivery_types — authoritative local-vs-shipping, so Facebook stops
                         guessing from title phrases the way OfferUp used to.
      * is_sold / is_pending — drop dead listings before they reach Vision.
    """
    out = []
    if not html_text:
        return out
    seen = set()
    for m in re.finditer(re.escape(_FB_JSON_TITLE_KEY) + r'\s*:', html_text):
        try:
            blob = _fb_json_object_at(html_text, m.start())
            if not blob:
                continue
            obj = json.loads(blob)
            title = (obj.get('marketplace_listing_title') or '').strip()
            listing_id = str(obj.get('id') or '').strip()
            if not (title and listing_id.isdigit()):
                continue
            if listing_id in seen:
                continue

            # A sold or pending listing is not actionable, and alerting on one
            # is worse than missing it.
            if obj.get('is_sold') or obj.get('is_pending') or obj.get('is_hidden'):
                continue
            if obj.get('is_live') is False:
                continue

            # Bare numeric string, like OfferUp's — extract_price expects a
            # currency symbol and would return None for every row.
            try:
                price = float((obj.get('listing_price') or {}).get('amount'))
            except (TypeError, ValueError):
                continue
            # A free listing is kept at its real price of 0, not dropped and not
            # rewritten. The DOM path gets this wrong: Facebook renders a free
            # item as the WORD "Free" followed by the struck-through old price,
            # so extract_price takes the strikethrough and a free item is stored
            # at what it used to cost. Measured 2026-08-11 — "Egg case phone
            # iPhone 8", listing_price 0.00, strikethrough $1, recorded as $1.00.
            # check_price_threshold compares only against bounds the user
            # actually set, so 0 flows through correctly; anyone who does not
            # want free items sets a min price.
            if price < 0:
                continue

            geo = ((obj.get('location') or {}).get('reverse_geocode') or {})
            city, state = geo.get('city'), geo.get('state')
            photo = (obj.get('primary_listing_photo') or {}).get('image') or {}
            delivery = [str(d).upper() for d in (obj.get('delivery_types') or [])]

            seen.add(listing_id)
            out.append({
                'title': title,
                'price': price,
                'link': f'https://www.facebook.com/marketplace/item/{listing_id}{_FB_ITEM_LINK_SUFFIX}',
                'image_url': photo.get('uri'),
                'city': f'{city}, {state}' if (city and state) else (city or None),
                'creation_time': obj.get('creation_time'),
                'is_local': 'IN_PERSON' in delivery,
                'is_shipping': any('SHIPPING' in d for d in delivery),
            })
        except Exception:
            continue
    return out


def _fb_cookie_config():
    """
    (storage_state, plain_cookies) from FB_COOKIES_FILE, or (None, None).

    capture_fb_cookies.py writes storage_state (a dict with a 'cookies' key plus
    localStorage); a plain cookie array exported from a browser extension also
    works. One definition, used by both the DOM and lean paths — a duplicated
    copy of this is exactly how a previous session broke OfferUp.
    """
    cookies_file = os.getenv('FB_COOKIES_FILE', '').strip()
    if not (cookies_file and os.path.isfile(cookies_file)):
        return None, None
    try:
        with open(cookies_file, 'r') as f:
            raw = json.load(f)
        if isinstance(raw, dict) and 'cookies' in raw:
            return cookies_file, None
        return None, (raw if isinstance(raw, list) else raw.get('cookies') or [])
    except Exception:
        return None, None


class _FbLeanSession:
    """
    One browser and one context, reused across every term in a user's scrape.

    The lean fetch made each term cheap in BYTES (111KB vs 2.1MB) but every term
    still paid a full Chromium launch. Measured 2026-08-11: 9.7s per term across
    10 terms, and the navigation itself is only a fraction of that — the rest is
    process startup. Ten terms meant ten launches.

    Only the lean path uses this. On the DOM path the page render dominates the
    time anyway, and holding a context open across ten full Marketplace renders
    is a memory risk on a 512MB box, which is the failure mode that actually
    takes the API down.

    Cold start still applies: the first navigation in a fresh process can come
    back empty, and the context is dead rather than slow, so recovery is a full
    relaunch (see _fb_fetch_with_playwright). Here that is amortised too — it
    happens once for the session rather than once per term.
    """

    def __init__(self, proxy_override=None):
        self._pw = None
        self._browser = None
        self._ctx = None
        self._proxy = proxy_override
        self._settle_ms = _fb_lean_settle_ms()

    def _open(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(**_pw_launch_kwargs('facebook'))
        storage_state, plain_cookies = _fb_cookie_config()
        self._ctx = _pw_new_context(
            self._browser, stealth=True, platform='facebook',
            storage_state=storage_state, proxy_override=self._proxy,
            block_scripts=True,
        )
        if plain_cookies:
            try:
                self._ctx.add_cookies(plain_cookies)
            except Exception:
                pass

    def _ensure(self):
        if self._browser is None:
            self._open()

    def rotate(self, proxy_override):
        """Rebuild on a different exit IP after a block."""
        self.close()
        self._proxy = proxy_override

    def fetch(self, url):
        """One term's HTML, relaunching once if the browser came back empty."""
        attempts = _fb_fetch_attempts()
        html = ''
        for attempt in range(1, attempts + 1):
            try:
                self._ensure()
                html = self._fetch_once(url)
            except Exception as e:
                _emit(f"[facebook lean] fetch failed: {type(e).__name__}: {e}")
                html = ''
            if len(html or '') >= 3000:
                return html
            if attempt < attempts:
                _emit(f"[facebook lean] attempt {attempt}/{attempts} returned "
                      f"{len(html or '')} chars — relaunching browser")
                self.close()
        return html or ''

    def _fetch_once(self, url):
        page = self._ctx.new_page()
        _attach_byte_counter(page, 'facebook')
        try:
            try:
                page.goto(url, wait_until='domcontentloaded',
                          timeout=_fb_nav_timeout_ms())
            except Exception as e:
                _emit(f"[facebook nav warning] {e}")
            # No JS loaded, so there is no modal to dismiss, nothing to scroll
            # and no anchor to wait for. domcontentloaded already guarantees the
            # document (and the listings inline in it) arrived — see
            # _fb_lean_settle_ms, which defaults to no wait at all.
            if self._settle_ms:
                page.wait_for_timeout(self._settle_ms)
            return page.content() or ''
        finally:
            try:
                page.close()
            except Exception:
                pass

    def close(self):
        for obj, name in ((self._ctx, 'context'), (self._browser, 'browser')):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._ctx = self._browser = self._pw = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _fb_fetch_attempts():
    try:
        return max(1, int(os.getenv('FB_FETCH_ATTEMPTS', '2')))
    except ValueError:
        return 2


def _fb_nav_timeout_ms():
    try:
        return int(os.getenv('FB_NAV_TIMEOUT_MS', '30000'))
    except ValueError:
        return 30000


def _fb_lean_settle_ms():
    """
    Pause after navigation before reading the document. Default 0.

    It was 2500ms on the assumption that Facebook's Relay payload streams in
    after navigation. Measured 2026-08-11 across three terms, sampling at
    0/250/500/1000/2000/3000ms: the listing count is identical at every value,
    including 0. That follows from wait_until='domcontentloaded' — the event
    does not fire until the document has been fully parsed, and the listings are
    inline in that document, so there is nothing left to arrive.

    2.5s per term was the single largest cost on the lean path, ahead of the
    browser launch. Kept as an env var in case a slow exit ever needs it.
    """
    try:
        return max(0, int(os.getenv('FB_LEAN_SETTLE_MS', '0')))
    except ValueError:
        return 0


def _fb_fetch_with_playwright(url, proxy_override=None, lean=False):
    """
    Fetch a Facebook Marketplace search page, relaunching the browser if the
    first attempt comes back empty.

    The first browser context in a fresh process reliably fails to navigate:
    goto() times out and page.content() returns ~40 characters. Retrying the
    navigation on that same context fails again — measured, twice — but a
    brand-new browser succeeds immediately. So the retry has to happen here,
    around the whole launch, rather than around goto().

    Left unhandled this cost Facebook entirely on the first scrape after every
    backend restart, and reported it as a block, which sent debugging toward
    proxies and selectors instead of process startup.
    """
    try:
        attempts = max(1, int(os.getenv('FB_FETCH_ATTEMPTS', '2')))
    except ValueError:
        attempts = 2

    html = ''
    for attempt in range(1, attempts + 1):
        html = _fb_fetch_once(url, proxy_override=proxy_override, lean=lean)
        if len(html or '') >= 3000:
            return html
        if attempt < attempts:
            _emit(f"[facebook] attempt {attempt}/{attempts} returned "
                  f"{len(html or '')} chars — relaunching browser")
    return html or ''


def _fb_fetch_once(url, proxy_override=None, lean=False):
    """
    One Playwright fetch of a Facebook Marketplace search page.

    `proxy_override` forces one specific proxy for this fetch instead of the
    configured FB_PROXY_URL — used to retry a blocked page on a fresh sticky
    session (see _rotate_sticky_session).

    `lean` blocks JS and skips every step that only exists to drive it. See
    _fb_collect_from_json.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError as e:
        _emit(f"[facebook] Playwright not importable ({e}). "
              f"THIS BACKEND IS RUNNING: {sys.executable} — "
              "it must be <project>/.venv/Scripts/python.exe")
        return ''
    try:
        nav_wait_ms = int(os.getenv('FB_PLAYWRIGHT_WAIT_MS', '8000'))
    except ValueError:
        nav_wait_ms = 8000
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(**_pw_launch_kwargs('facebook'))
        except Exception as e:
            _emit(f"[facebook] chromium launch failed: {e}")
            return ''
        storage_state, plain_cookies = _fb_cookie_config()

        ctx = _pw_new_context(browser, stealth=True, platform='facebook', storage_state=storage_state,
                              proxy_override=proxy_override, block_scripts=lean)
        if plain_cookies:
            try:
                ctx.add_cookies(plain_cookies)
            except Exception:
                pass
        page = ctx.new_page()
        _attach_byte_counter(page, 'facebook')
        try:
            # Facebook keeps long-lived connections open, so 'networkidle' can
            # never fire and the whole navigation times out even though the page
            # rendered. Wait for the DOM, then for the listing anchors below.
            #
            # The FIRST navigation of a fresh process reliably blew the old 30s
            # budget — cold Chromium plus a first TLS handshake through the
            # residential proxy — and the timeout was only warned about, so the
            # code carried on and spent another ~14s dismissing modals and
            # scrolling a blank page before returning 63 characters. In practice
            # that meant the first scrape after every backend restart lost
            # Facebook entirely, and reported it as a block.
            try:
                nav_timeout_ms = int(os.getenv('FB_NAV_TIMEOUT_MS', '30000'))
            except ValueError:
                nav_timeout_ms = 30000

            # Only one attempt here on purpose. Measured: when the first
            # navigation of a process fails, retrying goto() on the SAME context
            # fails again every time — the context itself is dead, not merely
            # slow. Recovery happens a level up, in _fb_fetch_with_playwright,
            # which relaunches the browser.
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=nav_timeout_ms)
            except Exception as e:
                _emit(f"[facebook nav warning] {e}")

            # If nothing rendered, stop here. Scrolling and selector-waiting an
            # empty document just burns ~14s to reach the same conclusion.
            try:
                early = page.content() or ''
            except Exception:
                early = ''
            if len(early) < 3000:
                _emit(f"[facebook] navigation produced {len(early)} chars — giving up early")
                return early

            if lean:
                # Everything below this point exists to drive JS we did not load:
                # the login modal is inert, scrolling lazy-loads nothing, and
                # wait_for_selector on an anchor React never renders would burn
                # the full nav_wait_ms every single term.
                try:
                    settle = _fb_lean_settle_ms()
                    if settle:
                        page.wait_for_timeout(settle)
                    return page.content()
                except Exception:
                    return early

            # Logged-out Marketplace renders listings behind a dismissible login
            # modal. Closing it (rather than treating it as a block) is what makes
            # the no-login path work. Try a few selectors FB has used for the close
            # button / overlay; ignore if none present (e.g. authenticated session).
            for sel in (
                'div[aria-label="Close"]',
                'div[role="dialog"] div[aria-label="Close"]',
                '[aria-label="Close"]',
            ):
                try:
                    el = page.query_selector(sel)
                    if el:
                        el.click(timeout=2000)
                        page.wait_for_timeout(800)
                        break
                except Exception:
                    continue
            # Some variants lock body scroll behind the modal; pressing Escape and
            # clearing overflow lets the scroll-to-load below actually work.
            try:
                page.keyboard.press('Escape')
                page.evaluate("document.body.style.overflow = 'auto'")
            except Exception:
                pass

            # Scroll to lazy-load more results (mirrors the tutorial's 4 scrolls).
            try:
                scroll_count = int(os.getenv('FB_SCROLL_COUNT', '4'))
            except ValueError:
                scroll_count = 4
            for _ in range(max(0, scroll_count)):
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)
                except Exception:
                    break

            try:
                page.wait_for_selector("a[href*='/marketplace/item/']", timeout=nav_wait_ms)
            except PWTimeout:
                pass
            return page.content()
        except Exception as e:
            _emit(f"[facebook playwright error] {e}")
            return ''
        finally:
            browser.close()


def scrape_facebook_for_user(user_id, zip_code, search_radius, search_terms, exclusions,
                             ai_enabled, ai_strictness, debug=False, log_callback=None,
                             known_links=None):
    listings = []
    _set_active_log(user_id, log_callback)
    if log_callback:
        log_callback(user_id, 'Waking up Facebook Marketplace (Playwright)...', 'info')

    try:
        max_age_days = int(os.getenv('FB_MAX_LISTING_AGE_DAYS', '45'))
    except ValueError:
        max_age_days = 45
    try:
        max_rows = int(os.getenv('FB_MAX_ROWS_PER_TERM', '40'))
    except ValueError:
        max_rows = 40

    # Resolve the user's zip to Facebook's nearest marketplace region once per
    # scrape, not per term.
    fb_region = None
    try:
        match = fb_region_for_zip(zip_code)
        if match:
            fb_region, region_name, miles = match
            if log_callback:
                log_callback(
                    user_id,
                    f"Facebook region: {region_name} ({miles:.0f} mi from {zip_code})",
                    'info',
                )
        elif log_callback:
            log_callback(
                user_id,
                f"Could not map {zip_code} to a Facebook region — results will follow "
                "the proxy's location, not yours.",
                'error',
            )
    except Exception as e:
        _emit(f"[facebook] region lookup failed: {e}")

    # Lean fetch: block JS and read the server-rendered JSON instead of the
    # rendered DOM. ~93% less residential bandwidth for identical listings —
    # see _fb_collect_from_json. Opt-in until it has production miles on it.
    lean = _env_flag('FB_LEAN_FETCH', False)
    lean_fallback = _env_flag('FB_LEAN_FALLBACK', True)
    if lean and debug and log_callback:
        log_callback(user_id, 'Facebook: lean fetch enabled (JSON, no page render)', 'info')

    # Needed to apply the distance slider ourselves — see the filter below.
    user_coords = _zip_to_latlon(zip_code)
    skipped_far = 0
    skipped_stale = 0
    if search_radius and not user_coords and log_callback:
        log_callback(
            user_id,
            f"Could not geocode {zip_code} — Facebook distance filtering is off for this scan.",
            'info',
        )

    # One browser for the whole term loop instead of one per term. Only the
    # lean path: it made each fetch cheap in bytes but every term still paid a
    # full Chromium launch, which then dominated the time. See _FbLeanSession.
    session = _FbLeanSession() if lean else None
    try:
        for term in search_terms.keys():
            if not is_user_active(user_id):
                if debug and log_callback:
                    log_callback(user_id, 'Facebook stopped by user.', 'info')
                return listings
            try:
                q = quote_plus(term)
                # Without a region slug Facebook redirects to whatever region it
                # infers from the exit IP, so results come from wherever the proxy
                # happens to be rather than near the user. Query params (latitude/
                # longitude/radius) and browser geolocation are both ignored — the
                # slug in the path is the only thing that works.
                if fb_region is None:
                    url = f'https://www.facebook.com/marketplace/search/?query={q}'
                else:
                    url = f'https://www.facebook.com/marketplace/{fb_region}/search/?query={q}'

                html = session.fetch(url) if session else _fb_fetch_with_playwright(url)
                rotated = None
                if _fb_html_is_blocked(html, lean=lean):
                    # A block is usually the exit IP, not the page. Retry once on a
                    # fresh sticky session before calling it a failure — this is the
                    # difference between an intermittently broken platform and a
                    # reliable one.
                    rotated = _rotate_sticky_session(os.getenv('FB_PROXY_URL'))
                    if rotated:
                        if log_callback:
                            log_callback(
                                user_id,
                                f"Facebook '{term}': nothing rendered on this exit IP — "
                                "retrying once on a fresh proxy session.",
                                'info',
                            )
                        if session:
                            # Rebuild the shared browser on the new exit IP. Every
                            # later term in this scrape then reuses it, so a
                            # rotation costs one relaunch, not one per term.
                            session.rotate(rotated)
                            html = session.fetch(url)
                        else:
                            html = _fb_fetch_with_playwright(url, proxy_override=rotated)

                if lean and _fb_html_is_blocked(html, lean=True) and lean_fallback:
                    # Two exits produced no listing JSON. Before reporting a failure,
                    # spend one full render: it is the only way to tell "Facebook
                    # changed the payload" (fixable) from "this term genuinely has no
                    # results" (not). Costs ~1.6MB, so it must stay the exception —
                    # if this fires routinely, the lean parser needs updating, not
                    # more retries.
                    _emit("[facebook] lean fetch found no listing JSON — "
                          "falling back to a full render once")
                    html = _fb_fetch_with_playwright(url, proxy_override=rotated)
                    lean_this_term = False
                else:
                    lean_this_term = lean

                if _fb_html_is_blocked(html, lean=lean_this_term):
                    if log_callback:
                        log_callback(
                            user_id,
                            f"Facebook '{term}': no listings returned"
                            + (" after retrying on a second proxy session" if rotated else "")
                            + ". Two independent exits failing points at the request, not the IP — "
                            "check the exit ASN with: curl.exe -x <FB_PROXY_URL> https://ipinfo.io/json "
                            "before touching selectors. If it persists, set FB_COOKIES_FILE "
                            "for authenticated access.",
                            'error',
                        )
                    continue

                raw_items = (_fb_collect_from_json(html) if lean_this_term
                             else _fb_collect_from_html(html))
                if debug and log_callback:
                    how = 'lean JSON' if lean_this_term else 'Playwright'
                    log_callback(user_id, f"Facebook '{term}': scanned {len(raw_items)} rows ({how})", 'info')

                for item in raw_items[:max(1, max_rows)]:
                    if not is_user_active(user_id):
                        return listings
                    try:
                        title = item.get('title', '')
                        price = item.get('price')
                        link = item.get('link', '')
                        if not (title and price is not None and link):
                            continue
                        # Already have it — do not pay ~1.3s of Vision to rediscover.
                        if known_links and link in known_links:
                            continue
                        # FB_MAX_LISTING_AGE_DAYS has been read and then ignored for
                        # as long as this scraper has existed, because a rendered card
                        # carries no timestamp. The JSON does, so on the lean path the
                        # setting finally means something.
                        created = item.get('creation_time')
                        if created:
                            try:
                                if (time.time() - float(created)) > max_age_days * 86400:
                                    skipped_stale += 1
                                    continue
                            except (TypeError, ValueError):
                                pass
                        meets_threshold, matched_term, max_price = check_price_threshold(title, price, search_terms)
                        if not meets_threshold:
                            continue
                        if is_excluded(title, price, exclusions, search_terms, matched_term):
                            continue
                        if not check_image_with_ai(
                            item.get('image_url'), ai_enabled, ai_strictness,
                            debug, log_callback, user_id, platform_name='facebook',
                            matched_term=matched_term,
                            term_exclusions=(search_terms.get(matched_term) or {}).get('exclusions'),
                        ):
                            continue
                        # Facebook regions are metro-sized and FB ignores every
                        # radius parameter, so the user's distance slider has to be
                        # applied here. A listing whose city we can't geocode is
                        # kept — better an occasional far result than silently
                        # dropping good ones.
                        city = item.get('city')
                        distance_mi = None
                        if city and user_coords and search_radius:
                            cc = city_to_latlon(city)
                            if cc:
                                distance_mi = _haversine_miles(user_coords[0], user_coords[1], cc[0], cc[1])
                                if distance_mi > float(search_radius):
                                    skipped_far += 1
                                    continue

                        listings.append({
                            'title': title,
                            'price': price,
                            'link': link,
                            'platform': 'Facebook',
                            'console_type': matched_term,
                            'threshold': max_price,
                            'image_url': item.get('image_url'),
                            # Epoch seconds on the lean path, None on the DOM path —
                            # _parse_source_datetime handles both.
                            'listed_at': _parse_source_datetime(item.get('creation_time')),
                            'location': (f'{city} · {distance_mi:.0f} mi' if (city and distance_mi is not None)
                                         else (city or f'Facebook · {zip_code}')),
                        })
                    except Exception:
                        continue
                # Politeness pause between terms, same reasoning as OfferUp's:
                # on the lean path a term is one ~110KB document fetch, not a
                # 2MB render, so 2s per term was a large share of the total.
                # Configurable so it can be raised if blocks reappear.
                try:
                    fb_term_delay = float(os.getenv('FB_TERM_DELAY_SEC', '0.75'))
                except ValueError:
                    fb_term_delay = 0.75
                if fb_term_delay > 0:
                    time.sleep(fb_term_delay)
            except Exception as e:
                if log_callback:
                    log_callback(
                        user_id,
                        f"Facebook (Playwright) error on '{term}': {e.__class__.__name__}: {str(e)[:220]}",
                        'error',
                    )

        if debug and log_callback:
            far_note = f' ({skipped_far} beyond {search_radius} mi)' if skipped_far else ''
            stale_note = f' ({skipped_stale} older than {max_age_days}d)' if skipped_stale else ''
            log_callback(user_id,
                         f'Facebook complete: {len(listings)} candidate matches{far_note}{stale_note}',
                         'info')
        return listings
    finally:
        if session is not None:
            session.close()


# ===========================
# OFFERUP SCRAPER
# ===========================
def scrape_offerup_for_user(user_id, zip_code, search_radius, search_terms, exclusions, ai_enabled, ai_strictness,
                            debug=False, log_callback=None, known_links=None):
    listings = []
    _set_active_log(user_id, log_callback)

    if log_callback: log_callback(user_id, "Waking up OfferUp scraper (Playwright)...", "info")
    max_age_days = int(os.getenv('MAX_LISTING_AGE_DAYS', '7'))
    try:
        offerup_detail_delay = float(os.getenv('OFFERUP_ITEM_PAGE_DELAY_SEC', '0.45'))
    except ValueError:
        offerup_detail_delay = 0.45
    try:
        offerup_max_rows = int(os.getenv('OFFERUP_MAX_ROWS_PER_TERM', '30'))
    except ValueError:
        offerup_max_rows = 30

    # OfferUp throttles on request RATE, and it degrades gradually rather than
    # blocking outright: pages come back with progressively fewer listings
    # (~355KB/49 rows healthy → ~229KB/5 rows throttled → ~215KB/0 rows shell).
    # Rotating proxies does not avoid this, so space the terms out instead.
    # Coordinates drive both the GraphQL search location and the distance filter.
    user_coords = _zip_to_latlon(zip_code)
    skipped_far = 0
    if not user_coords and log_callback:
        log_callback(
            user_id,
            f"Could not geocode {zip_code} — OfferUp will fall back to IP-based "
            "location, which follows the proxy, not you.",
            'error',
        )

    # Politeness pause between terms. Was 12s, set when every term meant a full
    # ~400KB page render and hammering that looked abusive. A term is now a
    # single ~4KB GraphQL call, so 12s of sleep per term dwarfed the work itself:
    # 10 terms spent ~108s waiting to do ~30s of fetching. Lowered to 3s, still
    # slower than a human clicking through results, and still tunable — raise it
    # if OfferUp starts returning the "turn off your proxy" page.
    try:
        offerup_term_delay = float(os.getenv('OFFERUP_TERM_DELAY_SEC', '3'))
    except ValueError:
        offerup_term_delay = 3.0

    try:
        for term_index, term in enumerate(search_terms.keys()):
            if not is_user_active(user_id):
                if debug and log_callback:
                    log_callback(user_id, "OfferUp stopped by user.", "info")
                return listings
            if term_index and offerup_term_delay > 0:
                time.sleep(offerup_term_delay)
            try:
                sort_param = (os.getenv('OFFERUP_SORT', '-posted') or '-posted').strip()
                url = 'https://offerup.com/search/?' + urlencode({
                    'q': term,
                    'radius': search_radius,
                    'sort': sort_param,
                    'postal_code': str(zip_code or ''),
                })
                if log_callback: log_callback(user_id, f"Scanning OfferUp for '{term}' (Playwright)...", "info")

                # Go straight to stealth: the plain fetch is reliably judged blocked,
                # so attempting it first only burns a page load and adds rate-limit
                # pressure. OfferUp still serves an occasional listing-less shell
                # (~215KB), so retry once with a generous backoff — retrying harder
                # than this makes results worse, not better.
                try:
                    stealth_tries = int(os.getenv('OFFERUP_STEALTH_RETRIES', '2'))
                except ValueError:
                    stealth_tries = 2
                # Two failure modes need opposite responses:
                #  * ~45KB "turn off your proxy" page — OfferUp flagged the exit
                #    IP. Retrying the same pool rarely helps (shared ASNs), so
                #    after a couple of tries fall back to a direct connection.
                #  * ~215KB listing-less shell — rate limiting. Back off slowly;
                #    retrying fast makes it worse.
                html = ''
                proxy_retries = 0
                max_proxy_retries = 3
                attempt = 0
                geo_blocked = False
                # Preferred path: GraphQL with the user's own coordinates. It is
                # the only way OfferUp respects the user's location (every URL
                # param and client-side override is ignored), and the payload is
                # ~30KB of JSON instead of ~400KB of HTML.
                gql_items = None
                if user_coords and _env_flag('OFFERUP_USE_GRAPHQL', True):
                    # Default to a direct connection. Location now travels in
                    # the query, so the proxy buys nothing here — and it
                    # actively breaks the call: DataImpulse terminates TLS,
                    # which the API rejects (ERR_CERT_AUTHORITY_INVALID).
                    gql_items, gql_err = _offerup_fetch_via_graphql(
                        term, user_coords[0], user_coords[1],
                        force_no_proxy=_env_flag('OFFERUP_GRAPHQL_NO_PROXY', True))
                    if gql_err:
                        _emit(f"[offerup] graphql failed ({gql_err}) — falling back to HTML")
                        gql_items = None
                    elif debug and log_callback:
                        log_callback(
                            user_id,
                            f"OfferUp '{term}': {len(gql_items)} rows via GraphQL "
                            f"at {user_coords[0]:.3f},{user_coords[1]:.3f}",
                            'info',
                        )

                if gql_items:
                    items = gql_items
                    html = ''
                else:
                    while attempt < max(1, stealth_tries):
                        attempt += 1
                        html = _offerup_fetch_with_playwright(url, stealth=True)
                        if not _offerup_html_is_blocked(html):
                            break
                        if _offerup_is_geo_blocked(html):
                            # OfferUp flagged the exit IP as a proxy. Other proxies
                            # in the pool share ASNs, so try a couple, then stop.
                            geo_blocked = True
                            if proxy_retries < max_proxy_retries:
                                proxy_retries += 1
                                attempt -= 1
                                if debug and log_callback:
                                    log_callback(
                                        user_id,
                                        f"OfferUp '{term}': proxy rejected as VPN/proxy, "
                                        f"trying another ({proxy_retries}/{max_proxy_retries})…",
                                        'info',
                                    )
                                time.sleep(1)
                                continue
                            break
                        if attempt < stealth_tries:
                            if debug and log_callback:
                                log_callback(
                                    user_id,
                                    f"OfferUp '{term}': listing-less shell, retry {attempt}/{stealth_tries - 1}…",
                                    'info',
                                )
                            time.sleep(8)

                # Whole datacenter pool rejected. Escalate rather than give up.
                #
                # Residential is a LAST resort on purpose: an OfferUp page runs
                # 350-700KB even with images blocked, so routing every scan
                # through metered residential would cost more per user than the
                # subscription. Cheap pool first, paid bandwidth only when the
                # cheap path is actually refused.
                if geo_blocked and _offerup_html_is_blocked(html):
                    # Escalate to a DIFFERENT provider, not a bigger dose of the
                    # same one: a rejection is about the IP range, so another
                    # address from the same pool usually gets refused too.
                    # Dropping OFFERUP_PROXY_URL makes _get_proxy fall through to
                    # OFFERUP_PROXY_LIST / _FILE, i.e. the secondary pool.
                    fallback = _normalize_proxy(os.getenv('OFFERUP_FALLBACK_PROXY_URL') or '')
                    secondary_pool = (os.getenv('OFFERUP_PROXY_LIST') or
                                      os.getenv('OFFERUP_PROXY_FILE') or '')
                    if fallback or secondary_pool:
                        if log_callback:
                            log_callback(
                                user_id,
                                f"OfferUp '{term}': proxy pool rejected as VPN — "
                                "trying the secondary pool.",
                                'info',
                            )
                        prev = os.environ.get('OFFERUP_PROXY_URL')
                        if fallback:
                            os.environ['OFFERUP_PROXY_URL'] = fallback
                        else:
                            os.environ.pop('OFFERUP_PROXY_URL', None)
                        try:
                            alt_html = _offerup_fetch_with_playwright(url, stealth=True)
                        finally:
                            if prev is None:
                                os.environ.pop('OFFERUP_PROXY_URL', None)
                            else:
                                os.environ['OFFERUP_PROXY_URL'] = prev
                        if not _offerup_html_is_blocked(alt_html):
                            html = alt_html
                    elif _env_flag('OFFERUP_ALLOW_DIRECT_FALLBACK', True):
                        if log_callback:
                            log_callback(
                                user_id,
                                f"OfferUp '{term}': every proxy rejected as a VPN — "
                                "retrying on the direct connection.",
                                'info',
                            )
                        direct_html = _offerup_fetch_with_playwright(url, stealth=True, force_no_proxy=True)
                        if not _offerup_html_is_blocked(direct_html):
                            html = direct_html
                        elif _offerup_is_geo_blocked(direct_html) and log_callback:
                            log_callback(
                                user_id,
                                "OfferUp rejected the direct connection too — set "
                                "OFFERUP_FALLBACK_PROXY_URL to a residential endpoint.",
                                'error',
                            )

                # Only parse HTML when the GraphQL path didn't already supply rows.
                if not gql_items:
                    items = _offerup_collect_from_html(html)
                    if debug and log_callback:
                        fetch_mode = 'stealth' if _offerup_html_is_blocked(html) else 'playwright'
                        log_callback(
                            user_id,
                            f"OfferUp '{term}': scanned {len(items[:50])} rows (via {fetch_mode}, html_len={len(html or '')})",
                            "info",
                        )

                for item in items[:max(1, offerup_max_rows)]:
                    if not is_user_active(user_id):
                        if debug and log_callback:
                            log_callback(user_id, "OfferUp stopped by user.", "info")
                        return listings
                    try:
                        link = item.get('link')
                        title = item.get('title')
                        price = item.get('price')

                        if not price or not link or not title: continue
                        # Already have it — do not pay ~1.3s of Vision to rediscover.
                        if known_links and link in known_links: continue
                        meets_threshold, matched_term, max_price = check_price_threshold(title, price, search_terms)
                        if not meets_threshold: continue
                        if is_excluded(title, price, exclusions, search_terms, matched_term): continue

                        image_url = item.get('image_url')

                        if not check_image_with_ai(
                            image_url, ai_enabled, ai_strictness, debug, log_callback, user_id, platform_name='offerup',
                        matched_term=matched_term,
                        term_exclusions=(search_terms.get(matched_term) or {}).get('exclusions')):
                            continue

                        listed_at = item.get('listed_at')
                        if listed_at is None:
                            listed_at = _offerup_listed_at_from_item_page(link, offerup_detail_delay)
                        listed_for_age = listed_at.isoformat() if isinstance(listed_at, datetime) else listed_at
                        if listed_for_age and not _is_recent_timestamp(listed_for_age, max_age_days):
                            continue

                        # OfferUp ignores its own radius param (5 vs 200 returns
                        # identical results), so the distance slider is applied
                        # here. Unknown cities are kept, as on Facebook.
                        city = item.get('city')
                        distance_mi = None
                        if city and user_coords and search_radius:
                            cc = city_to_latlon(city)
                            if cc:
                                distance_mi = _haversine_miles(
                                    user_coords[0], user_coords[1], cc[0], cc[1])
                                if distance_mi > float(search_radius):
                                    skipped_far += 1
                                    continue

                        listings.append({
                            'title': title, 'price': price, 'link': link, 'platform': 'OfferUp',
                            'console_type': matched_term, 'threshold': max_price,
                            'image_url': image_url,
                            'listed_at': listed_at,
                            'location': (f'{city} · {distance_mi:.0f} mi'
                                         if (city and distance_mi is not None)
                                         else (city or f'OfferUp · {zip_code}'))
                        })
                    except:
                        continue
                time.sleep(3)
            except:
                pass
    except Exception as e:
        if log_callback:
            detail = (str(e) or '').replace('\n', ' ')[:220]
            log_callback(
                user_id,
                f"OfferUp unavailable: {e.__class__.__name__}: {detail}.",
                "error"
            )
    if debug and log_callback:
        far_note = f' ({skipped_far} beyond {search_radius} mi)' if skipped_far else ''
        log_callback(user_id, f"OfferUp complete: {len(listings)} candidate matches{far_note}", "info")
    return listings


# ===========================
# USER SCRAPER
# ===========================
def _clear_scrape_stamp_for_retry(user_id):
    """
    Undo the finish stamp after a failed scrape so the next cycle retries.

    scrape_for_user() stamps last_scraped_at in a finally block, which is right
    for a successful run but wrong for a crash: the countdown would start and
    the user would wait a full interval having received nothing. Clearing it
    means a failure costs one cycle (~1 min) instead of the whole interval.
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        if not conn:
            return
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_settings SET last_scraped_at = NULL WHERE user_id = %s",
            (user_id,),
        )
        conn.commit()
    except Exception as e:
        print(f"[retry-stamp] could not reset last_scraped_at for {user_id}: {e}", flush=True)
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _stamp_user_scrape_complete(user_id, duration_ms):
    """Record scrape finish time before clearing in-progress flag (drives next-check countdown)."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        if not conn:
            return
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE user_settings
            SET last_scraped_at = NOW(),
                last_scrape_duration_ms = %s
            WHERE user_id = %s
            """,
            (duration_ms, user_id),
        )
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"stamp last_scraped_at failed for {user_id[:8]}: {e}", flush=True)
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def scrape_for_user(user_config, log_callback=None, debug=False):
    user_id = user_config['user_id']
    set_user_scraping(user_id, True)
    started = time.time()
    _set_active_log(user_id, log_callback)

    # Log BEFORE anything can raise. _scrape_for_user_impl does several dict
    # lookups before its first log line, so an early KeyError produced a scrape
    # that stamped last_scraped_at, started the user's countdown, and printed
    # nothing — a "ghost scrape". One line here makes that impossible.
    if log_callback:
        log_callback(user_id, "Scan starting…", "info")

    try:
        return _scrape_for_user_impl(user_config, log_callback=log_callback, debug=debug)
    except Exception as e:
        # Report here too: the caller logs it, but only if the caller has a
        # callback. This guarantees the user sees the reason either way.
        if log_callback:
            log_callback(user_id, f"Scan aborted: {type(e).__name__}: {e}", "error")
        raise
    finally:
        duration_ms = int((time.time() - started) * 1000)
        # A run that ended because the user pressed STOP did no work, so it must
        # not consume their interval — otherwise stop/start costs a full cycle.
        aborted_by_stop = not is_user_active(user_id)
        if aborted_by_stop:
            _clear_scrape_stamp_for_retry(user_id)
        else:
            if log_callback and duration_ms < 1000:
                log_callback(
                    user_id,
                    f"Scan ended after {duration_ms}ms without scraping — "
                    "this usually means a configuration problem, not an empty result.",
                    "error",
                )
            _stamp_user_scrape_complete(user_id, duration_ms)
        set_user_scraping(user_id, False)


def _scrape_for_user_impl(user_config, log_callback=None, debug=False):
    user_id = user_config['user_id']
    zip_code = user_config['zip_code']
    # Per-scrape, so the figure reported at the end is this run's traffic.
    # NOTE: with SCRAPE_MAX_WORKERS > 1 the counter is shared across concurrent
    # scrapes, so the number is per-cycle rather than strictly per-user. Fine
    # for costing — it is the total that gets billed.
    _reset_bytes()
    user_config = {
        **user_config,
        'platforms': _coerce_platforms_dict(user_config.get('platforms')),
        'buyer_include_local': bool(user_config.get('buyer_include_local', True)),
        'buyer_include_shipping': bool(user_config.get('buyer_include_shipping', True)),
    }
    if not user_config['buyer_include_local'] and not user_config['buyer_include_shipping']:
        user_config['buyer_include_local'] = True

    # Entitlement follows the CURRENT plan, not whatever was stored when the
    # user last saved settings (see _ai_allowed_for_tier).
    if user_config.get('ai_enabled') and not _ai_allowed_for_tier(user_config.get('plan_tier')):
        user_config['ai_enabled'] = False

    if log_callback: log_callback(user_id, f"Target locked: Zip Code {zip_code} ({user_config['search_radius']}mi)",
                                  "info")

    plat = user_config['platforms']
    if log_callback:
        enabled = [name for name in ('craigslist', 'offerup', 'mercari', 'facebook') if plat.get(name)]
        log_callback(user_id, f"Scanning: {', '.join(enabled) or 'no platforms'}", 'info')

    all_terms = get_user_search_terms(user_id)
    if not all_terms:
        if log_callback: log_callback(user_id, "No search terms found. Scanner idle.", "error")
        return 0

    # Only scan the terms whose own interval has elapsed. A Pro user's 3 priority
    # terms come round every 5 minutes while the other 7 wait for 15, so scanning
    # the whole set whenever any one is due would throw the saving away.
    search_terms, next_term_due = due_terms_for_user(all_terms, user_config)
    if not search_terms:
        if log_callback and next_term_due:
            wait_s = max(0, int(next_term_due - time.time()))
            mins, secs = divmod(wait_s, 60)
            log_callback(user_id, f"No terms due yet — next in {mins}m {secs:02d}s.", 'info')
        return 0

    if len(search_terms) < len(all_terms) and log_callback:
        held = len(all_terms) - len(search_terms)
        log_callback(
            user_id,
            f"Scanning {len(search_terms)} of {len(all_terms)} terms "
            f"({held} not due yet on the standard interval).",
            'info',
        )

    if log_callback:
        parts = []
        # Cadence is per term now, so the console shows each term's own rate.
        # ⚡ still means "on the plan's fastest tier" — derived from the interval
        # rather than the is_priority flag, which is no longer authoritative.
        tier_for_log = (user_config.get('plan_tier')
                        or _tier_from_db_row(user_config) or '').strip().lower()
        standard_min = standard_floor_for_tier(tier_for_log)
        for t, rng in search_terms.items():
            lo = rng.get('min')
            hi = rng.get('max')
            # An unset bound is "any price", not $None.
            if lo is None and hi is None:
                bounds = 'any price'
            elif hi is None:
                bounds = f'${lo:g}+'
            elif lo is None:
                bounds = f'up to ${hi:g}'
            else:
                bounds = f'${lo:g}-${hi:g}'
            excl = rng.get('exclusions') or []
            suffix = f", excl: {', '.join(excl[:3])}" if excl else ''
            every = term_interval_minutes(rng, user_config)
            fast = ' ⚡' if every < standard_min else ''
            parts.append(f"{t!r}{fast} ({every}m, {bounds}{suffix})")
        preview = ", ".join(parts[:12])
        if len(parts) > 12:
            preview += f" … (+{len(parts) - 12} more)"
        log_callback(
            user_id,
            f"Active terms ({len(search_terms)}): {preview}",
            "info",
        )

    exclusions = get_user_exclusions(user_id)
    notify_prefs = get_user_notification_prefs(user_id)
    seen_listings = get_seen_listings(user_id)
    blocked_links, blocked_fingerprints = get_blocked_links_and_fingerprints(user_id)

    # Handed to every scraper so a listing we already hold is discarded on sight,
    # BEFORE the expensive per-item work: Vision runs ~1.3s per image and
    # Craigslist fetches a detail page plus a polite-delay sleep per tile.
    # These links were always going to be dropped by the dedup pass further
    # down; this just stops us paying to rediscover them first. It matters most
    # in steady state — on a 5-minute cadence nearly every candidate is one we
    # have already seen, so the old order paid nearly the full cost of a first
    # scrape to find almost nothing.
    known_links = seen_listings | blocked_links

    recent_sigs = get_recent_listing_signatures(user_id)
    recent_fp_to_prices = {}
    for row in recent_sigs:
        fp = row.get('title_fingerprint')
        if not fp:
            continue
        recent_fp_to_prices.setdefault(fp, []).append(float(row.get('price') or 0))


    # Skip platforms that cannot satisfy the buyer's delivery preference. A
    # shipping-only buyer gets nothing usable from Craigslist or OfferUp, so
    # scraping them wastes a run and proxy bandwidth on alerts they'd discard.
    want_local = user_config['buyer_include_local']
    want_shipping = user_config['buyer_include_shipping']

    def _delivery_allows(platform_name):
        if platform_matches_buyer_delivery_prefs(platform_name, want_local, want_shipping):
            return True
        if log_callback:
            mode = platform_delivery_mode(platform_name)
            wanted = 'local pickup' if want_local else 'shipping'
            log_callback(
                user_id,
                f"Skipping {platform_name.title()} — it's {mode}-only and you asked for {wanted}.",
                'info',
            )
        return False

    # Fresh allowance of new-city geocodes for this scrape — see city_to_latlon.
    reset_geocode_budget()

    stats = {'candidates': 0, 'saved': 0, 'seen_or_link': 0,
             'fingerprint': 0, 'recent_dupe': 0, 'delivery': 0}
    new_listings = []
    cycle_fp_to_prices = {}

    def _persist(batch):
        """
        Dedup and save ONE platform's results, immediately.

        This used to run once, after every platform had finished. That made the
        whole scrape all-or-nothing: when Mercari stalled, a scan that had
        already collected Craigslist and OfferUp listings saved none of them
        (observed in production 2026-08-12). Pressing STOP threw them away too.

        Saving per platform keeps whatever has been found. The cross-platform
        duplicate gate still works because cycle_fp_to_prices persists across
        calls — a listing found on both Craigslist and OfferUp in the same scan
        is still caught by the second call.
        """
        if not batch:
            return
        stats['candidates'] += len(batch)
        to_save = []
        for listing in batch:
            if not is_user_active(user_id):
                if log_callback:
                    log_callback(user_id, "Scrape stopped by user during result processing.", "info")
                break
            link = listing['link']
            fp = _title_fingerprint(listing.get('title'))
            listing['title_fingerprint'] = fp
            price = float(listing.get('price') or 0)

            if link in seen_listings or link in blocked_links:
                stats['seen_or_link'] += 1
                continue
            if fp and fp in blocked_fingerprints:
                stats['fingerprint'] += 1
                continue

            # Fuzzy-ish duplicate gate across marketplaces:
            # same normalized title fingerprint and close price (+/- $10)
            recent_prices = recent_fp_to_prices.get(fp, [])
            cycle_prices = cycle_fp_to_prices.get(fp, [])
            if fp and any(abs(price - p) <= 10 for p in recent_prices + cycle_prices):
                stats['recent_dupe'] += 1
                continue

            if not listing_matches_buyer_delivery_prefs(
                listing,
                user_config['buyer_include_local'],
                user_config['buyer_include_shipping'],
            ):
                stats['delivery'] += 1
                continue

            # Collected rather than written per row: one bulk insert replaces one
            # connection per listing. Reserve the fingerprint now so the in-cycle
            # duplicate gate above still sees it.
            to_save.append(listing)
            cycle_fp_to_prices.setdefault(fp, []).append(price)
            # Mark the link seen, so a later platform in this same scrape cannot
            # re-save it. seen_listings was previously only consulted, never
            # added to, which was safe only because this block ran exactly once.
            seen_listings.add(link)
            # known_links is the copy handed to the scrapers, and the later ones
            # have not started yet — adding here means they skip this listing
            # before paying for Vision or a detail page.
            known_links.add(link)

        if not to_save:
            return
        inserted_links = save_listings_bulk(user_id, to_save)
        for listing in to_save:
            if listing['link'] not in inserted_links:
                continue
            new_listings.append(listing)
            stats['saved'] += 1
            if log_callback:
                log_callback(
                    user_id,
                    f"New match: {listing['title'][:40]} — ${listing['price']}",
                    "success"
                )

    # Each platform is its own job so one failure cannot take the others with it.
    platform_jobs = []
    if user_config['platforms'].get('craigslist') and _delivery_allows('craigslist'):
        platform_jobs.append(('Craigslist', scrape_craigslist_for_user))
    if user_config['platforms'].get('offerup') and _delivery_allows('offerup'):
        platform_jobs.append(('OfferUp', scrape_offerup_for_user))
    if user_config['platforms'].get('mercari') and _delivery_allows('mercari'):
        platform_jobs.append(('Mercari', scrape_mercari_for_user))
    if user_config['platforms'].get('facebook') and _delivery_allows('facebook'):
        if (user_config.get('plan_tier') or '').strip().lower() == 'pro':
            platform_jobs.append(('Facebook', scrape_facebook_for_user))
        elif log_callback:
            log_callback(user_id, "Facebook Marketplace requires a Pro plan.", "info")

    for label, fn in platform_jobs:
        if not is_user_active(user_id):
            if log_callback:
                log_callback(user_id, f"Scrape stopped by user before {label}.", "info")
            break
        try:
            batch = fn(user_id, zip_code, user_config['search_radius'], search_terms,
                       exclusions, user_config['ai_enabled'], user_config['ai_strictness'],
                       debug, log_callback, known_links=known_links)
        except Exception as e:
            # One marketplace failing is not the scrape failing. Report it and
            # keep whatever the others found.
            print(f"  ❌ [{user_id[:8]}] {label} failed: {type(e).__name__}: {e}", flush=True)
            if log_callback:
                log_callback(user_id,
                             f"{label} failed ({type(e).__name__}) — continuing with "
                             "your other marketplaces.", 'error')
            batch = []
        # Saved here, before the next platform runs, so a later stall cannot
        # discard it.
        _persist(batch)
        _log_memory(label, log_callback, user_id)

    if new_listings:
        _notify_scrape_digest(user_id, new_listings, notify_prefs, log_callback)

    if debug and log_callback:
        log_callback(
            user_id,
            (
                f"Filter summary: candidates={stats['candidates']} "
                f"saved={stats['saved']} "
                f"seen_or_link_blocked={stats['seen_or_link']} "
                f"fingerprint_blocked={stats['fingerprint']} "
                f"recent_dupe={stats['recent_dupe']} "
                f"buyer_local_shipping_prefs={stats['delivery']}"
            ),
            "info",
        )

    if log_callback and len(new_listings) == 0:
        log_callback(user_id, "Scan complete. No new matches found.", "info")

    # Stamp only the terms actually scanned. Doing this at the end rather than up
    # front means a crash mid-scrape leaves the terms due, matching how
    # _clear_scrape_stamp_for_retry gives the user their interval back instead of
    # burning it on a failed run.
    stamp_terms_scraped(user_id, list(search_terms.keys()))

    _log_bandwidth(log_callback, user_id)

    return len(new_listings)


# ===========================
# MAIN LOOP
# ===========================
def main(log_callback=None, health_callback=None):
    if log_callback is None:
        def default_log(uid, msg, ltype="info"):
            print(f"[{uid[:8]}] {msg}", flush=True)
        log_callback = default_log

    print("=" * 60, flush=True)
    print("PIXELFLIP MASTER CLOCK SCRAPER", flush=True)
    print("(Craigslist, OfferUp, Mercari via Playwright; Facebook Marketplace on Pro tier)", flush=True)
    print("=" * 60, flush=True)
    sys.stdout.flush()

    cycle = 0

    while True:
        try:
            cycle += 1
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"\n[{timestamp}] 🔄 Master Cycle #{cycle}", flush=True)

            # Release the connection BEFORE scraping. It used to stay open for
            # the whole cycle — hours with several users — pinning a pooler slot
            # the entire time for a query that takes milliseconds.
            conn = get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            try:
                # 1. Pull all active users AND their last scraped times!
                cursor.execute("""
                    SELECT user_id, zip_code, search_radius, platforms,
                           ai_enabled, ai_strictness, check_interval_minutes,
                           plan_tier, is_pro,
                           buyer_include_local, buyer_include_shipping,
                           EXTRACT(EPOCH FROM last_scraped_at) as last_scraped_ts
                    FROM user_settings
                    WHERE is_active = TRUE;
                """)
                active_users = cursor.fetchall()

                # Every active user's terms in ONE query. Asking per user inside
                # the loop below would be a round-trip per user per cycle — 60
                # users is 60 extra queries a minute purely to decide who to skip.
                cursor.execute("""
                    SELECT t.user_id, t.search_term, t.is_priority, t.interval_minutes,
                           EXTRACT(EPOCH FROM t.last_scraped_at) AS last_scraped_ts
                    FROM user_search_terms t
                    JOIN user_settings s ON s.user_id = t.user_id
                    WHERE s.is_active = TRUE;
                """)
                all_terms_by_user = {}
                for row in cursor.fetchall():
                    all_terms_by_user.setdefault(row['user_id'], {})[row['search_term']] = {
                        'is_priority': bool(row['is_priority']),
                        'interval_minutes': (int(row['interval_minutes'])
                                             if row['interval_minutes'] is not None
                                             else DEFAULT_TERM_INTERVAL_MINUTES),
                        'last_scraped_ts': (float(row['last_scraped_ts'])
                                            if row['last_scraped_ts'] is not None else 0.0),
                    }
            finally:
                cursor.close()
                conn.close()

            current_time = time.time()
            total_new = 0

            # 2. THE CLOCK: decide who is due before running anything, so the
            #    due users can be scraped concurrently instead of queueing
            #    behind each other. Sequentially, one 5-minute user delayed
            #    everyone after them — with 30 users a cycle took hours and the
            #    5-minute Pro promise was unmeetable.
            due = []
            for user in active_users:
                uid = user['user_id']
                if is_user_scraping(uid):
                    continue
                last_scraped = float(user['last_scraped_ts']) if user['last_scraped_ts'] else 0.0
                tier = _tier_from_db_row(user)
                user_cfg = {**dict(user), 'plan_tier': tier}
                user_cfg['buyer_include_local'] = bool(user.get('buyer_include_local', True))
                user_cfg['buyer_include_shipping'] = bool(user.get('buyer_include_shipping', True))
                interval_min = _effective_check_interval_minutes_for_user(user_cfg)
                interval_seconds = int(interval_min) * 60

                # Cadence is per TERM on every tier now, so this is the whole
                # gate — a user is due as soon as ANY of their terms is. It used
                # to run only for tiers with priority terms and fall back to the
                # user-level interval otherwise, which after migration 012 meant
                # Basic users were scheduled by check_interval_minutes, the
                # column the per-term rates replaced.
                #
                # due_terms_for_user also hands back the soonest term's ready
                # time, which is what the idle message has to quote. Quoting the
                # user-level interval showed a Pro user "next scan in 25m" while
                # their terms were genuinely due in 10 — a countdown that
                # disagrees with reality reads as a broken scanner.
                terms_for_user = all_terms_by_user.get(uid)
                next_due_ts = None
                if terms_for_user:
                    due_now, next_due_ts = due_terms_for_user(
                        terms_for_user, user_cfg, now=current_time)
                    term_due = bool(due_now)
                else:
                    # No terms is nothing to schedule. Waking the user anyway
                    # bought a full browser launch to discover there was no work.
                    term_due = False

                if term_due:
                    due.append(user_cfg)
                else:
                    # Waiting on a term's interval. Say so rather than going
                    # silent — an unexplained quiet dashboard is indistinguishable
                    # from a broken scraper.
                    if log_callback and next_due_ts and cycle % 5 == 1:
                        wait_s = int(next_due_ts - current_time)
                        if wait_s > 0:
                            mins, secs = divmod(wait_s, 60)
                            log_callback(
                                uid,
                                f"Idle — next scan in {mins}m {secs:02d}s.",
                                'info',
                            )

            def _run_one(cfg):
                """One user's scrape, with the per-user error handling intact."""
                uid = cfg['user_id']
                try:
                    return scrape_for_user(cfg, log_callback=log_callback, debug=True)
                except Exception as e:
                    # A crash here used to be invisible: it printed to server
                    # stdout only, while the finally-block stamp started the
                    # user's countdown. From the dashboard that looked like a
                    # scan that ran in 0s and found nothing. Surface it to the
                    # user AND give the interval back.
                    print(f"  ❌ [{uid}] Error: {e}", flush=True)
                    traceback.print_exc()
                    if log_callback:
                        log_callback(uid, f"Scan failed: {type(e).__name__}: {e}", 'error')
                        log_callback(
                            uid,
                            "This scan did not count against your interval — retrying shortly.",
                            'info',
                        )
                    _clear_scrape_stamp_for_retry(uid)
                    return 0

            if due:
                # Mercari once for everybody, BEFORE the pool fans out. The
                # search API is user-independent, so N users sharing a term is N
                # identical requests — and each one would otherwise open its own
                # real-Chrome context at ~644MB. One browser per cycle here is
                # what stops Mercari dictating SCRAPE_MAX_WORKERS.
                try:
                    shared_terms = set()
                    for cfg in due:
                        plats = _coerce_platforms_dict(cfg.get('platforms'))
                        if not plats.get('mercari'):
                            continue
                        if (cfg.get('plan_tier') or '').strip().lower() == 'inactive':
                            continue
                        user_terms = all_terms_by_user.get(cfg['user_id']) or {}
                        due_now, _ = due_terms_for_user(user_terms, cfg, now=current_time)
                        shared_terms.update(due_now.keys() if due_now else user_terms.keys())
                    if shared_terms:
                        n, mc_err = mercari_prefetch_cycle(shared_terms)
                        if mc_err:
                            print(f"  ⚠ Mercari shared fetch failed ({mc_err}) — "
                                  "users will fetch individually", flush=True)
                            # Also tell the affected users. This ran in the
                            # scheduler, so its only output was the server's
                            # stdout — which meant the dashboard showed the
                            # resulting cooldown but never the reason for it,
                            # and the actual failure was invisible without
                            # Render log access.
                            if log_callback:
                                for cfg in due:
                                    try:
                                        log_callback(cfg['user_id'],
                                                     f"Mercari unavailable: {mc_err[:160]}",
                                                     'error')
                                    except Exception:
                                        pass
                        else:
                            print(f"  🛒 Mercari: {n} unique term(s) fetched once for "
                                  f"{len(due)} user(s)", flush=True)
                except Exception as e:
                    # Never let the optimisation break the cycle; a failure here
                    # just means each user fetches for itself, as before.
                    print(f"  ⚠ Mercari prefetch skipped: {type(e).__name__}: {e}", flush=True)

                # Bounded by MEMORY, not by CPU. Measured peak per concurrent
                # scrape, 2026-08-11 (peak working set of the whole
                # chrome-headless-shell process group):
                #
                #   lean path (FB_LEAN_FETCH=1)   206MB over 4 processes
                #   DOM path  (renders Facebook)  455MB over 6 processes
                #
                # The DOM figure is why 512MB instances die: one scrape alone
                # peaks at 455MB of a 512MB container that also holds Python and
                # Flask. The ceiling is roughly (container RAM - 200MB) / peak,
                # so 512MB is 1 worker either way, 2GB is 8 lean, 4GB is 18.
                # Raise this only alongside RAM — queueing is survivable, an OOM
                # kill takes the whole process down including the API.
                try:
                    max_workers = max(1, int(os.getenv('SCRAPE_MAX_WORKERS', '3')))
                except ValueError:
                    max_workers = 3
                workers = min(max_workers, len(due))
                print(f"  ▶ {len(due)} user(s) due — running {workers} at a time", flush=True)

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix='scrape') as pool:
                    futures = [pool.submit(_run_one, cfg) for cfg in due]
                    for fut in as_completed(futures):
                        try:
                            total_new += fut.result() or 0
                        except Exception as e:
                            # _run_one already handles per-user failures; this is
                            # only for something raised outside it.
                            print(f"  ❌ worker future failed: {e}", flush=True)

            # How long before we look for due users again. This is ALSO the
            # worst-case delay between a user pressing START and their console
            # showing anything: /api/start only flips is_active, and nothing
            # happens until the next cycle notices. At 60s that felt broken.
            # The cycle costs two indexed queries, so polling four times as often
            # is cheap next to the scraping it gates.
            try:
                cycle_sleep = max(5, int(os.getenv('SCRAPE_CYCLE_SLEEP_SEC', '15')))
            except ValueError:
                cycle_sleep = 15

            print(f"\n✅ Total new listings this cycle: {total_new}", flush=True)
            print(f"⏳ Cycle complete. Waiting {cycle_sleep}s...", flush=True)
            sys.stdout.flush()

            # Report health check timestamp
            if health_callback:
                try:
                    health_callback()
                except Exception:
                    pass

            time.sleep(cycle_sleep)

        except Exception as e:
            print(f"❌ Error: {e}", flush=True)
            time.sleep(60)

if __name__ == "__main__":
    main()