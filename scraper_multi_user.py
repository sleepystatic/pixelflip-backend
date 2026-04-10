import time
import os
import sys
import re
import hashlib
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

print("✅ Multi-user scraper loaded", flush=True)


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
    cursor = conn.cursor()

    # CRITICAL FIX: Only grab users who clicked "Start Scanner" (is_active = TRUE)
    cursor.execute('''
        SELECT user_id, zip_code, search_radius, platforms, 
               ai_enabled, ai_strictness, check_interval_minutes
        FROM user_settings
        WHERE is_active = TRUE
    ''')

    users = []
    for row in cursor.fetchall():
        users.append({
            'user_id': row[0],
            'zip_code': row[1] or '95212',
            'search_radius': row[2] or 25,
            'platforms': row[3] or {'craigslist': True},
            'ai_enabled': row[4],
            'ai_strictness': row[5] or 'balanced',
            'check_interval': row[6] or 10
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
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
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


def check_price_threshold(title, price, search_terms):
    title_lower = title.lower()
    for term, thresholds in sorted(search_terms.items(), key=lambda x: len(x[0]), reverse=True):
        if term.lower() in title_lower:
            if thresholds['min'] <= price <= thresholds['max']:
                return True, term, thresholds['max']
    return False, None, None


def is_excluded(title, price, exclusions):
    title_lower = title.lower()
    if price < 10: return True
    for keyword in exclusions:
        if keyword.lower() in title_lower: return True
    game_patterns = [r'\d+\s*games?', r'game\s*(lot|bundle|collection)', r'(ds|3ds|gameboy|gba)\s*games']
    for pattern in game_patterns:
        if re.search(pattern, title_lower): return True
    return False


# ===========================
# AI IMAGE DETECTION
# ===========================
GOOGLE_VISION_API_KEY = os.getenv('GOOGLE_VISION_API_KEY')


def check_image_with_ai(image_url, ai_enabled, ai_strictness, debug=False, log_callback=None, user_id=None):
    if not ai_enabled or not GOOGLE_VISION_API_KEY or not image_url: return True

    try:
        url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"
        payload = {"requests": [{"image": {"source": {"imageUri": image_url}},
                                 "features": [{"type": "LABEL_DETECTION", "maxResults": 10},
                                              {"type": "OBJECT_LOCALIZATION", "maxResults": 5}]}]}
        response = requests.post(url, json=payload, timeout=10)

        if response.status_code != 200: return True

        result = response.json()
        if 'responses' not in result or not result['responses']: return True

        data = result['responses'][0]
        labels = [label['description'].lower() for label in data.get('labelAnnotations', [])]
        objects = [obj['name'].lower() for obj in data.get('localizedObjectAnnotations', [])]
        all_detected = labels + objects

        console_keywords = ['handheld game console', 'game console', 'gaming console', 'portable game console',
                            'video game console', 'electronics']
        non_console_keywords = ['game cartridge', 'cartridge', 'game case', 'cd', 'dvd', 'disc', 'box', 'packaging',
                                'charger', 'cable', 'paper', 'book', 'document']

        has_console = any(kw in all_detected for kw in console_keywords)
        has_non_console = any(kw in all_detected for kw in non_console_keywords)

        if ai_strictness == 'strict':
            passed = has_console and not has_non_console
        elif ai_strictness == 'balanced':
            passed = True if not (has_non_console and not has_console) else False
        else:
            passed = not (has_non_console and not has_console)

        # Log AI rejections to the dashboard
        if not passed and log_callback and user_id:
            log_callback(user_id, f"AI Filtered Image: Found {all_detected[0]}", "error")

        return passed
    except:
        return True


# ===========================
# CRAIGSLIST SCRAPER
# ===========================
def scrape_craigslist_for_user(user_id, zip_code, search_radius, search_terms, exclusions, ai_enabled, ai_strictness,
                               debug=False, log_callback=None):
    listings = []
    subdomain = "stockton"

    if log_callback: log_callback(user_id, "Waking up Craigslist scraper...", "info")

    for term in search_terms.keys():
        url = f"https://{subdomain}.craigslist.org/search/vga?query={term.replace(' ', '+')}&sort=date&postal={zip_code}&search_distance={search_radius}"
        try:
            response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            soup = BeautifulSoup(response.content, 'html.parser')
            items = soup.find_all('li', class_='cl-static-search-result')

            for item in items:
                try:
                    title_elem = item.find('div', class_='title')
                    title = title_elem.text.strip() if title_elem else None
                    if not title: continue

                    link_elem = item.find('a')
                    link = link_elem['href'] if link_elem else None
                    if link and not link.startswith('http'): link = f'https://{subdomain}.craigslist.org' + link

                    price_elem = item.find('div', class_='price')
                    price = extract_price(price_elem.text if price_elem else None)
                    if not price or not link: continue

                    meets_threshold, console_type, max_price = check_price_threshold(title, price, search_terms)
                    if not meets_threshold: continue
                    if is_excluded(title, price, exclusions): continue

                    image_url = None
                    try:
                        img_elem = item.find('img')
                        if img_elem and 'src' in img_elem.attrs: image_url = img_elem['src']
                    except:
                        pass

                    if not check_image_with_ai(image_url, ai_enabled, ai_strictness, debug, log_callback,
                                               user_id): continue

                    listings.append({
                        'title': title, 'price': price, 'link': link, 'platform': 'Craigslist',
                        'console_type': console_type, 'threshold': max_price,
                        'image_url': image_url,
                        'location': f'Craigslist · {zip_code} ({search_radius} mi)'
                    })
                except:
                    continue
            time.sleep(1)
        except:
            pass
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

        # FIND BINARY
        chrome_bin = next((p for p in ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser'] if
                           os.path.exists(p)), None)
        if chrome_bin:
            chrome_options.binary_location = chrome_bin

        # INITIALIZE ONCE
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)

        # STEALTH HANDSHAKE
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })

        for term in search_terms.keys():
            try:
                url = f"https://offerup.com/search/?q={term.replace(' ', '%20')}&radius={search_radius}"
                if log_callback: log_callback(user_id, f"Scanning OfferUp for '{term}'...", "info")

                driver.get(url)
                time.sleep(5)

                for scroll in range(3):
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(2)

                items = driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")

                for item in items[:50]:
                    try:
                        link = item.get_attribute('href')
                        title = item.get_attribute('aria-label') or item.text
                        price = extract_price(item.text)

                        if not price or not link or not title: continue
                        meets_threshold, console_type, max_price = check_price_threshold(title, price, search_terms)
                        if not meets_threshold: continue
                        if is_excluded(title, price, exclusions): continue

                        image_url = None
                        try:
                            img_elem = item.find_element(By.TAG_NAME, 'img')
                            image_url = img_elem.get_attribute('src')
                        except:
                            pass

                        if not check_image_with_ai(image_url, ai_enabled, ai_strictness, debug, log_callback,
                                                   user_id): continue

                        listings.append({
                            'title': title, 'price': price, 'link': link, 'platform': 'OfferUp',
                            'console_type': console_type, 'threshold': max_price,
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
            log_callback(
                user_id,
                f"OfferUp robot failed to start: {e.__class__.__name__}: {e}",
                "error"
            )
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
    return listings


# ===========================
# USER SCRAPER
# ===========================
def scrape_for_user(user_config, log_callback=None, debug=False):
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

    new_listings = []
    cycle_fp_to_prices = {}
    for listing in all_listings:
        link = listing['link']
        fp = _title_fingerprint(listing.get('title'))
        listing['title_fingerprint'] = fp
        price = float(listing.get('price') or 0)

        if link in seen_listings or link in blocked_links:
            continue
        if fp and fp in blocked_fingerprints:
            continue

        # Fuzzy-ish duplicate gate across marketplaces:
        # same normalized title fingerprint and close price (+/- $10)
        recent_prices = recent_fp_to_prices.get(fp, [])
        cycle_prices = cycle_fp_to_prices.get(fp, [])
        if fp and any(abs(price - p) <= 10 for p in recent_prices + cycle_prices):
            continue

        if save_listing(user_id, listing):
            new_listings.append(listing)
            cycle_fp_to_prices.setdefault(fp, []).append(price)
            # Send the success log straight to the UI!
            if log_callback:
                log_callback(
                    user_id,
                    f"DEAL FOUND: {listing['title'][:30]} for ${listing['price']}",
                    "success"
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


if __name__ == "__main__":
    main()