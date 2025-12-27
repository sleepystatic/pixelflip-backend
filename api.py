from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
import time
from datetime import datetime
import json
import os
import psycopg2

from scraper import (
    scrape_craigslist,
    scrape_offerup,
    scrape_mercari,
    send_email_alert,
    load_seen_listings,
    save_seen_listings,
    ZIP_CODE
)


DATABASE_URL = os.getenv('DATABASE_URL')

def get_db():
    return psycopg2.connect(DATABASE_URL)

SETTINGS_FILE = "user_settings.json"

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            return json.load(f)
    return None

def save_settings_to_file(settings):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

# Import your scraper functions
# We'll modify scraper.py to make functions importable

app = Flask(__name__)
CORS(app)  # Allow React to communicate with Flask

# Global state
scraper_state = {
    "running": False,
    "status": "stopped",  # stopped, running, error
    "last_check": None,
    "items_scanned_today": 0,
    "matches_found_today": 0,
    "recent_activity": [],
    "settings": {
        "platforms": {
            "craigslist": True,
            "offerup": True,
            "mercari": True
        },
        "zip_code": "95212",
        "distance": 25,
        "check_interval": 10,  # minutes
        "thresholds": {
            "game boy": 30,
            "gameboy": 30,
            "gba": 40,
            "gba sp": 80,
            "nintendo ds": 30,
            "3ds": 110,
            "3ds xl": 150,
            "2ds": 100,
            "2ds xl": 150,
        },
        "ai_detection": True,
        "description_scan": True,
        "strictness": 2  # 1=lenient, 2=medium, 3=strict
    }
}

# Load saved settings if they exist
saved = load_settings()
if saved:
    scraper_state["settings"] = saved

scraper_thread = None


def run_scraper_loop():
    """Background thread that runs the scraper"""
    global scraper_state

    # Load seen listings to avoid duplicates
    seen_listings = load_seen_listings()

    while scraper_state["running"]:
        try:
            scraper_state["status"] = "running"
            scraper_state["last_check"] = datetime.now().strftime("%H:%M:%S")

            # Add to activity log
            scraper_state["recent_activity"].insert(0, {
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": "Starting scan..."
            })

            # Keep only last 50 activities
            scraper_state["recent_activity"] = scraper_state["recent_activity"][:50]

            # ACTUALLY CALL THE SCRAPER!
            all_listings = []

            # Get settings from scraper_state
            platforms = scraper_state["settings"]["platforms"]

            # Scrape enabled platforms
            if platforms.get("craigslist", True):
                scraper_state["recent_activity"].insert(0, {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": "Checking Craigslist..."
                })
                craigslist_listings = scrape_craigslist(ZIP_CODE, debug=False)
                all_listings.extend(craigslist_listings)
                scraper_state["items_scanned_today"] += len(craigslist_listings)

            if platforms.get("offerup", True):
                scraper_state["recent_activity"].insert(0, {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": "Checking OfferUp..."
                })
                offerup_listings = scrape_offerup(debug=False)
                all_listings.extend(offerup_listings)
                scraper_state["items_scanned_today"] += len(offerup_listings)

            if platforms.get("mercari", True):
                scraper_state["recent_activity"].insert(0, {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": "Checking Mercari..."
                })
                mercari_listings = scrape_mercari(debug=False)
                all_listings.extend(mercari_listings)
                scraper_state["items_scanned_today"] += len(mercari_listings)

            # Filter out already seen listings
            new_listings = []
            for listing in all_listings:
                listing_id = f"{listing['platform']}_{listing['link']}"
                if listing_id not in seen_listings:
                    new_listings.append(listing)
                    seen_listings.append(listing_id)

            # Update matches found
            scraper_state["matches_found_today"] += len(new_listings)

            if new_listings:
                scraper_state["recent_activity"].insert(0, {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": f"Found {len(new_listings)} new match(es)!",
                    "type": "success"
                })
                send_email_alert(new_listings)
                save_seen_listings(seen_listings)
            else:
                scraper_state["recent_activity"].insert(0, {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": "Scan complete. No new matches found.",
                    "type": "info"
                })

            # Wait for next check
            wait_seconds = scraper_state["settings"]["check_interval"] * 60
            time.sleep(wait_seconds)

        except Exception as e:
            scraper_state["status"] = "error"
            scraper_state["recent_activity"].insert(0, {
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": f"Error: {str(e)}",
                "type": "error"
            })
            time.sleep(60)  # Wait a minute before retrying


@app.route('/api/status', methods=['GET'])
def get_status():
    """Get current scraper status"""
    return jsonify(scraper_state)


@app.route('/api/start', methods=['POST'])
def start_scraper():
    """Start the scraper"""
    global scraper_state, scraper_thread

    if not scraper_state["running"]:
        scraper_state["running"] = True
        scraper_state["status"] = "running"
        scraper_state["items_scanned_today"] = 0
        scraper_state["matches_found_today"] = 0

        scraper_thread = threading.Thread(target=run_scraper_loop, daemon=True)
        scraper_thread.start()

        scraper_state["recent_activity"].insert(0, {
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": "Scraper started",
            "type": "success"
        })

    return jsonify({"success": True, "status": scraper_state["status"]})


@app.route('/api/stop', methods=['POST'])
def stop_scraper():
    """Stop the scraper"""
    global scraper_state

    scraper_state["running"] = False
    scraper_state["status"] = "stopped"

    scraper_state["recent_activity"].insert(0, {
        "time": datetime.now().strftime("%H:%M:%S"),
        "message": "Scraper stopped",
        "type": "info"
    })

    return jsonify({"success": True, "status": "stopped"})


@app.route('/api/settings', methods=['GET', 'POST'])
def handle_settings():
    """Get or update settings"""
    global scraper_state

    if request.method == 'GET':
        return jsonify(scraper_state["settings"])

    elif request.method == 'POST':
        new_settings = request.json
        scraper_state["settings"].update(new_settings)

        return jsonify({"success": True, "settings": scraper_state["settings"]})


if __name__ == '__main__':
    import os
    port = int(os.getenv('PORT', 5000))

    print("PixelFlip API Server Starting...")
    print(f"Running on port {port}")
    print(f"🌍 Environment: {'Production' if is_production else 'Development'}")


    app.run(
        host='0.0.0.0',
        port=port,
        debug=not is_production
    )