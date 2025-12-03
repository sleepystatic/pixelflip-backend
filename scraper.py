import time
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import re
import os
import platform
import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import undetected_chromedriver as uc
import psycopg2

load_dotenv()

# Configuration
DATABASE_URL = os.getenv('DATABASE_URL')
EMAIL_ADDRESS = os.getenv('EMAIL_ADDRESS')
GOOGLE_VISION_API_KEY = os.getenv('GOOGLE_VISION_API_KEY')
ZIP_CODE = os.getenv('ZIP_CODE', '95212')

PRICE_THRESHOLDS = {
    "game boy": 150,
    "gameboy": 50,
    "game boy color": 40,
    "gameboy color": 40,
    "gbc": 50,
    "game boy advance": 60,
    "gameboy advance": 60,
    "gba": 60,
    "game boy advance sp": 90,
    "gameboy advance sp": 90,
    "gba sp": 90,
    "nintendo ds": 35,
    "ds lite": 35,
    "3ds": 100,
    "3ds xl": 120,
    "new 3ds": 120,
    "new 3ds xl": 120,
    "2ds": 100,
    "2ds xl": 160,
    "nes": 60,
    "snes": 70,
    "super nintendo": 60,
    "n64": 40,
    "nintendo 64": 40,
    "gamecube": 50,
    "game cube": 50,
    "wii": 50,
}

EXCLUSION_KEYWORDS = {
    "general": [
        "shell only",
        "housing only",
        "case only",
        "replacement shell",
        "replacement housing",
        "aftermarket shell",
        "custom shell",
        "no motherboard",
        "no internals",
        "no board",
        "parts only",
        "for parts",
        "broken",
        "not working",
        "as is",
        "junk",
    ],

    "games": [
        "game only"
    ],

    "accessories": [
        "cable only"
    ],

    "game_titles": [
        "mario"
    ]
}

MINIMUM_PRICES = {
    "game boy": 15,
    "gameboy": 15,
    "gba": 15,
    "gba sp": 15,
    "nintendo ds": 15,
    "ds lite": 15,
    "3ds": 30,
    "3ds xl": 30,
    "2ds": 30,
    "2ds xl": 30,
}

SEEN_LISTINGS_FILE = "seen_listings.json"


def save_listing(listing):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute('''
        INSERT INTO listings (title, price, link, platform, created_at)
        VALUES (%s, %s, %s, %s, NOW())
    ''', (listing['title'], listing['price'], listing['link'], listing['platform']))

    conn.commit()
    cursor.close()
    conn.close()
    
    
def load_seen_listings():
    if os.path.exists(SEEN_LISTINGS_FILE):
        with open(SEEN_LISTINGS_FILE, 'r') as f:
            return json.load(f)
    return []


def save_seen_listings(seen_listings):
    with open(SEEN_LISTINGS_FILE, 'w') as f:
        json.dump(seen_listings, f)


def extract_price(price_text):
    if not price_text:
        return None
    price_match = re.search(r'[\$](\d+(?:,\d{3})*(?:\.\d{2})?)', str(price_text))
    if price_match:
        return float(price_match.group(1).replace(',', ''))
    price_match = re.search(r'(\d+(?:,\d{3})*(?:\.\d{2})?)', str(price_text))
    if price_match:
        return float(price_match.group(1).replace(',', ''))
    return None


def check_price_threshold(title, price):
    title_lower = title.lower()
    for console, threshold in sorted(PRICE_THRESHOLDS.items(), key=lambda x: len(x[0]), reverse=True):
        if console in title_lower:
            if price <= threshold:
                return True, console, threshold
            else:
                return False, console, threshold
    return False, None, None


