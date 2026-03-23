import time
import os
import sys
import re
import requests
import psycopg2
from urllib.parse import urlparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

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

    cursor.execute('''
        SELECT user_id, zip_code, search_radius, platforms, 
               ai_enabled, ai_strictness, check_interval_minutes
        FROM user_settings
        WHERE ai_enabled = TRUE
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
    """Get search terms for specific user"""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT search_term, min_price, max_price
        FROM user_search_terms
        WHERE user_id = %s
    ''', (user_id,))

    terms = {}
    for row in cursor.fetchall():
        terms[row[0]] = {'min': float(row[1]), 'max': float(row[2])}

    cursor.close()
    conn.close()

    return terms


def get_user_exclusions(user_id):
    """Get exclusion keywords for specific user"""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT keyword
        FROM user_exclusions
        WHERE user_id = %s
    ''', (user_id,))

    exclusions = [row[0] for row in cursor.fetchall()]

    cursor.close()
    conn.close()

    return exclusions


def get_seen_listings(user_id):
    """Get URLs of listings already seen by user"""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT DISTINCT link
        FROM listings
        WHERE user_id = %s
    ''', (user_id,))

    seen = set(row[0] for row in cursor.fetchall())

    cursor.close()
    conn.close()

    return seen


def save_listing(user_id, listing):
    """Save listing to database"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO listings (user_id, title, price, link, platform, console_type, threshold, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (link) DO NOTHING
        ''', (
            user_id,
            listing['title'],
            listing['price'],
            listing['link'],
            listing['platform'],
            listing.get('console_type'),
            listing.get('threshold')
        ))

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
    """Extract numeric price from text"""
    if not price_text:
        return None
    match = re.search(r'[\$](\d+(?:,\d{3})*(?:\.\d{2})?)', str(price_text))
    if match:
        return float(match.group(1).replace(',', ''))
    return None


def check_price_threshold(title, price, search_terms):
    """Check if price meets user's threshold"""
    title_lower = title.lower()

    for term, thresholds in sorted(search_terms.items(), key=lambda x: len(x[0]), reverse=True):
        if term.lower() in title_lower:
            if thresholds['min'] <= price <= thresholds['max']:
                return True, term, thresholds['max']

    return False, None, None


def is_excluded(title, price, exclusions):
    """Check if listing matches exclusion keywords"""
    title_lower = title.lower()

    # Price too low
    if price < 10:
        return True

    # Check user's exclusions
    for keyword in exclusions:
        if keyword.lower() in title_lower:
            return True

    # Common game patterns
    game_patterns = [
        r'\d+\s*games?',
        r'game\s*(lot|bundle|collection)',
        r'(ds|3ds|gameboy|gba)\s*games',
    ]

    for pattern in game_patterns:
        if re.search(pattern, title_lower):
            return True

    return False


# ===========================
# CRAIGSLIST SCRAPER
# ===========================

def scrape_craigslist_for_user(user_id, zip_code, search_radius, search_terms, exclusions, debug=False):
    """Scrape Craigslist for specific user"""
    listings = []
    subdomain = "stockton"  # TODO: Map zip code to subdomain

    for term in search_terms.keys():
        url = f"https://{subdomain}.craigslist.org/search/vga?query={term.replace(' ', '+')}&sort=date&postal={zip_code}&search_distance={search_radius}"

        try:
            response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            soup = BeautifulSoup(response.content, 'html.parser')
            items = soup.find_all('li', class_='cl-static-search-result')

            if debug:
                print(f"      [{term}] {len(items)} items", flush=True)

            for item in items:
                try:
                    title_elem = item.find('div', class_='title')
                    title = title_elem.text.strip() if title_elem else None
                    if not title:
                        continue

                    link_elem = item.find('a')
                    link = link_elem['href'] if link_elem else None
                    if link and not link.startswith('http'):
                        link = f'https://{subdomain}.craigslist.org' + link

                    price_elem = item.find('div', class_='price')
                    price = extract_price(price_elem.text if price_elem else None)

                    if not price or not link:
                        continue

                    # Check price threshold
                    meets_threshold, console_type, max_price = check_price_threshold(title, price, search_terms)
                    if not meets_threshold:
                        continue

                    # Check exclusions
                    if is_excluded(title, price, exclusions):
                        continue

                    listings.append({
                        'title': title,
                        'price': price,
                        'link': link,
                        'platform': 'Craigslist',
                        'console_type': console_type,
                        'threshold': max_price
                    })

                except Exception as e:
                    continue

            time.sleep(1)  # Rate limiting

        except Exception as e:
            if debug:
                print(f"      Craigslist error: {e}", flush=True)

    return listings


# ===========================
# OFFERUP SCRAPER
# ===========================

