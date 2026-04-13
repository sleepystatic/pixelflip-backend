import time
import os
import sys
import re
import json
import hashlib
import shutil
import threading
import requests
import psycopg2
from psycopg2 import errorcodes
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse, urlunparse, quote_plus, quote
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

print("✅ Multi-user scraper loaded", flush=True)

# Users currently in scrape_for_user (same process as Flask when ENABLE_SCRAPER_THREAD=1).
SCRAPING_USERS = set()
_SCRAPING_LOCK = threading.Lock()


def set_user_scraping(user_id, active):
    with _SCRAPING_LOCK:
        if active:
            SCRAPING_USERS.add(user_id)
        else:
            SCRAPING_USERS.discard(user_id)


def resolve_selenium_remote_url():
    """
    Normalize remote WebDriver URL for Browserless.

    Selenium 4 builds Basic auth from URL userinfo as f"{username}:{password}".
    If you use https://TOKEN@host with no password, urllib parses password as None and Selenium
    encodes the literal "TOKEN:None" — Browserless rejects that with 401.
    Always use an explicit empty password: https://TOKEN:@host/webdriver
    """
    base = (os.getenv('SELENIUM_REMOTE_URL') or '').strip()
    if not base:
        return None
    # Common .env mistake: value includes "SELENIUM_REMOTE_URL=" prefix.
    if '=' in base and base.lower().startswith('selenium_remote_url='):
        base = base.split('=', 1)[1].strip()
    # Strip wrapping quotes from env values.
    if (base.startswith('"') and base.endswith('"')) or (base.startswith("'") and base.endswith("'")):
        base = base[1:-1].strip()
    # Common typo: duplicated scheme (https://https://...)
    base = re.sub(r'^(https?://)(https?://)', r'\2', base, flags=re.I)
    token = (os.getenv('BROWSERLESS_TOKEN') or os.getenv('BROWSERLESS_API_KEY') or '').strip()
    try:
        p = urlparse(base)
        if p.scheme not in ('http', 'https') or not p.netloc:
            return None
        netloc_lower = (p.netloc or '').lower()
        if 'browserless' not in netloc_lower:
            return base

        hostname = p.hostname or ''
        port = p.port
        host_only = f'{hostname}:{port}' if port else hostname

        path = (p.path or '').strip()
        if not path or path == '/' or path.endswith('/chrome') or path.endswith('/chromium') or path.endswith('/content'):
            path = '/webdriver'
        if '/webdriver' not in path:
            path = '/webdriver'

        # User supplied credentials in URL — fix token@host (no password) for Selenium Basic auth.
        if p.username is not None and str(p.username).strip() != '':
            if p.password is None:
                safe_user = quote(p.username, safe='')
                netloc = f'{safe_user}:@{host_only}'
            else:
                netloc = p.netloc
            return urlunparse((p.scheme, netloc, path, p.params, p.query, p.fragment))

        if token:
            safe = quote(token, safe='')
            # Empty password segment so Selenium sends Basic auth for "token:" not "token:None"
            netloc = f'{safe}:@{host_only}'
            return urlunparse((p.scheme, netloc, path, p.params, '', p.fragment))

        return urlunparse((p.scheme, p.netloc, path, p.params, p.query, p.fragment))
    except Exception:
        return base