def is_likely_console(title, price, debug=False):
    title_lower = title.lower()

    exclude_keywords = [
        "cartridge", "cart", "game only", "cib", "complete in box",
        "sealed", "new sealed", "pokemon", "mario", "zelda", "metroid",
        "kirby", "donkey kong", "super mario", "legend of", "final fantasy",
        "fire emblem", "animal crossing", "case only", "box only",
        "manual", "instruction", "game case", "lot of games",
        "games only", "no console", "software", "disc only",
        " game ", "games ", " game$"
    ]

    for keyword in exclude_keywords:
        if keyword in title_lower:
            if debug:
                print(f"          Filtered: Contains '{keyword}' (likely a game/accessory)")
            return False

    inclusion_keywords = [
        "console", "system", "handheld", "console only", "no games",
        "sp only", "unit only", "device", "xl console", "bundle with console"
    ]

    for keyword in inclusion_keywords:
        if keyword in title_lower:
            if debug:
                print(f"          Confirmed: Contains '{keyword}' (definitely a console)")
            return True

    if any(x in title_lower for x in ["game boy", "gameboy", "gba", "gbc"]):
        if price < 25 and "sp" not in title_lower:
            if debug:
                print(f"          Filtered: Price ${price} too low for Game Boy console (likely a game)")
            return False

    if any(x in title_lower for x in ["ds", "3ds", "2ds"]):
        if price < 20:
            if debug:
                print(f"          Filtered: Price ${price} too low for DS/3DS console (likely a game)")
            return False

    if debug:
        print(f"          Ambiguous but passed filters - including")
    return True


def is_excluded_listing(title, price, console_type, debug=False):
    """
    Advanced filtering to catch false positives.
    Returns True if listing should be EXCLUDED, False if it's good.
    """
    title_lower = title.lower()

    # Filter out $0 or unrealistic prices
    if price == 0 or price < 5:
        if debug:
            print(f"          ❌ Excluded: Price ${price} is $0 or too low (trade/free)")
        return True

    # Check exclusion keywords
    for category, keywords in EXCLUSION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in title_lower:
                if debug:
                    print(f"          ❌ Excluded: Contains '{keyword}' ({category})")
                return True

    # Check minimum price (catch shells/parts with suspiciously low prices)
    if console_type in MINIMUM_PRICES:
        min_price = MINIMUM_PRICES[console_type]
        if price < min_price:
            if debug:
                print(f"          ❌ Excluded: Price ${price} below minimum ${min_price} for {console_type}")
            return True

    # Additional pattern checks
    suspicious_patterns = [
        r'\bshell\b',  # "shell" as standalone word
        r'\bhousing\b',  # "housing" as standalone word
        r'\bparts?\b',  # "part" or "parts"
        r'\bbroken\b',  # "broken"
        r'\bnot working\b',  # "not working"
        r'\bfor repair\b',  # "for repair"
        r'\bjunk\b',  # "junk"
        r'\breproduction\b',  # "reproduction"
        r'\brepro\b',  # "repro"
        r'\bfake\b',  # "fake"
        r'\br4\b',  # "r4" flash cart
        r'\bflash\s*cart\b',  # "flash cart" or "flashcart"
    ]

    for pattern in suspicious_patterns:
        if re.search(pattern, title_lower):
            matched = re.search(pattern, title_lower).group(0)
            if debug:
                print(f"          ❌ Excluded: Matched pattern '{matched}'")
            return True

    # Passed all filters
    if debug:
        print(f"          ✅ Passed exclusion filters")
    return False

