from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
import time
import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from functools import wraps
import requests
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()


app = Flask(__name__)

# Wildcard origin + supports_credentials=True is invalid per CORS; browsers drop Allow-Origin on preflight.
# List explicit origins (comma-separated in CORS_ORIGINS on Render) or default to Vercel + local dev.
_default_origins = (
    "https://pixelflipdashboard.vercel.app,"
    "http://localhost:3000,http://127.0.0.1:3000"
)
_cors_origins = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", _default_origins).split(",")
    if o.strip()
]
CORS(
    app,
    resources={r"/*": {"origins": _cors_origins}},
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    supports_credentials=False,
)

# ==========================================
# DATABASE & AUTH SETUP
# ==========================================
DATABASE_URL = os.getenv('DATABASE_URL')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_ANON_KEY = os.getenv('SUPABASE_ANON_KEY')

def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"Database connection error: {e}", flush=True)
        return None



def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Allow browser preflight checks to pass without a token
        if request.method == 'OPTIONS':
            return '', 200

        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"error": "Missing token"}), 401

        token = auth_header.split(" ")[1]
        try:
            # Ask Supabase directly if the token is valid
            verify_url = f"{SUPABASE_URL}/auth/v1/user"
            response = requests.get(
                verify_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": SUPABASE_ANON_KEY
                }
            )

            if response.status_code != 200:
                print(f"🔒 Supabase Auth Rejected: {response.text}", flush=True)
                return jsonify({"error": "Invalid or expired token"}), 401

            user_data = response.json()
            user_id = user_data.get('id')

            if not user_id:
                return jsonify({"error": "User ID not found in token"}), 401

        except Exception as e:
            print(f"🔒 Auth Server Error: {str(e)}", flush=True)
            return jsonify({"error": f"Server auth error: {str(e)}"}), 500

        return f(user_id, *args, **kwargs)

    return decorated


from datetime import datetime

# ==========================================
# IN-MEMORY LOG BUFFER (User-Specific)
# ==========================================
# Looks like: { "user_id_123": [{"time": "10:00:00 AM", "message": "Scraping...", "type": "info"}] }
user_logs = {}


def add_log(user_id, message, log_type="info"):
    """Saves a log to the specific user's buffer to be sent to React"""
    if user_id not in user_logs:
        user_logs[user_id] = []

    timestamp = datetime.now().strftime("%I:%M:%S %p")
    user_logs[user_id].append({"time": timestamp, "message": message, "type": log_type})

    # Keep only the last 50 logs so we don't run out of server memory
    if len(user_logs[user_id]) > 50:
        user_logs[user_id].pop(0)


# ==========================================
# BACKGROUND SCRAPER STATUS
# ==========================================
scraper_status = {
    'running': False,
    'error': None,
}


def start_background_scraper():
    """Run multi-user scraper"""
    global scraper_status
    try:
        print("🚀 Starting multi-user scraper...", flush=True)
        from scraper_multi_user import main as run_scraper
        scraper_status['running'] = True

        # We pass our log function directly into the scraper!
        run_scraper(log_callback=add_log)

    except Exception as e:
        scraper_status['running'] = False
        scraper_status['error'] = str(e)
        print(f"❌ Scraper error: {e}", flush=True)


# ==========================================
# API ENDPOINTS
# ==========================================
@app.route('/')
def health_check():
    return jsonify({"status": "running", "scraper_active": scraper_status['running']})


@app.route('/api/status', methods=['GET', 'OPTIONS'])
@require_auth
def get_status(user_id):
    if request.method == 'OPTIONS':
        return '', 200

    conn = get_db_connection()
    if not conn: return jsonify({"error": "DB error"}), 500

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        # Pull the time they were last scraped
        cursor.execute(
            "SELECT is_active, check_interval_minutes, EXTRACT(EPOCH FROM last_scraped_at) as last_scraped_ts FROM user_settings WHERE user_id = %s;",
            (user_id,))
        us = cursor.fetchone()

        # Ensure it is a whole number (strips decimals)
        is_running = us['is_active'] if us else False
        interval_secs = int(us['check_interval_minutes'] if us else 10) * 60

        # If they've never been scraped, default to 0 so the timer hits 0:00 immediately
        last_scraped = int(us['last_scraped_ts']) if us and us['last_scraped_ts'] else 0

        next_check_timestamp = last_scraped + interval_secs

        return jsonify({
            "status": "running" if is_running else "stopped",
            "running": is_running,
            "items_scanned_today": 0,
            "matches_found_today": 0,
            "next_check_timestamp": next_check_timestamp,  # SEND TO REACT
            "recent_activity": user_logs.get(user_id, [])
        })
    finally:
        cursor.close()
        conn.close()


