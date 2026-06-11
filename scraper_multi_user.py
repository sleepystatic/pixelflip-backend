import time
import os
import sys
import re
import json
import hashlib
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import psycopg2
from psycopg2 import errorcodes
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse, quote_plus, urlencode
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

print("Multi-user scraper loaded", flush=True)

# Users currently in scrape_for_user (same process as Flask when ENABLE_SCRAPER_THREAD=1).
SCRAPING_USERS = set()
_SCRAPING_LOCK = threading.Lock()
_SB_BUDGET = threading.local()


def set_user_scraping(user_id, active):
    with _SCRAPING_LOCK:
        if active:
            SCRAPING_USERS.add(user_id)
        else:
            SCRAPING_USERS.discard(user_id)


def is_user_scraping(user_id):
    with _SCRAPING_LOCK:
        return user_id in SCRAPING_USERS


def is_user_active(user_id):
    """Live check used for mid-scrape abort support."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT is_active FROM user_settings WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        if row is None:
            return True
        return bool(row[0])
    except Exception:
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
    """Get database connection"""
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        raise Exception("DATABASE_URL not set")

    url = urlparse(database_url)
    conn = psycopg2.connect(
        host=url.hostname,
        port=url.port or 5432,
        database=url.path[1:],
        user=url.username,
        password=url.password,
        sslmode='require',
        connect_timeout=10
    )
    try:
        from db_schema import ensure_buyer_delivery_columns
        ensure_buyer_delivery_columns(conn)
    except Exception as schema_err:
        print(f"Schema ensure (buyer prefs) warning: {schema_err}", flush=True)
    return conn


# ===========================
# USER DATA FUNCTIONS
# ===========================
def get_active_users():
    """Get all users with active scraping enabled"""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

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

    cursor.close()
    conn.close()
    return users


def get_user_search_terms(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT search_term, min_price, max_price FROM user_search_terms WHERE user_id = %s', (user_id,))
    terms = {row[0]: {'min': float(row[1]), 'max': float(row[2])} for row in cursor.fetchall()}
    cursor.close()
    conn.close()
    return terms


def get_user_exclusions(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT keyword FROM user_exclusions WHERE user_id = %s', (user_id,))
    exclusions = [row[0] for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return exclusions


def get_seen_listings(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT link FROM listings WHERE user_id = %s', (user_id,))
    seen = set(row[0] for row in cursor.fetchall())
    cursor.close()
    conn.close()
    return seen


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
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO listings (user_id, title, price, link, platform, console_type, threshold, image_url, location, title_fingerprint, listed_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (link) DO NOTHING
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
                ON CONFLICT (link) DO NOTHING
            ''', (
            user_id, listing['title'], listing['price'], listing['link'], listing['platform'], listing.get('console_type'),
            listing.get('threshold'), listing.get('image_url'), listing.get('location'), listing.get('title_fingerprint')))
        inserted = cursor.rowcount > 0
        conn.commit()
        cursor.close()
        conn.close()
        return inserted
    except Exception as e:
        print(f"❌ Save error: {e}", flush=True)
        return False


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


def listing_matches_buyer_delivery_prefs(entry, buyer_include_local=True, buyer_include_shipping=True):
    """
    Soft filter using title + location phrases. Unknown / ambiguous rows are kept so we don't over-filter.
    """
    if buyer_include_local and buyer_include_shipping:
        return True
    blob = ' '.join([
        str(entry.get('title') or ''),
        str(entry.get('location') or ''),
        str(entry.get('platform') or ''),
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
    title_lower = title.lower()
    for term, thresholds in sorted(search_terms.items(), key=lambda x: len(x[0]), reverse=True):
        if _search_term_matches_title(term, title_lower):
            if thresholds['min'] <= price <= thresholds['max']:
                return True, term, thresholds['max']
    return False, None, None


def is_excluded(title, price, exclusions):
    title_lower = title.lower()
    # Optional floor (default: off). The old hard-coded $10 rule dropped $0–$9 deals and "free" posts.
    try:
        min_listing = float(os.getenv('MIN_LISTING_PRICE', '0'))
    except ValueError:
        min_listing = 0.0
    if min_listing > 0 and price < min_listing:
        return True
    for keyword in exclusions:
        if keyword.lower() in title_lower:
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
    """ISO / epoch-ish values (Bright Data) plus Mercari-style MM/DD/YY."""
    if val is None or val == '':
        return None
    dt = _parse_source_datetime(val)
    if dt:
        return dt
    if isinstance(val, str):
        return _parse_mercari_item_posted_text(val)
    return None


def _brightdata_fb_listed_at_from_row(row):
    """
    Prefer human listing dates; use `timestamp` only when nothing else parses (often collection time).
    """
    for key in (
        'listing_date',
        'creationTime',
        'createdAt',
        'listed_at',
        'posted_at',
        'date_listed',
    ):
        dt = _parse_flexible_marketplace_date(row.get(key))
        if dt:
            return dt
    return _parse_source_datetime(row.get('timestamp'))


def _env_flag(name, default=False):
    raw = (os.getenv(name) or '').strip().lower()
    if not raw:
        return default
    return raw in ('1', 'true', 'yes', 'on')


def _scrapingbee_max_calls_per_scan():
    try:
        return max(1, int(os.getenv('SCRAPINGBEE_MAX_CALLS_PER_SCAN', '12')))
    except ValueError:
        return 12


def reset_scrapingbee_budget():
    _SB_BUDGET.used = 0
    _SB_BUDGET.max_calls = _scrapingbee_max_calls_per_scan()


def scrapingbee_calls_used():
    return int(getattr(_SB_BUDGET, 'used', 0) or 0)


def _scrapingbee_budget_remaining():
    return max(0, int(getattr(_SB_BUDGET, 'max_calls', _scrapingbee_max_calls_per_scan())) - scrapingbee_calls_used())


def _mercari_listed_at_from_item_page(link, headers, polite_delay_sec):
    """
    Optional posted-date enrichment. Off by default — each ScrapingBee item fetch is expensive.
    Enable with MERCARI_FETCH_ITEM_POSTED=true (direct HTTP only unless MERCARI_ITEM_PAGE_SCRAPINGBEE=true).
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
        if out is None and _env_flag('MERCARI_ITEM_PAGE_SCRAPINGBEE', False):
            html = _mercari_fetch_with_scrapingbee(u)
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