def check_description_for_games(description, debug=False):
    """
    Final check: Scan the listing description for red flags indicating it's just games.
    Returns True if it's likely a console, False if it's just games.
    """
    if not description:
        return True

    desc_lower = description.lower()

    game_only_phrases = [
        "game only", "games only", "no console", "cartridge only", "cart only",
        "2 games", "3 games", "4 games", "5 games", "lot of games",
        "game cartridge", "game case", "just the game", "only the game",
        "ds games", "3ds games", "gameboy games", "gba games",
        "works great", "both work"
    ]

    game_count = 0
    for phrase in game_only_phrases:
        if phrase in desc_lower:
            game_count += 1
            if debug:
                print(f"          Description contains: '{phrase}'")

    if game_count >= 2:
        if debug:
            print(f"          Description scan: {game_count} game-only indicators - filtering out")
        return False

    game_listing_patterns = [
        r'\d+\s*(nintendo\s*)?ds\s*games?',
        r'\d+\s*(game\s*boy|gameboy)\s*games?',
        r'\d+\s*3ds\s*games?',
        r'\d+\s*gba\s*games?',
        r'buy\s*one\s*get',
        r'take\s*both\s*for'
    ]

    for pattern in game_listing_patterns:
        if re.search(pattern, desc_lower):
            if debug:
                print(f"          Description scan: Matched pattern '{pattern}' - filtering out")
            return False

    if debug:
        print(f"          Description scan: Passed")
    return True


def check_image_with_ai(image_url, debug=False):
    if not GOOGLE_VISION_API_KEY:
        if debug:
            print(f"          Google Vision API key not configured - skipping AI check")
        return True

    try:
        url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"

        payload = {
            "requests": [
                {
                    "image": {
                        "source": {
                            "imageUri": image_url
                        }
                    },
                    "features": [
                        {
                            "type": "LABEL_DETECTION",
                            "maxResults": 10
                        },
                        {
                            "type": "OBJECT_LOCALIZATION",
                            "maxResults": 5
                        }
                    ]
                }
            ]
        }

        response = requests.post(url, json=payload, timeout=10)

        if response.status_code == 200:
            result = response.json()

            labels = []
            if 'responses' in result and len(result['responses']) > 0:
                response_data = result['responses'][0]

                if 'labelAnnotations' in response_data:
                    labels = [label['description'].lower() for label in response_data['labelAnnotations']]

                objects = []
                if 'localizedObjectAnnotations' in response_data:
                    objects = [obj['name'].lower() for obj in response_data['localizedObjectAnnotations']]

            all_detected = labels + objects

            if debug:
                print(f"          AI Detected: {', '.join(all_detected[:5])}")

            console_keywords = [
                'game console', 'video game console', 'handheld game console',
                'gaming console', 'portable game console',
                'nintendo ds', 'game boy', 'gameboy', 'playstation portable',
                'psp', 'nintendo switch', 'gaming device'
            ]

            game_keywords = [
                'game cartridge', 'video game cartridge', 'cartridge',
                'game case', 'game packaging', 'cd', 'dvd', 'disc',
                'box', 'packaging', 'video game software', 'game card',
                'game', 'video game'
            ]

            console_score = sum(1 for keyword in console_keywords if any(keyword in item for item in all_detected))
            game_score = sum(1 for keyword in game_keywords if any(keyword in item for item in all_detected))

            has_specific_console = any(keyword in all_detected for keyword in [
                'handheld game console', 'portable game console', 'gaming console',
                'nintendo ds', 'game boy', 'gameboy', 'playstation portable', 'psp',
                'nintendo 3ds', '3ds', 'game boy advance'
            ])

            if game_score > 0 and not has_specific_console:
                if debug:
                    print(f"          AI: Detected game indicators without specific console - filtering out")
                return False
            elif has_specific_console and console_score >= 2 and console_score > game_score:
                if debug:
                    print(
                        f"          AI: Confirmed CONSOLE with specific identifiers (score: {console_score} vs {game_score})")
                return True
            elif game_score > 0:
                if debug:
                    print(
                        f"          AI: Detected GAME/CARTRIDGE keywords (score: {game_score} vs {console_score}) - filtering out")
                return False
            elif has_specific_console and console_score >= 3:
                if debug:
                    print(f"          AI: Strong specific console signals (score: {console_score}) - including")
                return True
            else:
                if debug:
                    print(
                        f"          AI: Insufficient signals (console: {console_score}, game: {game_score}) - filtering out")
                return False
        else:
            if debug:
                print(f"          AI check failed (status {response.status_code}) - defaulting to include")
            return True

    except Exception as e:
        if debug:
            print(f"          AI check error: {e} - defaulting to include")
        return True