def scrape_offerup_for_user(user_id, zip_code, search_radius, search_terms, exclusions, debug=False):
    """Scrape OfferUp for specific user"""
    listings = []
    driver = None

    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        # Chrome options
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])

        # Try system ChromeDriver first (Render), fallback to webdriver-manager (local)
        try:
            # Render deployment
            chrome_options.binary_location = '/usr/bin/chromium'
            service = Service('/usr/bin/chromedriver')
            driver = webdriver.Chrome(service=service, options=chrome_options)
        except:
            # Local development
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)

        for term in search_terms.keys():
            try:
                url = f"https://offerup.com/search/?q={term.replace(' ', '%20')}&radius={search_radius}"

                if debug:
                    print(f"      [{term}] OfferUp...", flush=True)

                driver.get(url)
                time.sleep(5)

                # Scroll to load items
                driver.execute_script("window.scrollTo(0, 800);")
                time.sleep(2)

                # Find items
                items = driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")

                if debug:
                    print(f"        {len(items)} items", flush=True)

                for item in items[:15]:  # Limit to first 15
                    try:
                        link = item.get_attribute('href')
                        title = item.get_attribute('aria-label') or item.text
                        price_text = item.text
                        price = extract_price(price_text)

                        if not price or not link or not title:
                            continue

                        # Check price threshold
                        meets_threshold, console_type, max_price = check_price_threshold(title, price, search_terms)
                        if not meets_threshold:
                            continue

                        # Check exclusions
                        if is_excluded(title, price, exclusions):
                            continue

                        listings.append({
                            'title': title,
                            'price': price,
                            'link': link,
                            'platform': 'OfferUp',
                            'console_type': console_type,
                            'threshold': max_price
                        })

                    except Exception as e:
                        continue

                time.sleep(3)  # Rate limiting

            except Exception as e:
                if debug:
                    print(f"        OfferUp error: {e}", flush=True)

    except Exception as e:
        if debug:
            print(f"      OfferUp driver error: {e}", flush=True)

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

def scrape_for_user(user_config, debug=False):
    """Scrape all enabled platforms for a single user"""
    user_id = user_config['user_id']
    zip_code = user_config['zip_code']

    if debug:
        print(f"  [{user_id}] Scraping (Zip: {zip_code})", flush=True)

    # Get user configuration
    search_terms = get_user_search_terms(user_id)
    if not search_terms:
        print(f"  [{user_id}] No search terms configured", flush=True)
        return 0

    exclusions = get_user_exclusions(user_id)
    seen_listings = get_seen_listings(user_id)

    if debug:
        print(f"      Terms: {len(search_terms)}, Exclusions: {len(exclusions)}, Seen: {len(seen_listings)}",
              flush=True)

    # Scrape enabled platforms
    all_listings = []

    if user_config['platforms'].get('craigslist'):
        if debug:
            print(f"      Craigslist...", flush=True)

        listings = scrape_craigslist_for_user(
            user_id,
            zip_code,
            user_config['search_radius'],
            search_terms,
            exclusions,
            debug
        )
        all_listings.extend(listings)

        # Scrape enabled platforms
        all_listings = []

        if user_config['platforms'].get('craigslist'):
            if debug:
                print(f"      Craigslist...", flush=True)

            listings = scrape_craigslist_for_user(
                user_id,
                zip_code,
                user_config['search_radius'],
                search_terms,
                exclusions,
                debug
            )
            all_listings.extend(listings)

        if user_config['platforms'].get('offerup'):
            if debug:
                print(f"      OfferUp...", flush=True)

            listings = scrape_offerup_for_user(
                user_id,
                zip_code,
                user_config['search_radius'],
                search_terms,
                exclusions,
                debug
            )
            all_listings.extend(listings)

        # TODO: Add Mercari, Facebook scrapers here

    # Filter new listings
    new_listings = []
    for listing in all_listings:
        if listing['link'] not in seen_listings:
            if save_listing(user_id, listing):
                new_listings.append(listing)

    if debug:
        print(f"      ✅ {len(new_listings)} new listings", flush=True)

    # TODO: Send email if new listings found

    return len(new_listings)


# ===========================
# MAIN LOOP
# ===========================

def main():
    """Multi-user scraper main loop"""
    print("=" * 60, flush=True)
    print("PIXELFLIP MULTI-USER SCRAPER", flush=True)
    print("=" * 60, flush=True)

    sys.stdout.flush()

    cycle = 0

    while True:
        try:
            cycle += 1
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"\n[{timestamp}] 🔄 Cycle #{cycle}", flush=True)

            # Get active users
            users = get_active_users()
            print(f"Active users: {len(users)}", flush=True)

            if len(users) == 0:
                print("No active users - waiting 5 minutes...", flush=True)
                sys.stdout.flush()
                time.sleep(300)
                continue

            # Scrape for each user (parallel processing)
            total_new = 0

            # For now, run sequentially (easier debugging)
            # TODO: Use ThreadPoolExecutor for parallel
            for user in users:
                try:
                    new_count = scrape_for_user(user, debug=True)
                    total_new += new_count
                except Exception as e:
                    print(f"  ❌ [{user['user_id']}] Error: {e}", flush=True)

            print(f"\n✅ Total new listings: {total_new}", flush=True)
            print(f"⏳ Next check in 10 minutes...\n", flush=True)

            sys.stdout.flush()
            time.sleep(600)

        except KeyboardInterrupt:
            print("\n🛑 Stopped", flush=True)
            break
        except Exception as e:
            print(f"❌ Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(600)


if __name__ == "__main__":
    main()