def check_image_with_ai(image_url, ai_enabled, ai_strictness, debug=False, log_callback=None, user_id=None, platform_name=''):
    if not _ai_enabled_for_platform(platform_name or ''):
        return True
    if not ai_enabled or not GOOGLE_VISION_API_KEY or not image_url:
        return True

    positives = _vision_label_substrings_from_env('AI_IMAGE_POSITIVE_LABELS')
    negatives = _vision_label_substrings_from_env('AI_IMAGE_NEGATIVE_LABELS')
    # General marketplace default: no hardcoded category — skip Vision unless you configure labels.
    if not positives and not negatives:
        return True

    try:
        url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"
        payload = {"requests": [{"image": {"source": {"imageUri": image_url}},
                                 "features": [{"type": "LABEL_DETECTION", "maxResults": 15},
                                              {"type": "OBJECT_LOCALIZATION", "maxResults": 8}]}]}
        response = requests.post(url, json=payload, timeout=10)

        if response.status_code != 200:
            return True

        result = response.json()
        if 'responses' not in result or not result['responses']:
            return True

        data = result['responses'][0]
        labels = [label['description'].lower() for label in data.get('labelAnnotations', [])]
        objects = [obj['name'].lower() for obj in data.get('localizedObjectAnnotations', [])]
        all_detected = labels + objects

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
                               debug=False, log_callback=None):
    listings = []
    sites_env = os.getenv('CRAIGSLIST_SITES', 'stockton,sacramento,sfbay,modesto')
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
                        if is_excluded(title, entry['price'], exclusions):
                            continue
                        entry['console_type'] = matched_term
                        entry['threshold'] = max_price

                        if not check_image_with_ai(
                            entry['image_url'], ai_enabled, ai_strictness, debug, log_callback, user_id, platform_name='craigslist'
                        ):
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