def wait_for_captcha_solve(driver, timeout=120):
    """
    Wait for user to manually solve CAPTCHA.
    Checks every 2 seconds if CAPTCHA is gone.
    """
    print("\n" + "=" * 60)
    print("CAPTCHA DETECTED!")
    print("Please solve the CAPTCHA in the browser window.")
    print("The script will automatically continue once solved.")
    print("=" * 60 + "\n")

    start_time = time.time()
    while time.time() - start_time < timeout:
        page_source = driver.page_source.lower()

        if "verify you are human" not in page_source and "captcha" not in page_source:
            print("CAPTCHA solved! Continuing...")
            return True

        time.sleep(2)

    print("Timeout waiting for CAPTCHA solve.")
    return False

def get_listing_description(driver, listing_url, platform, debug=False):
    """
    Navigate to listing page and extract the description.
    Works for both OfferUp and Mercari.
    Returns the description text or None if unable to extract.
    """
    try:
        driver.get(listing_url)
        time.sleep(3)

        description = None

        if platform == "OfferUp":
            possible_selectors = [
                "div[data-testid='description']",
                "div[class*='description']",
                "p[class*='description']",
                "div[class*='Details']",
            ]
        elif platform == "Mercari":
            possible_selectors = [
                "div[data-testid='ItemDescription']",
                "div[class*='item-description']",
                "div[class*='ItemDescription']",
                "p[itemprop='description']",
            ]

        for selector in possible_selectors:
            try:
                desc_elem = driver.find_element(By.CSS_SELECTOR, selector)
                description = desc_elem.text
                if description and len(description) > 10:
                    break
            except:
                continue

        if debug and description:
            print(f"        Description found: {description[:100]}...")
        elif debug:
            print(f"        Could not extract description")

        return description

    except Exception as e:
        if debug:
            print(f"        Error getting description: {e}")
        return None


def create_driver():
    chrome_options = Options()
    chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument(
        '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    chrome_options.add_argument('--disable-blink-features=AutomationControlled')

    try:
        if platform.system() in ['Windows', 'Darwin']:
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
        else:
            try:
                driver = webdriver.Chrome(options=chrome_options)
            except:
                service = Service(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service, options=chrome_options)

        driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            "userAgent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })

        return driver
    except Exception as e:
        print(f"Error creating driver: {e}")
        print("   Make sure Chrome/Chromium is installed!")
        return None


def create_undetected_driver(headless=True):
    """
    Create an undetected ChromeDriver.
    Set headless=False to manually solve CAPTCHAs.
    """
    try:
        options = uc.ChromeOptions()

        # Only run headless if specified
        if headless:
            options.add_argument('--headless=new')

        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')

        driver = uc.Chrome(options=options, version_main=None)
        return driver
    except Exception as e:
        print(f"Error creating undetected driver: {e}")
        return None
def scrape_craigslist(zip_code, debug=False):
    """Scrape Craigslist for gaming consoles (no Selenium needed)"""
    listings = []

    search_terms = ["gameboy", "game boy", "nintendo ds", "3ds", "2ds", "retro console", "nes", "snes", "n64",
                    "gamecube"]

    for term in search_terms:
        url = f"https://stockton.craigslist.org/search/vga?query={term.replace(' ', '+')}&sort=date&postal={zip_code}&search_distance=25"

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.content, 'html.parser')

            items = soup.find_all('li', class_='cl-static-search-result')

            if debug:
                print(f"    [{term}] Found {len(items)} raw items on Craigslist")

            for item in items:
                try:
                    title_elem = item.find('div', class_='title')
                    if not title_elem:
                        title_elem = item.get('title')
                        title = title_elem if title_elem else None
                    else:
                        title = title_elem.text.strip()

                    if not title:
                        continue

                    link_elem = item.find('a')
                    link = link_elem['href'] if link_elem else None

                    if link and not link.startswith('http'):
                        link = 'https://stockton.craigslist.org' + link

                    price_elem = item.find('div', class_='price')
                    price_text = price_elem.text.strip() if price_elem else None
                    price = extract_price(price_text)

                    if debug and len(listings) < 3:
                        print(f"      - {title[:50]}... | Price: {price}")

                    if price and link:
                        meets_threshold, console_type, threshold = check_price_threshold(title, price)
                        if meets_threshold:
                            # Check if it's likely a console (not a game)
                            if not is_likely_console(title, price, debug=debug):
                                continue

                            # NEW: Check exclusion filters
                            if is_excluded_listing(title, price, console_type, debug=debug):
                                continue

                            # Passed all filters!
                            listing_data = {
                                'title': title,
                                'price': price,
                                'link': link,
                                'platform': 'Craigslist',  # or OfferUp/Mercari
                                'console_type': console_type,
                                'threshold': threshold
                            }
                            listings.append(listing_data)

                except Exception as e:
                    if debug:
                        print(f"      Error parsing item: {e}")
                    continue

            time.sleep(2)

        except Exception as e:
            if debug:
                print(f"    Error scraping Craigslist for '{term}': {e}")

    return listings


