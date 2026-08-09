import time
import tempfile
import uuid
import traceback
import os
import sys
import re
import json
import hashlib
import threading
import requests
import psycopg2
from psycopg2 import errorcodes
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse, quote, quote_plus, urlencode
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

    url = urlparse(database_url)
    try:
        attempts = int(os.getenv('DB_CONNECT_RETRIES', '3'))
    except ValueError:
        attempts = 3

    last_err = None
    conn = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            conn = psycopg2.connect(
                host=url.hostname,
                port=url.port or 5432,
                database=url.path[1:],
                user=url.username,
                password=url.password,
                sslmode='require',
                connect_timeout=10
            )
            break
        except psycopg2.OperationalError as e:
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
        from db_schema import ensure_buyer_delivery_columns, ensure_listing_uniqueness_per_user
        ensure_buyer_delivery_columns(conn)
        # save_listing lives in this module and targets ON CONFLICT (user_id, link),
        # so the matching index has to be guaranteed on the scraper's own
        # connection — test_scraper.py never goes through app.py's.
        ensure_listing_uniqueness_per_user(conn)
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
        cursor.execute('SELECT search_term, min_price, max_price FROM user_search_terms WHERE user_id = %s', (user_id,))
        terms = {
            row[0]: {
                'min': float(row[1]) if row[1] is not None else None,
                'max': float(row[2]) if row[2] is not None else None,
                'exclusions': [],
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
    try:
        detail_delay = float(os.getenv('CRAIGSLIST_DETAIL_DELAY_SEC', '0.35'))
    except ValueError:
        detail_delay = 0.35

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
                soup = BeautifulSoup(response.content, 'html.parser')
                items = soup.find_all('li', class_='cl-static-search-result')
                if debug and log_callback:
                    log_callback(user_id, f"Craigslist {subdomain} '{term}': scanned {len(items)} rows", "info")

                for item in items:
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

                        price_elem = item.find('div', class_='price')
                        price = extract_price(price_elem.text if price_elem else None)
                        if not price:
                            price = extract_price(item.get_text(" ", strip=True))
                        if _is_craigslist_placeholder_price(price):
                            price = None

                        if not link:
                            continue

                        # Placed before the detail fetch, not just before Vision:
                        # a listing we already hold costs a page GET plus a
                        # polite-delay sleep here, and it would be discarded by
                        # the dedup pass minutes later regardless.
                        if known_links and link in known_links:
                            continue

                        entry = {
                            'title': title,
                            'price': price,
                            'link': link,
                            'platform': 'Craigslist',
                            'console_type': None,
                            'threshold': None,
                            'image_url': None,
                            'listed_at': _parse_source_datetime(dt_text) if dt_text else None,
                            'location': f'Craigslist ({subdomain}) · {zip_code} ({search_radius} mi)',
                        }

                        if fetch_detail:
                            if not _enrich_craigslist_from_detail(session, entry, max_age_days, detail_delay):
                                continue
                            if not entry.get('image_url'):
                                entry['image_url'] = _craigslist_image_from_data_ids(item)
                        else:
                            if not entry.get('price'):
                                continue
                            try:
                                img_elem = item.find('img')
                                entry['image_url'] = _extract_lazy_image_url(img_elem)
                            except Exception:
                                pass
                            if not entry.get('image_url'):
                                entry['image_url'] = _craigslist_image_from_data_ids(item)

                        if entry.get('price') is None or _is_craigslist_placeholder_price(entry.get('price')):
                            continue

                        meets_threshold, matched_term, max_price = check_price_threshold(title, entry['price'], search_terms)
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


def _pw_block_heavy_requests(ctx):
    """
    Drop request types whose bytes we never use. Image *URLs* survive this —
    they live in the <img src> attribute, and the DOM keeps them whether or not
    Chromium downloads the pixels. Google Vision fetches images server-side by
    URI, and the dashboard embeds URLs for the end user's browser to load, so
    nothing downstream needs the bytes on our side.

    First-party JS is deliberately left alone: lazy-loaded listings and the
    antibot checks depend on it.
    """
    blocked_types = {'image', 'media', 'font'}
    if _env_flag('PW_BLOCK_CSS', True):
        blocked_types.add('stylesheet')

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
                    force_no_proxy=False, proxy_override=None):
    """
    New browser context with per-platform proxy routing (see _get_proxy).

    `proxy_override` bypasses that routing for a single context. It exists for
    retrying on a different sticky session after a block — see
    _rotate_sticky_session.
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
        _pw_block_heavy_requests(ctx)
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
            ctx = _pw_new_context(browser, stealth=True, platform='offerup',
                                  force_no_proxy=force_no_proxy)
            page = ctx.new_page()
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
                res = page.evaluate(_OFFERUP_GQL_JS, [query, term, lat, lon, 60])
                if res.get('status') != 200:
                    if attempt == 1:
                        query = _offerup_capture_query(page) or query
                        continue
                    return [], f"graphql HTTP {res.get('status')}"
                try:
                    payload = json.loads(res.get('text') or '{}')
                except Exception:
                    return [], 'unparseable graphql response'
                if payload.get('errors'):
                    msg = json.dumps(payload['errors'])[:160]
                    if attempt == 1:
                        # Most likely our stored document drifted from theirs.
                        query = _offerup_capture_query(page) or query
                        continue
                    return [], f'graphql errors: {msg}'
                return _offerup_tiles_to_items(payload), None
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
    try:
        from patchright.sync_api import sync_playwright
        prof = os.path.join(tempfile.gettempdir(), 'pixelflip_ua_probe')
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
    return _PATCHRIGHT_UA_CACHE


def _mercari_chrome_kwargs():
    """
    How to reach real Chrome: by channel, or by explicit path.

    `channel='chrome'` asks Playwright to find a system-installed Chrome, which
    on Render's native runtime does not exist — installing it needs apt and
    root. MERCARI_CHROME_PATH points straight at a binary instead, so Chrome can
    be unpacked from its .deb with `dpkg-deb -x` (no root) during the build.

    The two are mutually exclusive in Playwright: passing both raises.
    """
    explicit = (os.getenv('MERCARI_CHROME_PATH') or '').strip()
    if explicit:
        if not os.path.isfile(explicit):
            _emit(f"[mercari] MERCARI_CHROME_PATH set but no file at {explicit} — falling back to channel")
        else:
            return {'executable_path': explicit}
    return {'channel': os.getenv('MERCARI_CHROME_CHANNEL', 'chrome')}


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

    profile_dir = (os.getenv('PATCHRIGHT_PROFILE_DIR') or '').strip()
    if not profile_dir:
        profile_dir = os.path.join(tempfile.gettempdir(), 'pixelflip_mercari_profile')

    # Headless is requested via the Chrome flag rather than Playwright's
    # headless=True, so patchright doesn't layer old-headless flags on top.
    args = ['--window-size=1920,1080']
    if _env_flag('MERCARI_HEADLESS', True):
        args.insert(0, '--headless=new')
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
        try:
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=45000)
            except Exception as e:
                _emit(f"[mercari nav warning] {e}")

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


def _load_city_cache():
    global _CITY_CACHE, _CITY_CACHE_LOADED
    if _CITY_CACHE_LOADED:
        return
    _CITY_CACHE_LOADED = True
    try:
        if os.path.isfile(_CITY_CACHE_PATH):
            with open(_CITY_CACHE_PATH, 'r', encoding='utf-8') as f:
                _CITY_CACHE = {k: tuple(v) if v else None for k, v in json.load(f).items()}
    except Exception:
        _CITY_CACHE = {}


def _save_city_cache():
    try:
        with open(_CITY_CACHE_PATH, 'w', encoding='utf-8') as f:
            json.dump({k: list(v) if v else None for k, v in _CITY_CACHE.items()}, f, indent=0)
    except Exception:
        pass


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
#   FB_SCROLL_COUNT         — scroll-to-load passes before parsing (default 4)
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


def _fb_html_is_blocked(html):
    # The presence of login text is NOT itself a block: logged-out Marketplace
    # renders listings behind a login modal, and _fb_fetch_with_playwright
    # dismisses it. Only the absence of actual item links means we got nothing
    # usable (hard wall, checkpoint, or empty result set).
    if not html or len(html) < 3000:
        _emit(f"[facebook debug] page size {len(html or '')} chars — too small, treating as blocked", "info")
        return True
    if 'marketplace/item/' not in html.lower():
        _emit(f"[facebook debug] page size {len(html):,} chars — no item links found", "info")
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


def _fb_fetch_with_playwright(url, proxy_override=None):
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
        html = _fb_fetch_once(url, proxy_override=proxy_override)
        if len(html or '') >= 3000:
            return html
        if attempt < attempts:
            _emit(f"[facebook] attempt {attempt}/{attempts} returned "
                  f"{len(html or '')} chars — relaunching browser")
    return html or ''


def _fb_fetch_once(url, proxy_override=None):
    """
    One Playwright fetch of a Facebook Marketplace search page.

    `proxy_override` forces one specific proxy for this fetch instead of the
    configured FB_PROXY_URL — used to retry a blocked page on a fresh sticky
    session (see _rotate_sticky_session).
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
        # capture_fb_cookies.py writes storage_state (dict with a 'cookies' key,
        # plus localStorage); a plain cookie array from a browser extension also works.
        cookies_file = os.getenv('FB_COOKIES_FILE', '').strip()
        storage_state = None
        plain_cookies = None
        if cookies_file and os.path.isfile(cookies_file):
            try:
                with open(cookies_file, 'r') as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and 'cookies' in raw:
                    storage_state = cookies_file
                else:
                    plain_cookies = raw if isinstance(raw, list) else raw.get('cookies') or []
            except Exception:
                pass

        ctx = _pw_new_context(browser, stealth=True, platform='facebook', storage_state=storage_state,
                              proxy_override=proxy_override)
        if plain_cookies:
            try:
                ctx.add_cookies(plain_cookies)
            except Exception:
                pass
        page = ctx.new_page()
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

    # Needed to apply the distance slider ourselves — see the filter below.
    user_coords = _zip_to_latlon(zip_code)
    skipped_far = 0
    if search_radius and not user_coords and log_callback:
        log_callback(
            user_id,
            f"Could not geocode {zip_code} — Facebook distance filtering is off for this scan.",
            'info',
        )

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

            html = _fb_fetch_with_playwright(url)
            rotated = None
            if _fb_html_is_blocked(html):
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
                    html = _fb_fetch_with_playwright(url, proxy_override=rotated)

            if _fb_html_is_blocked(html):
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

            raw_items = _fb_collect_from_html(html)
            if debug and log_callback:
                log_callback(user_id, f"Facebook '{term}': scanned {len(raw_items)} rows (Playwright)", 'info')

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
                        'listed_at': None,
                        'location': (f'{city} · {distance_mi:.0f} mi' if (city and distance_mi is not None)
                                     else (city or f'Facebook · {zip_code}')),
                    })
                except Exception:
                    continue
            time.sleep(2)
        except Exception as e:
            if log_callback:
                log_callback(
                    user_id,
                    f"Facebook (Playwright) error on '{term}': {e.__class__.__name__}: {str(e)[:220]}",
                    'error',
                )

    if debug and log_callback:
        far_note = f' ({skipped_far} beyond {search_radius} mi)' if skipped_far else ''
        log_callback(user_id, f'Facebook complete: {len(listings)} candidate matches{far_note}', 'info')
    return listings


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

    try:
        offerup_term_delay = float(os.getenv('OFFERUP_TERM_DELAY_SEC', '12'))
    except ValueError:
        offerup_term_delay = 12.0

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

    search_terms = get_user_search_terms(user_id)
    if not search_terms:
        if log_callback: log_callback(user_id, "No search terms found. Scanner idle.", "error")
        return 0

    if log_callback:
        parts = []
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
            parts.append(f"{t!r} ({bounds}{suffix})")
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

    all_listings = []

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

    if user_config['platforms'].get('craigslist') and _delivery_allows('craigslist'):
        if not is_user_active(user_id):
            if log_callback:
                log_callback(user_id, "Scrape stopped by user before Craigslist.", "info")
            return 0
        all_listings.extend(
            scrape_craigslist_for_user(user_id, zip_code, user_config['search_radius'], search_terms, exclusions,
                                       user_config['ai_enabled'], user_config['ai_strictness'], debug, log_callback,
                                       known_links=known_links))

    if user_config['platforms'].get('offerup') and _delivery_allows('offerup'):
        if not is_user_active(user_id):
            if log_callback:
                log_callback(user_id, "Scrape stopped by user before OfferUp.", "info")
            return 0
        all_listings.extend(
            scrape_offerup_for_user(user_id, zip_code, user_config['search_radius'], search_terms, exclusions,
                                    user_config['ai_enabled'], user_config['ai_strictness'], debug, log_callback,
                                    known_links=known_links))

    if user_config['platforms'].get('mercari') and _delivery_allows('mercari'):
        if not is_user_active(user_id):
            if log_callback:
                log_callback(user_id, "Scrape stopped by user before Mercari.", "info")
            return 0
        all_listings.extend(
            scrape_mercari_for_user(user_id, zip_code, user_config['search_radius'], search_terms, exclusions,
                                    user_config['ai_enabled'], user_config['ai_strictness'], debug, log_callback,
                                    known_links=known_links))

    if user_config['platforms'].get('facebook') and _delivery_allows('facebook'):
        if not is_user_active(user_id):
            if log_callback:
                log_callback(user_id, "Scrape stopped by user before Facebook.", "info")
            return 0
        if (user_config.get('plan_tier') or '').strip().lower() == 'pro':
            all_listings.extend(
                scrape_facebook_for_user(user_id, zip_code, user_config['search_radius'], search_terms, exclusions,
                                         user_config['ai_enabled'], user_config['ai_strictness'], debug, log_callback,
                                         known_links=known_links))
        elif log_callback:
            log_callback(user_id, "Facebook Marketplace requires a Pro plan.", "info")

    skipped_seen_or_link_blocked = 0
    skipped_fingerprint_blocked = 0
    skipped_recent_dupe = 0
    skipped_buyer_delivery = 0
    saved_count = 0
    new_listings = []
    to_save = []
    cycle_fp_to_prices = {}
    for listing in all_listings:
        if not is_user_active(user_id):
            if log_callback:
                log_callback(user_id, "Scrape stopped by user during result processing.", "info")
            break
        link = listing['link']
        fp = _title_fingerprint(listing.get('title'))
        listing['title_fingerprint'] = fp
        price = float(listing.get('price') or 0)

        if link in seen_listings or link in blocked_links:
            skipped_seen_or_link_blocked += 1
            continue
        if fp and fp in blocked_fingerprints:
            skipped_fingerprint_blocked += 1
            continue

        # Fuzzy-ish duplicate gate across marketplaces:
        # same normalized title fingerprint and close price (+/- $10)
        recent_prices = recent_fp_to_prices.get(fp, [])
        cycle_prices = cycle_fp_to_prices.get(fp, [])
        if fp and any(abs(price - p) <= 10 for p in recent_prices + cycle_prices):
            skipped_recent_dupe += 1
            continue

        if not listing_matches_buyer_delivery_prefs(
            listing,
            user_config['buyer_include_local'],
            user_config['buyer_include_shipping'],
        ):
            skipped_buyer_delivery += 1
            continue

        # Collected rather than written here: one bulk insert after the loop
        # replaces one connection per listing. Reserve the fingerprint now so
        # the in-cycle duplicate gate above still sees it.
        to_save.append(listing)
        cycle_fp_to_prices.setdefault(fp, []).append(price)

    if to_save:
        inserted_links = save_listings_bulk(user_id, to_save)
        for listing in to_save:
            if listing['link'] not in inserted_links:
                continue
            new_listings.append(listing)
            saved_count += 1
            if log_callback:
                log_callback(
                    user_id,
                    f"New match: {listing['title'][:40]} — ${listing['price']}",
                    "success"
                )

    if new_listings:
        _notify_scrape_digest(user_id, new_listings, notify_prefs, log_callback)

    if debug and log_callback:
        log_callback(
            user_id,
            (
                f"Filter summary: candidates={len(all_listings)} "
                f"saved={saved_count} "
                f"seen_or_link_blocked={skipped_seen_or_link_blocked} "
                f"fingerprint_blocked={skipped_fingerprint_blocked} "
                f"recent_dupe={skipped_recent_dupe} "
                f"buyer_local_shipping_prefs={skipped_buyer_delivery}"
            ),
            "info",
        )

    if log_callback and len(new_listings) == 0:
        log_callback(user_id, "Scan complete. No new matches found.", "info")

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

            conn = get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)

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

            current_time = time.time()
            total_new = 0

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

                # 2. THE CLOCK: Has enough time passed since their last scrape?
                if current_time - last_scraped >= interval_seconds:
                    try:
                        new_count = scrape_for_user(user_cfg, log_callback=log_callback, debug=True)
                        total_new += new_count
                    except Exception as e:
                        # A crash here used to be invisible: it printed to server
                        # stdout only, while the finally-block stamp started the
                        # user's countdown. From the dashboard that looked like a
                        # scan that ran in 0s and found nothing. Surface it to the
                        # user AND give the interval back.
                        print(f"  ❌ [{uid}] Error: {e}", flush=True)
                        traceback.print_exc()
                        if log_callback:
                            log_callback(
                                uid,
                                f"Scan failed: {type(e).__name__}: {e}",
                                'error',
                            )
                            log_callback(
                                uid,
                                "This scan did not count against your interval — retrying shortly.",
                                'info',
                            )
                        _clear_scrape_stamp_for_retry(uid)
                else:
                    # Waiting for this user's interval. Say so rather than going
                    # silent — an unexplained quiet dashboard is indistinguishable
                    # from a broken scraper.
                    if log_callback:
                        wait_s = int(interval_seconds - (current_time - last_scraped))
                        if wait_s > 0 and cycle % 5 == 1:
                            mins, secs = divmod(wait_s, 60)
                            log_callback(
                                uid,
                                f"Idle — next scan in {mins}m {secs:02d}s.",
                                'info',
                            )

            print(f"\n✅ Total new listings this cycle: {total_new}", flush=True)
            print(f"⏳ Cycle complete. Waiting 1 minute...", flush=True)

            cursor.close()
            conn.close()
            sys.stdout.flush()

            # Report health check timestamp
            if health_callback:
                try:
                    health_callback()
                except Exception:
                    pass

            time.sleep(60)

        except Exception as e:
            print(f"❌ Error: {e}", flush=True)
            time.sleep(60)

if __name__ == "__main__":
    main()