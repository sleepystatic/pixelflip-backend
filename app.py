from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
import time
import os
import jwt
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from functools import wraps


app = Flask(__name__)
CORS(app)

# ==========================================
# DATABASE & AUTH SETUP
# ==========================================
DATABASE_URL = os.getenv('DATABASE_URL')
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "your-super-secret-jwt-key")


def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"Database connection error: {e}", flush=True)
        return None


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # NEW: Allow browser preflight checks to pass without a token
        if request.method == 'OPTIONS':
            return '', 200

        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"error": "Missing or invalid token"}), 401

        token = auth_header.split(" ")[1]
        try:
            decoded = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
            user_id = decoded['sub']
        except Exception as e:
            return jsonify({"error": str(e)}), 401

        return f(user_id, *args, **kwargs)

    return decorated


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
        run_scraper()
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


@app.route('/api/status', methods=['GET'])
@require_auth
def get_status(user_id):
    # In the future, we will query the DB for this specific user's stats
    return jsonify({
        "status": "running" if scraper_status['running'] else "stopped",
        "running": scraper_status['running'],
        "items_scanned_today": 0,
        "matches_found_today": 0,
        "recent_activity": []
    })


@app.route('/api/settings', methods=['GET', 'POST'])
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

            cursor.execute("SELECT search_term, max_price FROM user_search_terms WHERE user_id = %s;", (user_id,))
            terms = {row['search_term']: float(row['max_price']) for row in cursor.fetchall()}

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
                    "strictness": 2
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
            cursor.execute("DELETE FROM user_search_terms WHERE user_id = %s;", (user_id,))
            for term, max_price in data.get('thresholds', {}).items():
                cursor.execute(
                    "INSERT INTO user_search_terms (user_id, search_term, max_price, min_price) VALUES (%s, %s, %s, %s);",
                    (user_id, term, max_price, 10))

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


if __name__ == '__main__':
    # Start scraper thread
    scraper_thread = threading.Thread(target=start_background_scraper, daemon=True)
    scraper_thread.start()

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)