def scrape_mercari(debug=False):
    """
    Scrape Mercari for gaming consoles using Selenium.
    Mercari is similar to OfferUp - needs JavaScript rendering.
    """
    listings = []
    driver = None

    search_terms = ["gameboy", "nintendo ds", "3ds", "retro console"]

    try:
        driver = create_undetected_driver(headless=False)
        if not driver:
            return listings

        for term in search_terms:
            try:
                # Mercari search URL
                url = f"https://www.mercari.com/search/?keyword={term.replace(' ', '%20')}"

                if debug:
                    print(f"    [{term}] Loading Mercari...")

                driver.get(url)
                time.sleep(7)

                # Check for CAPTCHA and wait for manual solve
                if "verify you are human" in driver.page_source.lower():
                    if not wait_for_captcha_solve(driver):
                        print("Failed to solve CAPTCHA, skipping Mercari")
                        return listings
                    time.sleep(3)

                # Scroll to load more items
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
                time.sleep(2)

                # Mercari uses different selectors - these may need adjustment
                # Mercari items - use the selector we know works
                items = driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")

                if debug:
                    print(f"      Found {len(items)} items")

                if debug:
                    print(f"    [{term}] Found {len(items)} raw items on Mercari")

                parsed_count = 0
                for item in items[:20]:
                    try:
                        link = item.get_attribute('href')

                        # Get title - Mercari structure varies
                        title = item.get_attribute('aria-label') or item.text

                        # Extract price from item text
                        price_text = item.text
                        price = extract_price(price_text)

                        if debug and parsed_count < 3:
                            print(f"      - {title[:50]}... | Price: {price}")
                            parsed_count += 1

                        if price and link and title:
                            meets_threshold, console_type, threshold = check_price_threshold(title, price)
                            meets_threshold, console_type, threshold = check_price_threshold(title, price)
                            if meets_threshold:
                                # Check if it's likely a console (not a game)
                                if not is_likely_console(title, price, debug=debug):
                                    continue

                                # NEW: Check exclusion filters
                                if is_excluded_listing(title, price, console_type, debug=debug):
                                    continue

                                # Passed all filters!
                                listing_data = {
                                    'title': title,
                                    'price': price,
                                    'link': link,
                                    'platform': 'Mercari',  # or OfferUp/Mercari
                                    'console_type': console_type,
                                    'threshold': threshold
                                }
                                listings.append(listing_data)

                    except Exception as e:
                        continue

                time.sleep(3)

            except Exception as e:
                if debug:
                    print(f"    Error scraping Mercari for '{term}': {e}")
                continue

    except Exception as e:
        print(f"Error in Mercari scraper: {e}")

    finally:
        if driver:
            driver.quit()

    return listings