# ===========================
# DATABASE CONNECTION
# ===========================
def get_db_connection():
    """Get database connection"""
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        raise Exception("DATABASE_URL not set")

    url = urlparse(database_url)
    return psycopg2.connect(
        host=url.hostname,
        port=url.port or 5432,
        database=url.path[1:],
        user=url.username,
        password=url.password,
        sslmode='require',
        connect_timeout=10
    )


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
               plan_tier, is_pro
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
            'platforms': row['platforms'] or {'craigslist': True},
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
        for term in search_terms.keys():
            url = f"https://{subdomain}.craigslist.org/search/{cl_cat}?query={term.replace(' ', '+')}&sort=date&postal={zip_code}&search_distance={search_radius}"
            try:
                response = session.get(url, timeout=15)
                soup = BeautifulSoup(response.content, 'html.parser')
                items = soup.find_all('li', class_='cl-static-search-result')
                if debug and log_callback:
                    log_callback(user_id, f"Craigslist {subdomain} '{term}': scanned {len(items)} rows", "info")

                for item in items:
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
                        else:
                            if not entry.get('price'):
                                continue
                            try:
                                img_elem = item.find('img')
                                entry['image_url'] = _extract_lazy_image_url(img_elem)
                            except Exception:
                                pass

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
                if 'price' in obj:
                    price_obj = obj.get('price')
                    name = (obj.get('name') or obj.get('title') or '').strip()
                    pid = str(obj.get('id') or obj.get('item_id') or '').strip()
                    if name and pid and price_obj is not None:
                        if isinstance(price_obj, dict):
                            amt = price_obj.get('amount') or price_obj.get('value')
                        else:
                            amt = price_obj
                        if amt is not None:
                            try:
                                # Treat integral values as dollars by default; false cent conversion
                                # was filtering real listings out by dropping price 100x.
                                price = float(amt)
                            except (TypeError, ValueError):
                                price = extract_price(str(amt))
                            if price:
                                thumb = None
                                photos = obj.get('photos') or obj.get('thumbnails') or []
                                if photos and isinstance(photos[0], dict):
                                    thumb = photos[0].get('url') or photos[0].get('imageUrl')
                                link = f"https://www.mercari.com/us/item/{pid}/"
                                listed_dt = None
                                for lk in ('created', 'created_at', 'item_created_at', 'listed_at', 'updated', 'created_time'):
                                    if lk in obj and obj[lk] is not None:
                                        listed_dt = _parse_source_datetime(obj.get(lk))
                                        if listed_dt:
                                            break
                                out.append({
                                    'title': str(name).strip(),
                                    'price': price,
                                    'link': link,
                                    'image_url': thumb,
                                    'listed_at': listed_dt,
                                })
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(data)
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
    if not raw_items:
        soup = BeautifulSoup(html_text, 'html.parser')
        anchors = soup.select("a[href*='/item/']")
        for a in anchors[:80]:
            try:
                href = a.get('href') or ''
                if not href or '/item/' not in href:
                    continue
                link = href if href.startswith('http') else f"https://www.mercari.com{href}"
                title = (a.get('aria-label') or a.get_text(" ", strip=True) or '').strip()
                full_text = a.get_text(" ", strip=True)
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


def _mercari_fetch_with_scrapingfish(url):
    """Optional rendered fetch fallback when Selenium capacity is unavailable."""
    key = (os.getenv('SCRAPINGFISH_API_KEY') or '').strip()
    if not key:
        return ''
    try:
        r = requests.get(
            "https://api.scrapingfish.com/scrape",
            params={
                'api_key': key,
                'url': url,
                'render_js': 'true',
                'country': os.getenv('SCRAPINGFISH_COUNTRY', 'us'),
            },
            timeout=35,
        )
        if r.status_code == 200:
            return r.text or ''
    except Exception:
        pass
    return ''