@app.route('/api/settings', methods=['GET', 'POST', 'OPTIONS'])
@require_auth
def handle_settings(user_id):
    """Fetch or update settings directly from Supabase PostgreSQL"""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500

    cursor = conn.cursor(cursor_factory=RealDictCursor)

    try:
        if request.method == 'GET':
            cursor.execute("SELECT * FROM user_settings WHERE user_id = %s;", (user_id,))
            us = cursor.fetchone()

            # THE FIX: Added min_price to the SELECT statement and added 'None' safety nets
            cursor.execute("SELECT search_term, max_price, min_price FROM user_search_terms WHERE user_id = %s;",
                           (user_id,))
            terms = {
                row['search_term']: {
                    'max': float(row['max_price'] if row['max_price'] is not None else 0),
                    'min': float(row['min_price'] if row['min_price'] is not None else 0)
                } for row in cursor.fetchall()
            }

            cursor.execute("SELECT keyword FROM user_exclusions WHERE user_id = %s;", (user_id,))
            exclusions = [row['keyword'] for row in cursor.fetchall()]

            if not us:
                return jsonify({
                    "platforms": {"craigslist": True, "offerup": True, "mercari": True},
                    "zip_code": "95212",
                    "distance": 25,
                    "check_interval": 10,
                    "thresholds": terms,
                    "excluded_keywords": exclusions,
                    "ai_detection": True,
                    "strictness": 3,
                    "subscription_status": "inactive"
                })

            strict_map = {'lenient': 1, 'balanced': 2, 'strict': 3}
            return jsonify({
                "platforms": us['platforms'] if us['platforms'] else {"craigslist": True, "offerup": True,
                                                                      "mercari": True},
                "zip_code": us['zip_code'],
                "distance": us['search_radius'],
                "check_interval": us['check_interval_minutes'],
                "thresholds": terms,
                "excluded_keywords": exclusions,
                "ai_detection": us['ai_enabled'],
                "strictness": strict_map.get(us['ai_strictness'], 2),
                "subscription_status": us.get('subscription_status', 'inactive') if us else 'inactive'
            })

        elif request.method == 'POST':
            data = request.json
            strict_map = {1: 'lenient', 2: 'balanced', 3: 'strict'}
            strict_text = strict_map.get(data.get('strictness', 2), 'balanced')

            # UPSERT Core Settings
            cursor.execute("""
                INSERT INTO user_settings (user_id, zip_code, search_radius, platforms, ai_enabled, check_interval_minutes, ai_strictness)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    zip_code = EXCLUDED.zip_code, search_radius = EXCLUDED.search_radius, platforms = EXCLUDED.platforms,
                    ai_enabled = EXCLUDED.ai_enabled, check_interval_minutes = EXCLUDED.check_interval_minutes, ai_strictness = EXCLUDED.ai_strictness;
            """, (
            user_id, data.get('zip_code', '95212'), data.get('distance', 25), json.dumps(data.get('platforms', {})),
            data.get('ai_detection', True), data.get('check_interval', 10), strict_text))

            # REPLACE Search Terms
            # POST: Save both max and min to the database
            cursor.execute("DELETE FROM user_search_terms WHERE user_id = %s;", (user_id,))
            for term, prices in data.get('thresholds', {}).items():
                cursor.execute(
                    "INSERT INTO user_search_terms (user_id, search_term, max_price, min_price) VALUES (%s, %s, %s, %s);",
                    (user_id, term, prices.get('max', 0), prices.get('min', 0))
                )

            # REPLACE Exclusions
            cursor.execute("DELETE FROM user_exclusions WHERE user_id = %s;", (user_id,))
            for keyword in data.get('excluded_keywords', []):
                cursor.execute("INSERT INTO user_exclusions (user_id, keyword) VALUES (%s, %s);", (user_id, keyword))

            conn.commit()
            return jsonify({"success": True, "settings": data})

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/start', methods=['POST', 'OPTIONS'])
@require_auth
def start_scraper(user_id):
    """Enable the scraper for this specific user"""
    if request.method == 'OPTIONS':
        return '', 200

    conn = get_db_connection()
    if not conn: return jsonify({"error": "DB error"}), 500
    try:
        cursor = conn.cursor()
        # WE NOW FLIP THE CORRECT SWITCH
        cursor.execute("UPDATE user_settings SET is_active = TRUE WHERE user_id = %s;", (user_id,))
        conn.commit()
        return jsonify({"success": True, "status": "running"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/stop', methods=['POST', 'OPTIONS'])
@require_auth
def stop_scraper(user_id):
    """Disable the scraper for this specific user"""
    if request.method == 'OPTIONS':
        return '', 200

    conn = get_db_connection()
    if not conn: return jsonify({"error": "DB error"}), 500
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE user_settings SET is_active = FALSE WHERE user_id = %s;", (user_id,))
        conn.commit()
        return jsonify({"success": True, "status": "stopped"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


if __name__ == '__main__':
    # Start scraper thread
    scraper_thread = threading.Thread(target=start_background_scraper, daemon=True)
    scraper_thread.start()

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)