def create_undetected_driver(headless=False):
    """
    Create an undetected ChromeDriver.
    Set headless=False to avoid CAPTCHAs but minimize the window.
    """
    try:
        options = uc.ChromeOptions()

        if headless:
            options.add_argument('--headless=new')
        else:
            # Run visible but minimized
            options.add_argument('--window-size=400,300')  # Small window
            options.add_argument('--window-position=-2000,0')  # Move off-screen

        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')

        driver = uc.Chrome(options=options, version_main=None)
        return driver
    except Exception as e:
        print(f"Error creating undetected driver: {e}")
        return None

def scrape_offerup(debug=False):
    """Scrape OfferUp for gaming consoles using Selenium"""
    listings = []
    driver = None

    search_terms = ["gameboy", "nintendo ds", "3ds", "retro console"]

    try:
        driver = create_driver()
        if not driver:
            return listings

        for term in search_terms:
            try:
                url = f"https://offerup.com/search/?q={term.replace(' ', '%20')}&radius=25"

                if debug:
                    print(f"    [{term}] Loading OfferUp...")

                driver.get(url)
                time.sleep(5)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
                time.sleep(2)

                possible_selectors = [
                    "a[data-testid*='listing']",
                    "div[class*='MuiGrid-root'] a[href*='/item/']",
                    "a[href*='/item/']",
                ]

                items = []
                for selector in possible_selectors:
                    try:
                        items = driver.find_elements(By.CSS_SELECTOR, selector)
                        if items:
                            if debug:
                                print(f"      Found {len(items)} items with selector: {selector}")
                            break
                    except:
                        continue

                if debug:
                    print(f"    [{term}] Found {len(items)} raw items on OfferUp")

                parsed_count = 0
                for item in items[:20]:
                    try:
                        link = item.get_attribute('href')
                        title = item.get_attribute('aria-label') or item.text

                        try:
                            price_elem = item.find_element(By.CSS_SELECTOR, "[class*='price']")
                            price_text = price_elem.text
                        except:
                            price_text = item.text

                        price = extract_price(price_text)

                        if debug and parsed_count < 3:
                            print(f"      - {title[:50]}... | Price: {price}")
                            parsed_count += 1

                        if price and link and title:
                            meets_threshold, console_type, threshold = check_price_threshold(title, price)
                            if meets_threshold:
                                # Check if it's likely a console (not a game)
                                if not is_likely_console(title, price, debug=debug):
                                    continue

                                # NEW: Check exclusion filters
                                if is_excluded_listing(title, price, console_type, debug=debug):
                                    continue

                                # Passed all filters!
                                listing_data = {
                                    'title': title,
                                    'price': price,
                                    'link': link,
                                    'platform': 'Craigslist',  # or OfferUp/Mercari
                                    'console_type': console_type,
                                    'threshold': threshold
                                }
                                listings.append(listing_data)

                    except Exception as e:
                        continue

                time.sleep(3)

            except Exception as e:
                if debug:
                    print(f"    Error scraping OfferUp for '{term}': {e}")
                continue

    except Exception as e:
        print(f"Error in OfferUp scraper: {e}")

    finally:
        if driver:
            driver.quit()

    return listings