def _mercari_page_source_via_selenium(term):
    driver = None
    try:
        remote = resolve_selenium_remote_url()
        if not remote:
            return ''
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument(
            'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        driver = webdriver.Remote(command_executor=remote, options=chrome_options)
        url = f"https://www.mercari.com/us/search/?keyword={term.replace(' ', '%20')}&sort=created_time&order=desc"
        driver.get(url)
        time.sleep(6)
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(2)
        return driver.page_source or ''
    except Exception as e:
        print(f"Mercari selenium fetch: {e}", flush=True)
        return ''
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def _mercari_items_via_selenium(term, debug=False, log_callback=None, user_id=None):
    """Rendered DOM fallback when Mercari blocks plain HTTP."""
    driver = None
    items = []
    try:
        remote = resolve_selenium_remote_url()
        if not remote:
            return items
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.chrome.options import Options
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument(
            'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        driver = webdriver.Remote(command_executor=remote, options=chrome_options)
        url = f"https://www.mercari.com/us/search/?keyword={quote_plus(term)}&sort=created_time&order=desc"
        driver.get(url)
        time.sleep(5)
        for _ in range(2):
            driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
            time.sleep(2)

        anchors = driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")
        if debug and log_callback and user_id:
            title = (driver.title or '').strip()
            cur = (driver.current_url or '').strip()
            log_callback(user_id, f"Mercari browser page: title='{title[:80]}' url='{cur[:120]}' anchors={len(anchors)}", "info")
        for a in anchors[:100]:
            try:
                href = a.get_attribute('href') or ''
                if not href or '/item/' not in href:
                    continue
                title = (a.get_attribute('aria-label') or a.text or '').strip()
                text_blob = a.text or ''
                price = extract_price(text_blob)
                if not (title and price):
                    continue
                image_url = None
                try:
                    img = a.find_element(By.TAG_NAME, 'img')
                    image_url = img.get_attribute('src') or img.get_attribute('data-src')
                except Exception:
                    pass
                items.append({
                    'title': title,
                    'price': price,
                    'link': href if href.startswith('http') else f"https://www.mercari.com{href}",
                    'image_url': image_url,
                    'listed_at': None,
                })
            except Exception:
                continue
    except Exception as e:
        print(f"Mercari selenium DOM fetch: {e}", flush=True)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    return _mercari_dedupe_raw(items)


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

    for term in search_terms.keys():
        q = quote_plus(term)
        urls = [
            f"https://www.mercari.com/us/search/?keyword={q}&sort=created_time&order=desc",
            f"https://www.mercari.com/search/?keyword={q}&sort=created_time&order=desc",
        ]
        try:
            raw_items = []
            for url in urls:
                response = requests.get(url, headers=headers, timeout=20)
                if response.status_code != 200:
                    if debug and log_callback:
                        log_callback(user_id, f"Mercari '{term}': HTTP {response.status_code} from {urlparse(url).path}", "error")
                    continue
                html = response.text or ''
                raw_items = _mercari_collect_from_html(html)
                if not raw_items:
                    raw_items = _mercari_items_from_json_ld(html)
                if not raw_items:
                    raw_items = _mercari_extract_items_from_script_blobs(html)
                if raw_items:
                    break

            if not raw_items and resolve_selenium_remote_url():
                if debug and log_callback:
                    log_callback(user_id, f"Mercari '{term}': empty from HTTP; trying remote browser…", "info")
                html = _mercari_page_source_via_selenium(term)
                raw_items = _mercari_collect_from_html(html) or _mercari_items_from_json_ld(html)
                if not raw_items:
                    raw_items = _mercari_items_via_selenium(term, debug=debug, log_callback=log_callback, user_id=user_id)
                if debug and log_callback and not raw_items:
                    h = (html or '').lower()
                    if 'captcha' in h or 'robot' in h or 'blocked' in h or 'cloudflare' in h:
                        log_callback(user_id, f"Mercari '{term}': browser content appears blocked/challenged.", "error")
            if not raw_items:
                html = _mercari_fetch_with_scrapingfish(urls[0])
                if html:
                    if debug and log_callback:
                        log_callback(user_id, f"Mercari '{term}': trying ScrapingFish rendered HTML…", "info")
                    raw_items = _mercari_collect_from_html(html) or _mercari_items_from_json_ld(html)

            if debug and log_callback:
                log_callback(user_id, f"Mercari '{term}': scanned {len(raw_items)} rows", "info")

            for row in raw_items[:80]:
                try:
                    title = row['title']
                    price = row['price']
                    link = row['link']
                    image_url = row.get('image_url')

                    listed_at = row.get('listed_at')
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


# ===========================
# OFFERUP SCRAPER
# ===========================
def scrape_offerup_for_user(user_id, zip_code, search_radius, search_terms, exclusions, ai_enabled, ai_strictness,
                            debug=False, log_callback=None):
    listings = []
    driver = None

    if log_callback: log_callback(user_id, "Waking up OfferUp scraper...", "info")

    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")

        remote_url = resolve_selenium_remote_url()
        if remote_url:
            # SaaS-friendly path: use remote browser infrastructure (Browserless/Selenium Grid).
            if debug and log_callback:
                log_callback(user_id, "OfferUp using remote browser endpoint", "info")
            driver = webdriver.Remote(command_executor=remote_url, options=chrome_options)
        else:
            # FIND BINARY (env override first) for local/self-hosted installs.
            chrome_bin = os.getenv('CHROME_BINARY')
            if chrome_bin and not os.path.exists(chrome_bin):
                chrome_bin = None
            if not chrome_bin:
                windows_candidates = [
                    os.path.join(os.getenv("PROGRAMFILES", r"C:\Program Files"), r"Google\Chrome\Application\chrome.exe"),
                    os.path.join(os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), r"Google\Chrome\Application\chrome.exe"),
                    os.path.join(os.getenv("LOCALAPPDATA", r"C:\Users\Default\AppData\Local"), r"Google\Chrome\Application\chrome.exe"),
                    os.path.join(os.getenv("PROGRAMFILES", r"C:\Program Files"), r"Microsoft\Edge\Application\msedge.exe"),
                ]
                chrome_bin = next(
                    (p for p in [
                        *windows_candidates,
                        '/usr/bin/google-chrome',
                        '/usr/bin/google-chrome-stable',
                        '/usr/bin/chromium',
                        '/usr/bin/chromium-browser',
                        '/opt/google/chrome/chrome',
                    ] if os.path.exists(p)),
                    None
                )
            if not chrome_bin:
                chrome_bin = (
                    shutil.which("google-chrome")
                    or shutil.which("chrome")
                    or shutil.which("chromium")
                    or shutil.which("msedge")
                )
            if chrome_bin:
                chrome_options.binary_location = chrome_bin
                if debug and log_callback:
                    log_callback(user_id, f"OfferUp browser: {chrome_bin}", "info")
            else:
                if log_callback:
                    log_callback(
                        user_id,
                        "OfferUp skipped: no browser runtime. Set SELENIUM_REMOTE_URL (recommended for SaaS) or CHROME_BINARY.",
                        "error"
                    )
                return listings

            # INITIALIZE ONCE (local mode)
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)

        # STEALTH HANDSHAKE
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            })

        for term in search_terms.keys():
            try:
                # Newest first (matches OfferUp "Recent" sort intent)
                sort_param = os.getenv('OFFERUP_SORT', '-created_at')
                url = f"https://offerup.com/search/?q={term.replace(' ', '%20')}&radius={search_radius}&sort={sort_param}"
                if log_callback: log_callback(user_id, f"Scanning OfferUp for '{term}'...", "info")

                driver.get(url)
                time.sleep(5)

                for scroll in range(3):
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(2)

                items = []
                selector_candidates = [
                    "a[href*='/item/']",
                    "a[data-testid*='listing']",
                    "a[data-testid*='feed-item']",
                    "a[href*='/search/'][href*='cid']",
                ]
                for sel in selector_candidates:
                    try:
                        nodes = driver.find_elements(By.CSS_SELECTOR, sel)
                    except Exception:
                        nodes = []
                    if nodes:
                        items = nodes
                        break
                if not items:
                    # Fallback parse from rendered HTML for dynamic markup changes.
                    try:
                        soup = BeautifulSoup(driver.page_source or '', 'html.parser')
                        links = []
                        for a in soup.select("a[href]"):
                            href = a.get('href') or ''
                            if '/item/' in href:
                                links.append(href if href.startswith('http') else f"https://offerup.com{href}")
                        # Dummy wrappers with href/text-like access not needed; handle below.
                        items = links
                    except Exception:
                        items = []
                if debug and log_callback:
                    ttl = (driver.title or '').strip()
                    log_callback(user_id, f"OfferUp '{term}': scanned {len(items[:50])} rows (title='{ttl[:70]}')", "info")

                for item in items[:50]:
                    try:
                        if isinstance(item, str):
                            link = item
                            title = ''
                            price = None
                            # Resolve link page quickly for title/price when only href was found.
                            try:
                                rr = requests.get(link, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
                                if rr.status_code == 200:
                                    ps = BeautifulSoup(rr.text, 'html.parser')
                                    tt = ps.find('title')
                                    title = (tt.get_text(" ", strip=True) if tt else '').strip()
                                    price = extract_price(ps.get_text(" ", strip=True))
                            except Exception:
                                pass
                        else:
                            link = item.get_attribute('href')
                            title = item.get_attribute('aria-label') or item.text
                            price = extract_price(item.text)

                        if not price or not link or not title: continue
                        meets_threshold, matched_term, max_price = check_price_threshold(title, price, search_terms)
                        if not meets_threshold: continue
                        if is_excluded(title, price, exclusions): continue

                        image_url = None
                        try:
                            img_elem = item.find_element(By.TAG_NAME, 'img')
                            image_url = img_elem.get_attribute('src') or img_elem.get_attribute('data-src')
                        except Exception:
                            pass

                        if not check_image_with_ai(
                            image_url, ai_enabled, ai_strictness, debug, log_callback, user_id, platform_name='offerup'
                        ):
                            continue

                        listings.append({
                            'title': title, 'price': price, 'link': link, 'platform': 'OfferUp',
                            'console_type': matched_term, 'threshold': max_price,
                            'image_url': image_url,
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
                f"OfferUp unavailable: {e.__class__.__name__}: {detail}. Check SELENIUM_REMOTE_URL / Browserless token.",
                "error"
            )
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
    if debug and log_callback:
        log_callback(user_id, f"OfferUp complete: {len(listings)} candidate matches", "info")
    return listings


# ===========================
# USER SCRAPER
# ===========================
def scrape_for_user(user_config, log_callback=None, debug=False):
    user_id = user_config['user_id']
    set_user_scraping(user_id, True)
    try:
        return _scrape_for_user_impl(user_config, log_callback=log_callback, debug=debug)
    finally:
        set_user_scraping(user_id, False)


def _scrape_for_user_impl(user_config, log_callback=None, debug=False):
    user_id = user_config['user_id']
    zip_code = user_config['zip_code']

    if log_callback: log_callback(user_id, f"Target locked: Zip Code {zip_code} ({user_config['search_radius']}mi)",
                                  "info")

    search_terms = get_user_search_terms(user_id)
    if not search_terms:
        if log_callback: log_callback(user_id, "No search terms found. Scanner idle.", "error")
        return 0

    exclusions = get_user_exclusions(user_id)
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
        all_listings.extend(
            scrape_craigslist_for_user(user_id, zip_code, user_config['search_radius'], search_terms, exclusions,
                                       user_config['ai_enabled'], user_config['ai_strictness'], debug, log_callback))

    if user_config['platforms'].get('offerup'):
        all_listings.extend(
            scrape_offerup_for_user(user_id, zip_code, user_config['search_radius'], search_terms, exclusions,
                                    user_config['ai_enabled'], user_config['ai_strictness'], debug, log_callback))

    if user_config['platforms'].get('mercari'):
        all_listings.extend(
            scrape_mercari_for_user(user_id, zip_code, user_config['search_radius'], search_terms, exclusions,
                                    user_config['ai_enabled'], user_config['ai_strictness'], debug, log_callback))

    skipped_seen_or_link_blocked = 0
    skipped_fingerprint_blocked = 0
    skipped_recent_dupe = 0
    saved_count = 0
    new_listings = []
    cycle_fp_to_prices = {}
    for listing in all_listings:
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

        if save_listing(user_id, listing):
            new_listings.append(listing)
            saved_count += 1
            cycle_fp_to_prices.setdefault(fp, []).append(price)
            # Send the success log straight to the UI!
            if log_callback:
                log_callback(
                    user_id,
                    f"New match: {listing['title'][:40]} — ${listing['price']}",
                    "success"
                )

    if debug and log_callback:
        log_callback(
            user_id,
            (
                f"Filter summary: candidates={len(all_listings)} "
                f"saved={saved_count} "
                f"seen_or_link_blocked={skipped_seen_or_link_blocked} "
                f"fingerprint_blocked={skipped_fingerprint_blocked} "
                f"recent_dupe={skipped_recent_dupe}"
            ),
            "info",
        )

    if log_callback and len(new_listings) == 0:
        log_callback(user_id, "Scan complete. No new matches found.", "info")

    return len(new_listings)


# ===========================
# MAIN LOOP
# ===========================
def main(log_callback=None):
    if log_callback is None:
        def default_log(uid, msg, ltype="info"):
            print(f"[{uid[:8]}] {msg}", flush=True)
        log_callback = default_log

    print("=" * 60, flush=True)
    print("PIXELFLIP MASTER CLOCK SCRAPER", flush=True)
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
                       EXTRACT(EPOCH FROM last_scraped_at) as last_scraped_ts 
                FROM user_settings 
                WHERE is_active = TRUE;
            """)
            active_users = cursor.fetchall()

            current_time = time.time()
            total_new = 0

            for user in active_users:
                uid = user['user_id']
                last_scraped = float(user['last_scraped_ts']) if user['last_scraped_ts'] else 0.0
                interval_seconds = (user['check_interval_minutes'] or 10) * 60

                # 2. THE CLOCK: Has enough time passed since their last scrape?
                if current_time - last_scraped >= interval_seconds:
                    try:
                        started = time.time()
                        # Run the scrape
                        new_count = scrape_for_user(user, log_callback=log_callback, debug=True)
                        total_new += new_count
                        duration_ms = int((time.time() - started) * 1000)

                        # 3. THE MISSING PIECE: Stamp the clock in the database!
                        cursor.execute(
                            """
                            UPDATE user_settings
                            SET last_scraped_at = NOW(),
                                last_scrape_duration_ms = %s
                            WHERE user_id = %s
                            """,
                            (duration_ms, uid)
                        )
                        conn.commit()
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

            time.sleep(60)

        except Exception as e:
            print(f"❌ Error: {e}", flush=True)
            time.sleep(60)

if __name__ == "__main__":
    main()