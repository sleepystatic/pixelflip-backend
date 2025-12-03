from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
import time
from datetime import datetime
import json
import os
import psycopg2

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

            # TODO: Actually call your scraper functions here
            # For now, simulate with dummy data
            time.sleep(5)  # Simulate scraping time

            # Simulate finding items
            import random
            items_found = random.randint(0, 3)
            scraper_state["items_scanned_today"] += random.randint(10, 50)
            scraper_state["matches_found_today"] += items_found

            if items_found > 0:
                scraper_state["recent_activity"].insert(0, {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": f"Found {items_found} match(es)!",
                    "type": "success"
                })
            else:
                scraper_state["recent_activity"].insert(0, {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "message": "Scan complete. No matches found.",
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
    print("🎮 GameBoy Retreat API Server Starting...")
    print("📍 Running on http://localhost:5000")
    print("🔗 React app should connect to this server")
    app.run(debug=True, port=5000)