def send_email_alert(listings):
    if not listings:
        return

    subject = f"Found {len(listings)} Gaming Console Deal(s)!"

    html_content = """
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h2 style="color: #2c3e50;">New Gaming Console Deals Found!</h2>
        <p>Found {} listing(s) that meet your criteria:</p>
    """.format(len(listings))

    for listing in listings:
        html_content += f"""
        <div style="border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px;">
            <h3 style="color: #27ae60; margin: 0;">{listing['title']}</h3>
            <p style="margin: 5px 0;"><strong>Price:</strong> ${listing['price']:.2f} 
               <span style="color: #e74c3c;">(Threshold: ${listing['threshold']})</span></p>
            <p style="margin: 5px 0;"><strong>Platform:</strong> {listing['platform']}</p>
            <p style="margin: 5px 0;"><strong>Console Type:</strong> {listing['console_type']}</p>
            <a href="{listing['link']}" style="display: inline-block; padding: 10px 20px; 
               background-color: #3498db; color: white; text-decoration: none; 
               border-radius: 5px; margin-top: 10px;">View Listing</a>
        </div>
        """

    html_content += """
        <p style="margin-top: 20px; color: #7f8c8d; font-size: 12px;">
            This alert was generated by your GameBoy Retreat scraper.
        </p>
    </body>
    </html>
    """

    print(f"\n{'=' * 60}")
    print(f"EMAIL ALERT WOULD BE SENT:")
    print(f"To: {EMAIL_ADDRESS}")
    print(f"Subject: {subject}")
    print(f"Listings found: {len(listings)}")
    for listing in listings:
        print(f"  - {listing['title']} - ${listing['price']} on {listing['platform']}")
        print(f"    {listing['link']}")
    print(f"{'=' * 60}\n")


def main():
    print(f"GameBoy Retreat Unified Scraper Started!")
    print(f"Location: {ZIP_CODE}")
    print(f"Alert Email: {EMAIL_ADDRESS}")
    print(f"Checking every 10 minutes...")
    print(f"Using Selenium for OfferUp and Mercari")
    print(f"Using simple requests for Craigslist")
    print(f"AI Image Detection: {'Enabled' if GOOGLE_VISION_API_KEY else 'Disabled'}")
    print(f"Description Scanning: Enabled")
    print(f"\nDEBUG MODE: Enabled for first run\n")
    print(f"{'=' * 60}\n")

    seen_listings = load_seen_listings()
    debug_mode = True

    while True:
        try:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking listings...")

            all_listings = []

            # Scrape Craigslist
            print("  Checking Craigslist...")
            craigslist_listings = scrape_craigslist(ZIP_CODE, debug=debug_mode)
            all_listings.extend(craigslist_listings)
            print(f"    Found {len(craigslist_listings)} matching listings")

            # Scrape OfferUp
            print("  Checking OfferUp...")
            offerup_listings = scrape_offerup(debug=debug_mode)
            all_listings.extend(offerup_listings)
            print(f"    Found {len(offerup_listings)} matching listings")

            # Scrape Mercari
            print("  Checking Mercari...")
            mercari_listings = scrape_mercari(debug=debug_mode)
            all_listings.extend(mercari_listings)
            print(f"    Found {len(mercari_listings)} matching listings")

            if debug_mode:
                debug_mode = False
                print(f"\n  Debug mode OFF for subsequent checks\n")

            new_listings = []
            for listing in all_listings:
                listing_id = f"{listing['platform']}_{listing['link']}"
                if listing_id not in seen_listings:
                    new_listings.append(listing)
                    seen_listings.append(listing_id)

            if new_listings:
                print(f"\n  Found {len(new_listings)} NEW listing(s)!")
                send_email_alert(new_listings)
                save_seen_listings(seen_listings)
            else:
                print(f"\n  No new listings found that meet thresholds.")

            print(f"  Waiting 10 minutes until next check...\n")
            time.sleep(600)

        except KeyboardInterrupt:
            print("\n\nScraper stopped by user.")
            break
        except Exception as e:
            print(f"  Error in main loop: {e}")
            print(f"  Waiting 10 minutes before retry...\n")
            time.sleep(600)


def wait_for_captcha_solve(driver, timeout=120):
    """
    Wait for user to manually solve CAPTCHA.
    Checks every 2 seconds if CAPTCHA is gone.
    """
    print("\n" + "=" * 60)
    print("CAPTCHA DETECTED!")
    print("Please solve the CAPTCHA in the browser window.")
    print("The script will automatically continue once solved.")
    print("=" * 60 + "\n")

    start_time = time.time()
    while time.time() - start_time < timeout:
        # Check if CAPTCHA is still present
        page_source = driver.page_source.lower()

        if "verify you are human" not in page_source and "captcha" not in page_source:
            print("CAPTCHA solved! Continuing...")
            return True

        time.sleep(2)

    print("Timeout waiting for CAPTCHA solve.")
    return False