def _http_session_with_retries(total=3, backoff_factor=1.5):
    """Shared session for outbound API calls (Bright Data, etc.)."""
    retry = Retry(
        total=total,
        connect=total,
        read=total,
        backoff_factor=backoff_factor,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(['GET', 'POST']),
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


_BRIGHTDATA_HTTP = None


def _brightdata_http_session():
    global _BRIGHTDATA_HTTP
    if _BRIGHTDATA_HTTP is None:
        try:
            attempts = int(os.getenv('BRIGHTDATA_HTTP_RETRIES', '3'))
        except ValueError:
            attempts = 3
        _BRIGHTDATA_HTTP = _http_session_with_retries(total=max(1, attempts), backoff_factor=2.0)
    return _BRIGHTDATA_HTTP


def _fetch_with_scrapingbee(url, country_env='SCRAPINGBEE_COUNTRY', wait_env='SCRAPINGBEE_WAIT_MS'):
    """Rendered fetch helper using ScrapingBee API."""
    key = (os.getenv('SCRAPINGBEE_API_KEY') or '').strip()
    if not key:
        return ''
    if _scrapingbee_budget_remaining() <= 0:
        return ''
    params = {
        'api_key': key,
        'url': url,
        'render_js': 'true',
        'country_code': os.getenv(country_env, 'us'),
        'wait': os.getenv(wait_env, '3500'),
    }
    # Keep this on by default for anti-bot heavy marketplaces.
    premium = (os.getenv('SCRAPINGBEE_PREMIUM_PROXY', 'true') or 'true').strip().lower()
    if premium in ('1', 'true', 'yes', 'on'):
        params['premium_proxy'] = 'true'
    stealth = (os.getenv('SCRAPINGBEE_STEALTH_PROXY', '') or '').strip().lower()
    if stealth in ('1', 'true', 'yes', 'on'):
        params['stealth_proxy'] = 'true'
    try:
        timeout = float(os.getenv('SCRAPINGBEE_TIMEOUT_SEC', '60'))
    except ValueError:
        timeout = 60.0
    try:
        r = requests.get(
            "https://app.scrapingbee.com/api/v1/",
            params=params,
            timeout=timeout,
        )
        if r.status_code == 200:
            _SB_BUDGET.used = scrapingbee_calls_used() + 1
            return r.text or ''
    except Exception:
        pass
    _SB_BUDGET.used = scrapingbee_calls_used() + 1
    return ''


def _mercari_collect_from_search_urls(urls, headers, log_callback=None, user_id=None, term=None, debug=False):
    """Try direct HTTP, then ScrapingBee across Mercari search URL variants."""
    raw_items = []
    saw_403 = False
    skip_direct = (os.getenv('MERCARI_SKIP_DIRECT', '') or '').strip().lower() in ('1', 'true', 'yes')
    sb_key = (os.getenv('SCRAPINGBEE_API_KEY') or '').strip()

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

    if raw_items or not sb_key:
        if saw_403 and debug and log_callback and user_id is not None and not sb_key:
            log_callback(
                user_id,
                f"Mercari {term!r}: blocked (HTTP 403) and SCRAPINGBEE_API_KEY is not set.",
                'error',
            )
        return raw_items, 'blocked' if saw_403 else 'empty'

    if debug and log_callback and user_id is not None:
        log_callback(
            user_id,
            f"Mercari {term!r}: direct fetch blocked — using ScrapingBee on search pages…",
            'info',
        )

    # One ScrapingBee call per search term (not per URL variant) — saves credits and ~minutes per scan.
    sb_url = urls[1] if len(urls) > 1 else urls[0]
    html = _mercari_fetch_with_scrapingbee(sb_url)
    if html:
        raw_items = _mercari_collect_from_html(html) or _mercari_items_from_json_ld(html)
        if not raw_items:
            raw_items = _mercari_extract_items_from_script_blobs(html)
        if raw_items:
            return raw_items, 'scrapingbee'

    return [], 'scrapingbee_empty'


def _mercari_fetch_with_scrapingbee(url):
    return _fetch_with_scrapingbee(url, country_env='SCRAPINGBEE_COUNTRY', wait_env='SCRAPINGBEE_WAIT_MS')


def _offerup_fetch_with_scrapingbee(url):
    return _fetch_with_scrapingbee(url, country_env='SCRAPINGBEE_COUNTRY', wait_env='SCRAPINGBEE_WAIT_MS_OFFERUP')


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
                title = (
                    (a.get('aria-label') or '').strip()
                    or (a.get_text(" ", strip=True) or '').strip()
                )
                text_blob = a.get_text(" ", strip=True) or ''
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
    """Off by default — one SB call per listing burned ~1000 credits per scan. See OFFERUP_FETCH_ITEM_POSTED."""
    if not _env_flag('OFFERUP_FETCH_ITEM_POSTED', False):
        return None
    if not link or '/item/' not in link:
        return None
    try:
        html = ''
        if _env_flag('OFFERUP_ITEM_PAGE_SCRAPINGBEE', True):
            html = _offerup_fetch_with_scrapingbee(link)
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
                            debug=False, log_callback=None):
    listings = []
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
        q = quote_plus(term)
        urls = [
            f"https://www.mercari.com/search/?keyword={q}&sortBy=2",
            f"https://www.mercari.com/us/search/?keyword={q}&sort=created_time&order=desc",
            f"https://www.mercari.com/search/?keyword={q}&sort=created_time&order=desc",
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
                    if is_excluded(title, price, exclusions):
                        continue

                    if not check_image_with_ai(
                        image_url, ai_enabled, ai_strictness, debug, log_callback, user_id, platform_name='mercari'
                    ):
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


def _effective_check_interval_minutes_for_user(row_or_cfg):
    """
    Facebook via Bright Data is billed per collection — minimum 30 minutes when Pro enables it.
    """
    try:
        stored = int(row_or_cfg.get('check_interval_minutes') or 10)
    except (TypeError, ValueError):
        stored = 10
    plat = _coerce_platforms_dict(row_or_cfg.get('platforms'))
    tier = (row_or_cfg.get('plan_tier') or _tier_from_db_row(row_or_cfg)).strip().lower()
    if tier == 'pro' and plat.get('facebook'):
        return max(stored, 30)
    return stored


# ===========================
# FACEBOOK MARKETPLACE (Bright Data Web Scraper API — discover by keyword)
#
# Official dataset id for "Facebook Marketplace — discover by keyword" (see Bright Data
# control panel → Data API / example curl): gd_lvt9iwuh6fbcwmx1a
# Override only if Bright Data shows a different dataset_id for your subscription.
#
# Example from Bright Data (sync scrape; body uses "input" array with keyword + city):
#   POST .../datasets/v3/scrape?dataset_id=gd_lvt9iwuh6fbcwmx1a&notify=false&include_errors=true
#        &type=discover_new&discover_by=keyword
#   {"input":[{"keyword":"ps5","city":"New York","date_listed":""}]}
# Per user, `city` is the zip/city string from user_settings.zip_code (same field as other platforms).
# Facebook listing age: BRIGHTDATA_FB_MAX_LISTING_AGE_DAYS (default 45) — Bright Data often returns
# older listing_date values than a typical 7-day Mercari window; override to match MAX_LISTING_AGE_DAYS if desired.
# ===========================
DEFAULT_BRIGHTDATA_FB_DATASET_ID = 'gd_lvt9iwuh6fbcwmx1a'

BRIGHTDATA_API_BASE = (os.getenv('BRIGHTDATA_API_BASE') or 'https://api.brightdata.com').rstrip('/')


def _brightdata_api_key():
    return (os.getenv('BRIGHTDATA_API_KEY') or os.getenv('BRIGHTDATA_API_TOKEN') or '').strip()


def _brightdata_headers_json():
    k = _brightdata_api_key()
    return {
        'Authorization': f'Bearer {k}',
        'Content-Type': 'application/json',
    }


def _brightdata_headers_bearer():
    k = _brightdata_api_key()
    return {'Authorization': f'Bearer {k}'}


def _brightdata_fb_one_input_row(keyword, zip_code):
    """One row inside {"input": [...]} — keyword + location + date_listed (Bright Data sample shape)."""
    row = {
        'keyword': str(keyword),
        'date_listed': os.getenv('BRIGHTDATA_FB_DATE_LISTED_DEFAULT', ''),
    }
    loc = str(zip_code or '').strip()
    if loc:
        row['city'] = loc
    return row


def _brightdata_parse_items_response(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get('data'), list):
            return data['data']
        if isinstance(data.get('items'), list):
            return data['items']
    return []


def _brightdata_collect_facebook_keyword(keyword, limit_per_input, zip_code, max_wait_sec, poll_sec, log_callback, user_id):
    """
    Run Bright Data Facebook Marketplace discover-by-keyword for one search term.
    Prefers synchronous /scrape (matches Bright Data UI curl); on HTTP 202, polls snapshot.

    Bright Data often keeps the POST connection open until the scrape finishes (sync mode),
    which can take many minutes. Use BRIGHTDATA_FB_SCRAPE_READ_TIMEOUT_SEC (default 900)
    for the read timeout; BRIGHTDATA_FB_SCRAPE_CONNECT_TIMEOUT_SEC (default 30) for connect.
    """
    try:
        connect_to = float(os.getenv('BRIGHTDATA_FB_SCRAPE_CONNECT_TIMEOUT_SEC', '30'))
    except ValueError:
        connect_to = 30.0
    try:
        read_to = float(os.getenv('BRIGHTDATA_FB_SCRAPE_READ_TIMEOUT_SEC', '900'))
    except ValueError:
        read_to = 900.0
    req_timeout = (connect_to, read_to)

    dataset_id = (os.getenv('BRIGHTDATA_FB_DATASET_ID') or DEFAULT_BRIGHTDATA_FB_DATASET_ID).strip()
    params = {
        'dataset_id': dataset_id,
        'notify': 'false',
        'include_errors': 'true',
        'type': 'discover_new',
        'discover_by': 'keyword',
        'limit_per_input': str(int(limit_per_input)),
        'format': 'json',
    }
    body = {'input': [_brightdata_fb_one_input_row(keyword, zip_code)]}
    url = f'{BRIGHTDATA_API_BASE}/datasets/v3/scrape'

    if log_callback:
        log_callback(
            user_id,
            f"Bright Data: POST /scrape for {keyword!r} (read timeout {int(read_to)}s; sync responses can take several minutes)…",
            'info',
        )
    t0 = time.time()
    try:
        r = _brightdata_http_session().post(
            url, params=params, headers=_brightdata_headers_json(), json=body, timeout=req_timeout,
        )
    except requests.ConnectionError as exc:
        hint = (
            'Cannot reach Bright Data (api.brightdata.com). Check BRIGHTDATA_API_KEY, '
            'outbound HTTPS from this host, VPN/firewall, and BRIGHTDATA_API_BASE if customized.'
        )
        raise RuntimeError(f'{hint} Original: {exc}') from exc
    elapsed = time.time() - t0
    if log_callback:
        log_callback(
            user_id,
            f'Bright Data: HTTP {r.status_code} after {elapsed:.1f}s',
            'info',
        )
    if r.status_code >= 400:
        raise RuntimeError(f'Bright Data scrape HTTP {r.status_code}: {(r.text or "")[:600]}')

    if r.status_code == 202:
        data = r.json() if r.content else {}
        sid = (data or {}).get('snapshot_id')
        if not sid:
            raise RuntimeError(f'Bright Data scrape 202 but no snapshot_id: {str(data)[:300]}')
        if log_callback:
            log_callback(user_id, f'Bright Data: snapshot {sid} — polling until ready…', 'info')
        ready = _brightdata_poll_until_ready(sid, max_wait_sec, poll_sec, log_callback, user_id)
        if ready is None:
            return None
        if log_callback:
            log_callback(user_id, 'Bright Data: downloading snapshot…', 'info')
        return _brightdata_download_snapshot(sid)

    data = r.json() if r.content else []
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return []
    return _brightdata_parse_items_response(data)


def _brightdata_poll_until_ready(snapshot_id, max_wait_sec, poll_sec, log_callback, user_id):
    deadline = time.time() + float(max_wait_sec)
    last_log = 0.0
    poll_iv = max(2.0, float(poll_sec))
    while time.time() < deadline:
        if not is_user_active(user_id):
            return None
        url = f'{BRIGHTDATA_API_BASE}/datasets/v3/progress/{snapshot_id}'
        r = _brightdata_http_session().get(url, headers=_brightdata_headers_bearer(), timeout=45)
        if r.status_code >= 400:
            raise RuntimeError(f'Bright Data progress HTTP {r.status_code}: {(r.text or "")[:300]}')
        st = (r.json() or {}).get('status')
        if st == 'ready':
            return True
        if st == 'failed':
            raise RuntimeError('Bright Data snapshot failed')
        now = time.time()
        if log_callback and (now - last_log) >= 30.0:
            left = max(0, int(deadline - now))
            log_callback(
                user_id,
                f'Bright Data: snapshot {snapshot_id} status={st!r} (~{left}s left before timeout)',
                'info',
            )
            last_log = now
        time.sleep(poll_iv)
    raise TimeoutError(f'Bright Data snapshot {snapshot_id} not ready within {max_wait_sec}s')


def _brightdata_download_snapshot(snapshot_id):
    try:
        dl_to = float(os.getenv('BRIGHTDATA_FB_DOWNLOAD_TIMEOUT_SEC', '300'))
    except ValueError:
        dl_to = 300.0
    url = f'{BRIGHTDATA_API_BASE}/datasets/v3/snapshot/{snapshot_id}'
    r = _brightdata_http_session().get(
        url, params={'format': 'json'}, headers=_brightdata_headers_bearer(), timeout=(30.0, dl_to),
    )
    if r.status_code >= 400:
        raise RuntimeError(f'Bright Data snapshot download HTTP {r.status_code}: {(r.text or "")[:400]}')
    if not r.content:
        return []
    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and 'data' in data:
        d = data['data']
        return d if isinstance(d, list) else []
    return []


def _brightdata_fb_row_dict(item):
    """Bright Data rows may nest the listing under data/listing/output/etc.; merge for field lookup."""
    if not isinstance(item, dict):
        return {}
    merged = {}
    for k in ('listing', 'item', 'product', 'data', 'output', 'result', 'record', 'fields'):
        v = item.get(k)
        if isinstance(v, dict):
            merged.update(v)
    merged.update(item)
    return merged


def _fb_coerce_price_value(v):
    """Parse price from Bright Data scalars, dicts, or strings like '120', '$120', 'Free'."""
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ('amount', 'value', 'raw', 'display', 'text', 'final_price', 'initial_price'):
            p = _fb_coerce_price_value(v.get(k))
            if p is not None:
                return p
        return extract_price(str(v))
    s = str(v).strip()
    if not s:
        return None
    low = s.lower()
    if low in ('free', 'gratis', 'n/a', '—', '-'):
        return 0.0
    try:
        return float(s.replace(',', '').replace(' ', ''))
    except ValueError:
        pass
    p = extract_price(s)
    if p is not None:
        return p
    m = re.search(r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:usd|us\$|\$)?\s*$', low)
    if m:
        try:
            return float(m.group(1).replace(',', ''))
        except ValueError:
            pass
    return None


def _fb_marketplace_url_from_id(listing_id):
    if listing_id is None or listing_id == '':
        return ''
    s = str(listing_id).strip()
    if not s.isdigit():
        return ''
    return f'https://www.facebook.com/marketplace/item/{s}/'


def _normalize_fb_listing(item, zip_code, search_radius):
    """Map Bright Data Facebook Marketplace rows into our listing shape."""
    row = _brightdata_fb_row_dict(item)

    title = (
        row.get('title')
        or row.get('marketplace_listing_title')
        or row.get('listing_title')
        or row.get('name')
        or row.get('headline')
        or row.get('text')
        or row.get('description')
        or ''
    )
    if isinstance(title, dict):
        title = (title.get('text') or title.get('title') or '')
    title = str(title).strip()

    link = (
        row.get('url')
        or row.get('listingUrl')
        or row.get('link')
        or row.get('listing_url')
        or row.get('product_url')
        or row.get('page_url')
        or row.get('permalink')
        or row.get('canonical_url')
        or ''
    )
    link = str(link).strip()
    if link and not link.startswith('http'):
        link = f'https://www.facebook.com{link}' if link.startswith('/') else link
    if not link or 'marketplace/item' not in link:
        for key in ('listing_id', 'post_id', 'product_id', 'marketplace_id', 'id'):
            cand = row.get(key)
            if cand is not None and str(cand).strip().isdigit():
                link = _fb_marketplace_url_from_id(cand)
                break

    price = None
    for key in (
        'final_price',
        'initial_price',
        'price',
        'listing_price',
        'current_price',
        'min_price',
        'max_price',
        'price_text',
        'display_price',
        'formatted_price',
    ):
        price = _fb_coerce_price_value(row.get(key))
        if price is not None:
            break

    img = None
    imgs = row.get('images') or row.get('photos') or row.get('image_urls')
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        img = first if isinstance(first, str) else (first.get('url') or first.get('uri') or first.get('image'))
    if not img:
        img = row.get('primaryListingPhoto') or row.get('image') or row.get('thumbnail') or row.get('photo')
    if isinstance(img, dict):
        img = img.get('uri') or img.get('url')

    listed_at = _brightdata_fb_listed_at_from_row(row)

    loc_str = row.get('location') or row.get('country_code') or row.get('city') or ''
    if isinstance(loc_str, dict):
        loc_str = str(loc_str.get('name') or loc_str.get('city') or '')

    return {
        'title': title,
        'price': price,
        'link': link,
        'platform': 'Facebook',
        'console_type': None,
        'threshold': None,
        'image_url': img if isinstance(img, str) else None,
        'listed_at': listed_at,
        'location': f'Facebook · {loc_str or zip_code} ({search_radius} mi)',
    }


def scrape_facebook_for_user(user_id, zip_code, search_radius, search_terms, exclusions,
                             ai_enabled, ai_strictness, debug=False, log_callback=None):
    listings = []
    if not _brightdata_api_key():
        if debug and log_callback:
            log_callback(user_id, 'Facebook skipped: set BRIGHTDATA_API_KEY in the environment.', 'info')
        return listings

    if log_callback:
        log_callback(user_id, 'Waking up Facebook Marketplace (Bright Data)...', 'info')

    try:
        max_age_days = int(os.getenv('BRIGHTDATA_FB_MAX_LISTING_AGE_DAYS', '45'))
    except ValueError:
        max_age_days = 45
    try:
        limit_per_input = int(os.getenv('BRIGHTDATA_FB_LIMIT_PER_INPUT', '40'))
    except ValueError:
        limit_per_input = 40
    try:
        max_wait_sec = int(os.getenv('BRIGHTDATA_FB_MAX_WAIT_SEC', '900'))
    except ValueError:
        max_wait_sec = 900
    try:
        poll_sec = float(os.getenv('BRIGHTDATA_FB_POLL_SEC', '5'))
    except ValueError:
        poll_sec = 5.0

    for term in search_terms.keys():
        if not is_user_active(user_id):
            if debug and log_callback:
                log_callback(user_id, 'Facebook stopped by user.', 'info')
            return listings

        try:
            if debug and log_callback:
                log_callback(user_id, f"Facebook '{term}': requesting Bright Data scrape…", 'info')

            raw_items = _brightdata_collect_facebook_keyword(
                term, limit_per_input, zip_code, max_wait_sec, poll_sec, log_callback, user_id,
            )
            if raw_items is None:
                if log_callback:
                    log_callback(user_id, "Facebook: stopped while waiting for Bright Data.", "info")
                return listings

            if debug and log_callback:
                log_callback(user_id, f"Facebook '{term}': received {len(raw_items)} rows", "info")

            before_ct = len(listings)
            skip = {'missing_core': 0, 'too_old': 0, 'price_threshold': 0, 'excluded': 0, 'ai_image': 0, 'row_error': 0}

            for item in raw_items:
                if not is_user_active(user_id):
                    return listings
                try:
                    if isinstance(item, dict) and item.get('error'):
                        skip['row_error'] += 1
                        continue
                    entry = _normalize_fb_listing(item, zip_code, search_radius)
                    title, price, link = entry['title'], entry['price'], entry['link']
                    if not (title and price is not None and link):
                        skip['missing_core'] += 1
                        continue

                    la = entry.get('listed_at')
                    listed_for_age = la.isoformat() if isinstance(la, datetime) else None
                    if listed_for_age and not _is_recent_timestamp(listed_for_age, max_age_days):
                        skip['too_old'] += 1
                        continue

                    meets_threshold, matched_term, max_price = check_price_threshold(title, price, search_terms)
                    if not meets_threshold:
                        # Discover-by-keyword results are already scoped to `term`; titles often omit
                        # the exact phrase ("GBA SP" vs "gameboy advance sp"). Accept when price fits this term.
                        th = search_terms.get(term)
                        if th is not None and th['min'] <= price <= th['max']:
                            meets_threshold, matched_term, max_price = True, term, th['max']
                    if not meets_threshold:
                        skip['price_threshold'] += 1
                        continue
                    if is_excluded(title, price, exclusions):
                        skip['excluded'] += 1
                        continue

                    entry['console_type'] = matched_term
                    entry['threshold'] = max_price

                    if not check_image_with_ai(
                        entry['image_url'], ai_enabled, ai_strictness,
                        debug, log_callback, user_id, platform_name='facebook',
                    ):
                        skip['ai_image'] += 1
                        continue

                    listings.append(entry)
                except Exception:
                    skip['row_error'] += 1
                    continue

            if debug and log_callback and raw_items and len(listings) == before_ct:
                log_callback(
                    user_id,
                    f"Facebook '{term}': 0 matches after filters from {len(raw_items)} rows — "
                    f"missing title/price/link: {skip['missing_core']}, too old: {skip['too_old']}, "
                    f"price vs search terms: {skip['price_threshold']}, excluded: {skip['excluded']}, "
                    f"AI image: {skip['ai_image']}, row errors: {skip['row_error']}",
                    'info',
                )
                sample = raw_items[0]
                if isinstance(sample, dict):
                    keys = list(sample.keys())
                    preview = ', '.join(keys[:35])
                    if len(keys) > 35:
                        preview += f' … (+{len(keys) - 35} more keys)'
                    log_callback(user_id, f"Facebook '{term}': sample row top-level keys: {preview}", 'info')
                    probe = _normalize_fb_listing(sample, zip_code, search_radius)
                    log_callback(
                        user_id,
                        f"Facebook '{term}': sample normalized title={probe['title'][:80]!r} "
                        f"price={probe['price']!r} link={str(probe['link'] or '')[:90]!r}",
                        'info',
                    )

            time.sleep(1)
        except Exception as e:
            if log_callback:
                log_callback(
                    user_id,
                    f"Facebook (Bright Data) error on '{term}': {e.__class__.__name__}: {str(e)[:220]}",
                    'error',
                )

    if debug and log_callback:
        log_callback(user_id, f'Facebook complete: {len(listings)} candidate matches', 'info')
    return listings


# ===========================
# OFFERUP SCRAPER
# ===========================
def scrape_offerup_for_user(user_id, zip_code, search_radius, search_terms, exclusions, ai_enabled, ai_strictness,
                            debug=False, log_callback=None):
    listings = []

    if log_callback: log_callback(user_id, "Waking up OfferUp scraper...", "info")
    max_age_days = int(os.getenv('MAX_LISTING_AGE_DAYS', '7'))
    try:
        offerup_detail_delay = float(os.getenv('OFFERUP_ITEM_PAGE_DELAY_SEC', '0.45'))
    except ValueError:
        offerup_detail_delay = 0.45
    try:
        offerup_max_rows = int(os.getenv('OFFERUP_MAX_ROWS_PER_TERM', '30'))
    except ValueError:
        offerup_max_rows = 30

    try:
        for term in search_terms.keys():
            if not is_user_active(user_id):
                if debug and log_callback:
                    log_callback(user_id, "OfferUp stopped by user.", "info")
                return listings
            try:
                # Newest first (OfferUp UI: Sort → Recent first → value "-posted")
                sort_param = (os.getenv('OFFERUP_SORT', '-posted') or '-posted').strip()
                url = 'https://offerup.com/search/?' + urlencode({
                    'q': term,
                    'radius': search_radius,
                    'sort': sort_param,
                    'postal_code': str(zip_code or ''),
                })
                if log_callback: log_callback(user_id, f"Scanning OfferUp for '{term}'...", "info")

                html = _offerup_fetch_with_scrapingbee(url)
                items = _offerup_collect_from_html(html)
                if debug and log_callback:
                    html_len = len(html or '')
                    log_callback(
                        user_id,
                        f"OfferUp '{term}': scanned {len(items[:50])} rows (via ScrapingBee, html_len={html_len})",
                        "info",
                    )
                    if '/unavailable/blk' in (html or ''):
                        log_callback(
                            user_id,
                            "OfferUp rendered content still appears blocked/challenged.",
                            "error",
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
                        meets_threshold, matched_term, max_price = check_price_threshold(title, price, search_terms)
                        if not meets_threshold: continue
                        if is_excluded(title, price, exclusions): continue

                        image_url = item.get('image_url')

                        if not check_image_with_ai(
                            image_url, ai_enabled, ai_strictness, debug, log_callback, user_id, platform_name='offerup'
                        ):
                            continue

                        listed_at = item.get('listed_at')
                        if listed_at is None:
                            listed_at = _offerup_listed_at_from_item_page(link, offerup_detail_delay)
                        listed_for_age = listed_at.isoformat() if isinstance(listed_at, datetime) else listed_at
                        if listed_for_age and not _is_recent_timestamp(listed_for_age, max_age_days):
                            continue

                        listings.append({
                            'title': title, 'price': price, 'link': link, 'platform': 'OfferUp',
                            'console_type': matched_term, 'threshold': max_price,
                            'image_url': image_url,
                            'listed_at': listed_at,
                            'location': f'OfferUp · {zip_code} ({search_radius} mi)'
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
                f"OfferUp unavailable: {e.__class__.__name__}: {detail}. Check SCRAPINGBEE_API_KEY.",
                "error"
            )
    if debug and log_callback:
        log_callback(user_id, f"OfferUp complete: {len(listings)} candidate matches", "info")
    return listings


# ===========================
# USER SCRAPER
# ===========================
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
    try:
        return _scrape_for_user_impl(user_config, log_callback=log_callback, debug=debug)
    finally:
        duration_ms = int((time.time() - started) * 1000)
        _stamp_user_scrape_complete(user_id, duration_ms)
        set_user_scraping(user_id, False)


def _scrape_for_user_impl(user_config, log_callback=None, debug=False):
    user_id = user_config['user_id']
    reset_scrapingbee_budget()
    zip_code = user_config['zip_code']
    user_config = {
        **user_config,
        'platforms': _coerce_platforms_dict(user_config.get('platforms')),
        'buyer_include_local': bool(user_config.get('buyer_include_local', True)),
        'buyer_include_shipping': bool(user_config.get('buyer_include_shipping', True)),
    }
    if not user_config['buyer_include_local'] and not user_config['buyer_include_shipping']:
        user_config['buyer_include_local'] = True

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
            lo = rng.get('min', 0)
            hi = rng.get('max', 0)
            parts.append(f"{t!r} (${lo}-${hi})")
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
    recent_sigs = get_recent_listing_signatures(user_id)
    recent_fp_to_prices = {}
    for row in recent_sigs:
        fp = row.get('title_fingerprint')
        if not fp:
            continue
        recent_fp_to_prices.setdefault(fp, []).append(float(row.get('price') or 0))

    all_listings = []

    if user_config['platforms'].get('craigslist'):
        if not is_user_active(user_id):
            if log_callback:
                log_callback(user_id, "Scrape stopped by user before Craigslist.", "info")
            return 0
        all_listings.extend(
            scrape_craigslist_for_user(user_id, zip_code, user_config['search_radius'], search_terms, exclusions,
                                       user_config['ai_enabled'], user_config['ai_strictness'], debug, log_callback))

    if user_config['platforms'].get('offerup'):
        if not is_user_active(user_id):
            if log_callback:
                log_callback(user_id, "Scrape stopped by user before OfferUp.", "info")
            return 0
        all_listings.extend(
            scrape_offerup_for_user(user_id, zip_code, user_config['search_radius'], search_terms, exclusions,
                                    user_config['ai_enabled'], user_config['ai_strictness'], debug, log_callback))

    if user_config['platforms'].get('mercari'):
        if not is_user_active(user_id):
            if log_callback:
                log_callback(user_id, "Scrape stopped by user before Mercari.", "info")
            return 0
        all_listings.extend(
            scrape_mercari_for_user(user_id, zip_code, user_config['search_radius'], search_terms, exclusions,
                                    user_config['ai_enabled'], user_config['ai_strictness'], debug, log_callback))

    if user_config['platforms'].get('facebook'):
        if not is_user_active(user_id):
            if log_callback:
                log_callback(user_id, "Scrape stopped by user before Facebook.", "info")
            return 0
        if (user_config.get('plan_tier') or '').strip().lower() == 'pro':
            all_listings.extend(
                scrape_facebook_for_user(user_id, zip_code, user_config['search_radius'], search_terms, exclusions,
                                         user_config['ai_enabled'], user_config['ai_strictness'], debug, log_callback))
        elif log_callback:
            log_callback(user_id, "Facebook Marketplace is a Pro feature (Bright Data). Enable Pro to scan.", "info")

    skipped_seen_or_link_blocked = 0
    skipped_fingerprint_blocked = 0
    skipped_recent_dupe = 0
    skipped_buyer_delivery = 0
    saved_count = 0
    new_listings = []
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

        if save_listing(user_id, listing):
            new_listings.append(listing)
            saved_count += 1
            cycle_fp_to_prices.setdefault(fp, []).append(price)
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

    sb_used = scrapingbee_calls_used()
    sb_max = int(getattr(_SB_BUDGET, 'max_calls', _scrapingbee_max_calls_per_scan()))
    if log_callback and sb_used > 0:
        log_callback(
            user_id,
            f"ScrapingBee calls this scan: {sb_used}/{sb_max} (cap via SCRAPINGBEE_MAX_CALLS_PER_SCAN)",
            'info',
        )

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
    print("(Craigslist, OfferUp, Mercari; Facebook Marketplace when Pro + BRIGHTDATA_API_KEY)", flush=True)
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
                        print(f"  ❌ [{uid}] Error: {e}", flush=True)
                else:
                    # Skip quietly until it's their turn
                    pass

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