def diagnose_mercari():
    """Test Mercari with undetected chromedriver"""
    driver = create_undetected_driver()
    if not driver:
        return

    try:
        url = "https://www.mercari.com/search/?keyword=nintendo"
        print(f"Loading: {url}")
        driver.get(url)

        # Wait much longer for JavaScript to execute
        print("Waiting 10 seconds for page to fully load...")
        time.sleep(10)

        # Scroll to trigger lazy loading
        print("Scrolling page...")
        driver.execute_script("window.scrollTo(0, 500);")
        time.sleep(3)
        driver.execute_script("window.scrollTo(0, 1000);")
        time.sleep(3)

        # Save the HTML after scrolling
        with open('mercari_after_scroll.html', 'w', encoding='utf-8') as f:
            f.write(driver.page_source)
        print("Saved to mercari_after_scroll.html")

        # Count total elements
        all_elements = driver.find_elements(By.CSS_SELECTOR, "*")
        print(f"\nTotal HTML elements on page: {len(all_elements)}")

        # Look for spans with prices (items always have prices)
        price_elements = driver.find_elements(By.CSS_SELECTOR, "span")
        prices_found = []
        for span in price_elements:
            text = span.text
            if text and '$' in text:
                prices_found.append(text)

        print(f"Found {len(prices_found)} elements with $ signs")
        if prices_found:
            print(f"Sample prices: {prices_found[:5]}")

        # Try to find ANY links
        all_links = driver.find_elements(By.TAG_NAME, "a")
        print(f"\nTotal <a> tags: {len(all_links)}")

        # Show first 10 links
        print("First 10 links:")
        for i, link in enumerate(all_links[:10]):
            href = link.get_attribute('href')
            text = link.text[:40] if link.text else "[no text]"
            print(f"  {i + 1}. {text} -> {href}")

    finally:
        try:
            driver.quit()
        except:
            pass


def test_mercari_only():
    """Test just Mercari with one search term"""
    print("Testing Mercari scraper...")
    print("A browser window will open - solve the CAPTCHA when it appears\n")

    driver = None
    try:
        driver = create_undetected_driver(headless=False)
        if not driver:
            print("Failed to create driver")
            return

        # Test with just one search term
        term = "nintendo"
        url = f"https://www.mercari.com/search/?keyword={term}"

        print(f"Loading: {url}")
        driver.get(url)
        time.sleep(5)

        # Check for CAPTCHA
        if "verify you are human" in driver.page_source.lower():
            print("\nCAPTCHA detected! Solve it in the browser window...")
            if not wait_for_captcha_solve(driver):
                print("Failed to solve CAPTCHA")
                return
        else:
            print("No CAPTCHA detected!")

        # Scroll and wait
        print("Scrolling page...")
        driver.execute_script("window.scrollTo(0, 500);")
        time.sleep(3)

        # Try to find items
        print("\nLooking for items...")

        # Try multiple selectors
        selectors = [
            "a[href*='/item/']",
            "a[href*='/us/item/']",
        ]

        for selector in selectors:
            items = driver.find_elements(By.CSS_SELECTOR, selector)
            if items:
                print(f"Found {len(items)} items with selector: {selector}")
                print(f"First item: {items[0].get_attribute('href')}")
                break
        else:
            print("No items found with any selector")

            # Debug: show what's on the page
            all_links = driver.find_elements(By.TAG_NAME, "a")
            print(f"\nTotal links on page: {len(all_links)}")
            if all_links:
                print("First 5 links:")
                for i, link in enumerate(all_links[:5]):
                    print(f"  {link.get_attribute('href')}")

        print("\nKeeping browser open for 30 seconds so you can inspect...")
        time.sleep(30)

    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass


if __name__ == "__main__":
    main()