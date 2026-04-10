from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
import time
import os
import shutil
import json
import secrets
import hashlib
import psycopg2
from psycopg2.extras import RealDictCursor
from functools import wraps
import requests
from datetime import datetime, timedelta
import stripe

from dotenv import load_dotenv
load_dotenv()

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

app = Flask(__name__)


def _frontend_base_url():
    return (os.getenv('FRONTEND_URL') or 'http://localhost:3000').rstrip('/')


def _send_email_change_code_email(to_email, code):
    mailgun_api_key = os.getenv('MAILGUN_API_KEY')
    mailgun_domain = os.getenv('MAILGUN_DOMAIN')
    mailgun_from = os.getenv('MAILGUN_FROM_EMAIL')
    mailgun_base_url = os.getenv('MAILGUN_BASE_URL', 'https://api.mailgun.net')

    if not all([mailgun_api_key, mailgun_domain, mailgun_from]):
        raise ValueError("Mailgun settings missing (MAILGUN_API_KEY/MAILGUN_DOMAIN/MAILGUN_FROM_EMAIL)")

    endpoint = f"{mailgun_base_url.rstrip('/')}/v3/{mailgun_domain}/messages"
    subject = "PixelFlip email change verification code"
    text_body = (
        f"Your PixelFlip verification code is: {code}\n\n"
        "This code expires in 10 minutes. If you did not request this, ignore this email."
    )

    resp = requests.post(
        endpoint,
        auth=("api", mailgun_api_key),
        data={
            "from": mailgun_from,
            "to": [to_email],
            "subject": subject,
            "text": text_body,
        },
        timeout=20,
    )
    if resp.status_code >= 300:
        try:
            payload = resp.json()
            message = payload.get("message") or payload.get("error") or resp.text
        except Exception:
            message = resp.text
        raise ValueError(f"Mailgun send failed ({resp.status_code}): {message}")


def _ensure_email_change_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS email_change_requests (
          user_id TEXT PRIMARY KEY,
          new_email TEXT NOT NULL,
          code_hash TEXT NOT NULL,
          expires_at TIMESTAMPTZ NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _epoch_from_last_scraped(value):
    """Normalize last_scraped_at / timestamp to unix seconds for countdown."""
    if value is None:
        return 0
    if isinstance(value, datetime):
        return int(value.timestamp())
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _subscription_is_entitled(status):
    return status in ('active', 'trialing')


def _apply_subscription_row(cursor, user_id, is_pro, period_end=None, cancel_at_end=None, customer_id=None, sub_id=None):
    """Keep is_pro, subscription_status, and Stripe ids in sync."""
    sets = ["is_pro = %s", "subscription_status = %s"]
    vals = [is_pro, 'active' if is_pro else 'inactive']
    if customer_id:
        sets.append("stripe_customer_id = %s")
        vals.append(customer_id)
    if sub_id:
        sets.append("stripe_subscription_id = %s")
        vals.append(sub_id)
    if period_end is not None:
        sets.append("subscription_current_period_end = %s")
        vals.append(period_end)
    if cancel_at_end is not None:
        sets.append("subscription_cancel_at_period_end = %s")
        vals.append(cancel_at_end)
    vals.append(user_id)
    cursor.execute(f"UPDATE user_settings SET {', '.join(sets)} WHERE user_id = %s", vals)


def _upsert_pro_subscription(cursor, user_id, customer_id, sub_id, period_end, cancel_at_end):
    """
    Set Pro + active subscription after Checkout or webhook.
    Inserts a minimal user_settings row if none exists (common for brand-new accounts).
    """
    platforms = json.dumps({"craigslist": True, "offerup": True, "mercari": True})
    cursor.execute(
        """
        INSERT INTO user_settings (
            user_id, zip_code, search_radius, platforms, ai_enabled,
            check_interval_minutes, ai_strictness,
            is_pro, subscription_status,
            stripe_customer_id, stripe_subscription_id,
            subscription_current_period_end, subscription_cancel_at_period_end
        )
        VALUES (
            %s, '95212', 25, %s::jsonb, TRUE, 10, 'balanced',
            TRUE, 'active', %s, %s, %s, %s
        )
        ON CONFLICT (user_id) DO UPDATE SET
            is_pro = TRUE,
            subscription_status = 'active',
            stripe_customer_id = EXCLUDED.stripe_customer_id,
            stripe_subscription_id = EXCLUDED.stripe_subscription_id,
            subscription_current_period_end = EXCLUDED.subscription_current_period_end,
            subscription_cancel_at_period_end = EXCLUDED.subscription_cancel_at_period_end
        """,
        (user_id, platforms, customer_id, sub_id, period_end, cancel_at_end),
    )

# Wildcard origin + supports_credentials=True is invalid per CORS; browsers drop Allow-Origin on preflight.
# List explicit origins (comma-separated in CORS_ORIGINS on Render) or default to Vercel + local dev.
_default_origins = (
    "https://pixelflipdashboard.vercel.app,"
    "http://localhost:3000,http://127.0.0.1:3000,"
    "http://localhost:3001,http://127.0.0.1:3001,"
    "http://localhost:3002,http://127.0.0.1:3002"
)
_cors_origins = [
    "https://pixelflip.app",           # Landing Page
    "https://dashboard.pixelflip.app", # The New Dashboard Subdomain
    "https://api.pixelflip.app",       # The API itself
    "http://localhost:3000",           # Local Dev (React default)
    "http://localhost:3001",           # Local Dev (Your current port)
    "http://127.0.0.1:3001"            # Local Dev (Alternative)
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
SUPABASE_SERVICE_ROLE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY')

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


def cleanup_old_listings():
    """Delete old listings on a loop to keep DB size bounded."""
    while True:
        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            if conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM listings WHERE created_at < NOW() - INTERVAL '7 days'")
                deleted = cursor.rowcount
                conn.commit()
                if deleted > 0:
                    print(f"🧹 Cleanup removed {deleted} old listings", flush=True)
        except Exception as e:
            print(f"cleanup_old_listings error: {e}", flush=True)
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        # Run every 6 hours
        time.sleep(6 * 60 * 60)


# ==========================================
# BACKGROUND SCRAPER STATUS
# ==========================================
scraper_status = {
    'running': False,
    'error': None,
}


def start_background_scraper():
    global scraper_status
    try:
        print("🚀 Starting multi-user scraper...", flush=True)
        from scraper_multi_user import main as run_scraper
        scraper_status['running'] = True

        # THE BRIDGE: This specific wrapper ensures the logs
        # go into the user_logs dictionary that get_status() reads from.
        def log_bridge(user_id, message, log_type="info"):
            add_log(user_id, message, log_type)

        # Pass the bridge into the scraper
        run_scraper(log_callback=log_bridge)

    except Exception as e:
        scraper_status['running'] = False
        scraper_status['error'] = str(e)
        print(f"❌ Scraper error: {e}", flush=True)

_scraper_thread = None
_scraper_thread_started = False
_cleanup_thread = None
_cleanup_thread_started = False

def ensure_scraper_thread_started():
    """
    When running under gunicorn, __main__ doesn't execute.
    We optionally start the background scraper thread on import.

    Set ENABLE_SCRAPER_THREAD=1 on Render if this web service should also run the scraper loop.
    """
    global _scraper_thread, _scraper_thread_started
    if _scraper_thread_started:
        return
    if os.getenv("ENABLE_SCRAPER_THREAD", "0") != "1":
        return
    _scraper_thread = threading.Thread(target=start_background_scraper, daemon=True)
    _scraper_thread.start()
    _scraper_thread_started = True

ensure_scraper_thread_started()


def ensure_cleanup_thread_started():
    global _cleanup_thread, _cleanup_thread_started
    if _cleanup_thread_started:
        return
    if os.getenv("ENABLE_CLEANUP_THREAD", "1") != "1":
        return
    _cleanup_thread = threading.Thread(target=cleanup_old_listings, daemon=True)
    _cleanup_thread.start()
    _cleanup_thread_started = True


ensure_cleanup_thread_started()


# ==========================================
# API ENDPOINTS
# ==========================================
@app.route('/')
def health_check():
    return jsonify({"status": "running", "scraper_active": scraper_status['running']})


def _detect_local_browser_binary():
    windows_candidates = [
        os.path.join(os.getenv("PROGRAMFILES", r"C:\Program Files"), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.getenv("LOCALAPPDATA", r"C:\Users\Default\AppData\Local"), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.getenv("PROGRAMFILES", r"C:\Program Files"), r"Microsoft\Edge\Application\msedge.exe"),
    ]
    linux_candidates = [
        '/usr/bin/google-chrome',
        '/usr/bin/google-chrome-stable',
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        '/opt/google/chrome/chrome',
    ]
    for p in [*windows_candidates, *linux_candidates]:
        if os.path.exists(p):
            return p
    return (
        shutil.which("google-chrome")
        or shutil.which("chrome")
        or shutil.which("chromium")
        or shutil.which("msedge")
    )


@app.route('/api/scraper-health', methods=['GET', 'OPTIONS'])
@app.route('/scraper-health', methods=['GET', 'OPTIONS'])
@require_auth
def scraper_health(user_id):
    if request.method == 'OPTIONS':
        return '', 200
    remote_url = os.getenv('SELENIUM_REMOTE_URL', '').strip()
    local_browser = _detect_local_browser_binary()
    offerup_mode = "remote" if remote_url else ("local" if local_browser else "disabled")
    return jsonify({
        "ok": True,
        "scraper_running": bool(scraper_status.get('running')),
        "scraper_error": scraper_status.get('error'),
        "scraper_thread_started": bool(_scraper_thread_started),
        "cleanup_thread_started": bool(_cleanup_thread_started),
        "offerup_mode": offerup_mode,
        "selenium_remote_enabled": bool(remote_url),
        "local_browser_detected": bool(local_browser),
        "local_browser_path": local_browser,
        "timestamp": int(time.time()),
    })


@app.route('/api/status', methods=['GET', 'OPTIONS'])
@app.route('/api/status/', methods=['GET', 'OPTIONS'])
@app.route('/status', methods=['GET', 'OPTIONS'])
@app.route('/status/', methods=['GET', 'OPTIONS'])
@require_auth
def get_status(user_id):
    if request.method == 'OPTIONS':
        return '', 200

    conn = get_db_connection()
    if not conn: return jsonify({"error": "DB error"}), 500

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM user_settings WHERE user_id = %s", (user_id,))
        us = cursor.fetchone()

        if not us:
            return jsonify({
                "running": False,
                "subscription_status": "inactive",
                "next_check_timestamp": 0,
                "listings_count": 0,
                "recent_activity": []
            })

        # 1. Permission & State
        is_pro = us.get('is_pro', False)
        is_running = us.get('is_active', False)

        # 2. Timer Logic (Always active)
        interval_min = us.get('check_interval_minutes') or 10
        interval_secs = int(interval_min) * 60

        last_raw = us.get('last_scraped_at')
        last_scraped = _epoch_from_last_scraped(last_raw)

        # Calculate when the next check SHOULD be
        next_check_timestamp = last_scraped + interval_secs

        # If the timer has already run out, keep it at 'current'
        # so the UI stays at 0:00 instead of showing negative numbers
        current_now = time.time()
        if current_now > next_check_timestamp:
            next_check_timestamp = current_now

        # 3. Stats
        cursor.execute("SELECT COUNT(*) AS c FROM listings WHERE user_id = %s", (user_id,))
        listings_count = cursor.fetchone()['c']

        return jsonify({
            "status": "running" if is_running else "stopped",
            "running": is_running,
            "subscription_status": "active" if is_pro else "inactive",
            "listings_count": listings_count,
            "last_scrape_duration_ms": us.get('last_scrape_duration_ms') or 0,
            "items_scanned_today": 0,
            "matches_found_today": 0,
            "next_check_timestamp": next_check_timestamp,
            "recent_activity": user_logs.get(user_id, [])
        })
    finally:
        cursor.close()
        conn.close()


@app.route('/api/settings', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/api/settings/', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/settings', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/settings/', methods=['GET', 'POST', 'OPTIONS'])
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
                    "subscription_status": "inactive",
                    "is_pro": False,
                    "subscription_current_period_end": None,
                    "subscription_cancel_at_period_end": False,
                })

            strict_map = {'lenient': 1, 'balanced': 2, 'strict': 3}
            is_pro = bool(us.get('is_pro'))
            sub_display = 'active' if is_pro else (us.get('subscription_status') or 'inactive')
            if is_pro:
                sub_display = 'active'
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
                "subscription_status": sub_display,
                "is_pro": is_pro,
                "subscription_current_period_end": us.get('subscription_current_period_end'),
                "subscription_cancel_at_period_end": bool(us.get('subscription_cancel_at_period_end')),
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
@app.route('/api/start/', methods=['POST', 'OPTIONS'])
@app.route('/start', methods=['POST', 'OPTIONS'])
@app.route('/start/', methods=['POST', 'OPTIONS'])
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
@app.route('/api/stop/', methods=['POST', 'OPTIONS'])
@app.route('/stop', methods=['POST', 'OPTIONS'])
@app.route('/stop/', methods=['POST', 'OPTIONS'])
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


@app.route('/api/webhook', methods=['POST'])
@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=False)
    sig_header = request.headers.get('Stripe-Signature') or request.environ.get('HTTP_STRIPE_SIGNATURE')
    secret = os.getenv('STRIPE_WEBHOOK_SECRET')
    if not secret:
        print("STRIPE_WEBHOOK_SECRET missing", flush=True)
        return jsonify(success=False, error="webhook not configured"), 500

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except Exception as e:
        print(f"Stripe webhook verify failed: {e}", flush=True)
        return jsonify(success=False), 400

    conn = get_db_connection()
    if not conn:
        return jsonify(success=False, error="db"), 500

    cursor = None
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        etype = event['type']

        if etype == 'checkout.session.completed':
            sess = event['data']['object']
            mode = sess.get('mode')
            if mode != 'subscription':
                conn.commit()
                return jsonify(success=True)
            user_id = sess.get('client_reference_id') or (sess.get('metadata') or {}).get('user_id')
            customer_id = sess.get('customer')
            sub_id = sess.get('subscription')
            if user_id and customer_id and sub_id:
                sub = stripe.Subscription.retrieve(sub_id)
                pe = sub.get('current_period_end')
                ca = bool(sub.get('cancel_at_period_end'))
                _upsert_pro_subscription(cursor, user_id, customer_id, sub_id, pe, ca)
            conn.commit()

        elif etype == 'customer.subscription.updated':
            sub = event['data']['object']
            customer_id = sub.get('customer')
            status = sub.get('status') or ''
            pe = sub.get('current_period_end')
            ca = bool(sub.get('cancel_at_period_end'))
            entitled = _subscription_is_entitled(status)
            cursor.execute(
                "SELECT user_id FROM user_settings WHERE stripe_customer_id = %s LIMIT 1",
                (customer_id,)
            )
            row = cursor.fetchone()
            if row:
                _apply_subscription_row(
                    cursor, row['user_id'], entitled,
                    period_end=pe, cancel_at_end=ca, sub_id=sub.get('id')
                )
            conn.commit()

        elif etype == 'customer.subscription.deleted':
            sub = event['data']['object']
            customer_id = sub.get('customer')
            cursor.execute(
                "SELECT user_id FROM user_settings WHERE stripe_customer_id = %s LIMIT 1",
                (customer_id,)
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    """
                    UPDATE user_settings SET
                        is_pro = FALSE,
                        subscription_status = 'inactive',
                        stripe_subscription_id = NULL,
                        subscription_current_period_end = NULL,
                        subscription_cancel_at_period_end = FALSE
                    WHERE user_id = %s
                    """,
                    (row['user_id'],)
                )
            conn.commit()

        else:
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Stripe webhook handler error: {e}", flush=True)
        return jsonify(success=False), 500
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()

    return jsonify(success=True)


@app.route('/api/update-password', methods=['POST', 'OPTIONS'])
@app.route('/update-password', methods=['POST', 'OPTIONS'])
@require_auth
def update_password(user_id):
    if request.method == 'OPTIONS':
        return '', 200

    data = request.json
    new_password = data.get('new_password')

    # We MUST get the token from the request to tell Supabase WHO is changing the password
    auth_header = request.headers.get('Authorization')

    # Supabase Auth endpoint for updating the current user
    update_url = f"{SUPABASE_URL}/auth/v1/user"

    headers = {
        "Authorization": auth_header,
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json"
    }

    try:
        print(f"🔑 Attempting password update for user: {user_id}")

        # Supabase API expects the password inside a JSON body
        response = requests.put(
            update_url,
            headers=headers,
            json={"password": new_password}
        )

        print(f"📡 Supabase Response Code: {response.status_code}")

        if response.status_code == 200:
            return jsonify({"success": True})
        else:
            error_data = response.json()
            print(f"❌ Supabase Rejected: {error_data}")
            return jsonify({
                "success": False,
                "error": error_data.get('msg') or error_data.get('error_description') or "Update failed"
            }), response.status_code

    except Exception as e:
        print(f"🔥 Python Crash in update_password: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/update-email', methods=['POST', 'OPTIONS'])
@app.route('/update-email', methods=['POST', 'OPTIONS'])
@require_auth
def update_email(user_id):
    if request.method == 'OPTIONS':
        return '', 200

    data = request.json or {}
    new_email = (data.get('new_email') or '').strip().lower()
    code = (data.get('code') or '').strip()
    if not new_email or not code:
        return jsonify({"success": False, "error": "Email and code are required"}), 400
    if '@' not in new_email or '.' not in new_email.split('@')[-1]:
        return jsonify({"success": False, "error": "Email format is invalid"}), 400

    code_hash = hashlib.sha256(code.encode('utf-8')).hexdigest()
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "error": "Database error"}), 500
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        _ensure_email_change_table(cursor)
        cursor.execute(
            """
            SELECT new_email, code_hash, expires_at, (expires_at > NOW()) AS is_valid
            FROM email_change_requests
            WHERE user_id = %s
            LIMIT 1
            """,
            (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"success": False, "error": "No verification request found"}), 400
        if row['new_email'] != new_email:
            return jsonify({"success": False, "error": "Email does not match requested address"}), 400
        if not row.get('is_valid'):
            return jsonify({"success": False, "error": "Verification code expired"}), 400
        if row['code_hash'] != code_hash:
            return jsonify({"success": False, "error": "Invalid verification code"}), 400

        # Prefer admin API (backend service role) for reliability.
        response = None
        if SUPABASE_SERVICE_ROLE_KEY:
            admin_url = f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}"
            admin_headers = {
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Content-Type": "application/json"
            }
            response = requests.put(
                admin_url,
                headers=admin_headers,
                json={"email": new_email, "email_confirm": True},
                timeout=20,
            )
        else:
            auth_header = request.headers.get('Authorization')
            update_url = f"{SUPABASE_URL}/auth/v1/user"
            headers = {
                "Authorization": auth_header,
                "apikey": SUPABASE_ANON_KEY,
                "Content-Type": "application/json"
            }
            response = requests.put(
                update_url,
                headers=headers,
                json={"email": new_email},
                timeout=20,
            )

        if response.status_code not in (200, 201):
            try:
                error_data = response.json()
            except Exception:
                error_data = {}
            source = "supabase-admin" if SUPABASE_SERVICE_ROLE_KEY else "supabase-user"
            return jsonify({
                "success": False,
                "error": (
                    error_data.get('msg')
                    or error_data.get('error_description')
                    or error_data.get('message')
                    or f"Email update failed via {source}"
                )
            }), response.status_code

        cursor.execute("DELETE FROM email_change_requests WHERE user_id = %s", (user_id,))
        conn.commit()
        return jsonify({"success": True, "email": new_email})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        try:
            cursor.close()
            conn.close()
        except Exception:
            pass


@app.route('/api/request-email-change', methods=['POST', 'OPTIONS'])
@app.route('/request-email-change', methods=['POST', 'OPTIONS'])
@require_auth
def request_email_change(user_id):
    if request.method == 'OPTIONS':
        return '', 200
    data = request.json or {}
    new_email = (data.get('new_email') or '').strip().lower()
    if not new_email:
        return jsonify({"success": False, "error": "Email is required"}), 400
    code = f"{secrets.randbelow(1000000):06d}"
    code_hash = hashlib.sha256(code.encode('utf-8')).hexdigest()
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database error"}), 500
    cursor = None
    try:
        cursor = conn.cursor()
        _ensure_email_change_table(cursor)
        cursor.execute(
            """
            INSERT INTO email_change_requests (user_id, new_email, code_hash, expires_at, created_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                new_email = EXCLUDED.new_email,
                code_hash = EXCLUDED.code_hash,
                expires_at = EXCLUDED.expires_at,
                created_at = NOW()
            """,
            (user_id, new_email, code_hash, expires_at)
        )
        _send_email_change_code_email(new_email, code)
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        msg = str(e)
        if "Mailgun send failed" in msg:
            # Friendly troubleshooting hint for common Mailgun recipient/domain issues.
            return jsonify({
                "success": False,
                "error": f"{msg}. Verify Mailgun domain DNS is active and recipient format is valid."
            }), 400
        return jsonify({"success": False, "error": msg}), 500
    finally:
        if cursor:
            cursor.close()
        conn.close()

@app.route('/api/create-checkout-session', methods=['POST', 'OPTIONS'])
@app.route('/create-checkout-session', methods=['POST', 'OPTIONS'])
@require_auth
def create_checkout(user_id):
    if request.method == 'OPTIONS':
        return '', 200
    price_id = os.getenv('STRIPE_PRICE_ID')
    if not price_id:
        return jsonify({"error": "STRIPE_PRICE_ID not configured"}), 500
    if not stripe.api_key:
        return jsonify({"error": "STRIPE_SECRET_KEY not configured"}), 500
    base = _frontend_base_url()
    try:
        checkout_session = stripe.checkout.Session.create(
            mode='subscription',
            line_items=[{'price': price_id, 'quantity': 1}],
            client_reference_id=user_id,
            metadata={'user_id': user_id},
            success_url=f"{base}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/?checkout=canceled",
            allow_promotion_codes=True,
        )
        return jsonify({'url': checkout_session.url})
    except Exception as e:
        print(f"create_checkout: {e}", flush=True)
        return jsonify(error=str(e)), 500


@app.route('/api/complete-checkout', methods=['POST', 'OPTIONS'])
@app.route('/complete-checkout', methods=['POST', 'OPTIONS'])
@require_auth
def complete_checkout(user_id):
    """
    Called when the user returns from Stripe Checkout with ?session_id=...
    Verifies the session server-side and upserts Pro status (works without webhooks in dev).
    """
    if request.method == 'OPTIONS':
        return '', 200
    if not stripe.api_key:
        return jsonify({"error": "STRIPE_SECRET_KEY not configured"}), 500
    data = request.get_json(silent=True) or {}
    session_id = data.get('session_id')
    if not session_id or not isinstance(session_id, str) or not session_id.startswith('cs_'):
        return jsonify({"error": "Invalid or missing session_id"}), 400
    try:
        sess = stripe.checkout.Session.retrieve(
            session_id,
            expand=['subscription'],
        )
    except Exception as e:
        print(f"complete_checkout retrieve: {e}", flush=True)
        return jsonify({"error": "Could not verify checkout session"}), 400

    ref = sess.get('client_reference_id') or (sess.get('metadata') or {}).get('user_id')
    if not ref or str(ref) != str(user_id):
        return jsonify({"error": "Session does not match signed-in user"}), 403

    if sess.get('mode') != 'subscription':
        return jsonify({"error": "Not a subscription checkout"}), 400

    pay_status = sess.get('payment_status') or ''
    if pay_status not in ('paid', 'no_payment_required'):
        return jsonify({"error": f"Payment not complete ({pay_status})"}), 400

    customer_id = sess.get('customer')
    sub_raw = sess.get('subscription')
    if not sub_raw:
        return jsonify({"error": "No subscription on session"}), 400
    if isinstance(sub_raw, str):
        sub_id = sub_raw
        sub = stripe.Subscription.retrieve(sub_id)
        pe = sub.get('current_period_end')
        ca = bool(sub.get('cancel_at_period_end'))
    else:
        # Expanded Subscription object (StripeObject behaves like a mapping)
        sub_id = sub_raw.get('id')
        pe = sub_raw.get('current_period_end')
        ca = bool(sub_raw.get('cancel_at_period_end'))

    if not customer_id or not sub_id:
        return jsonify({"error": "Missing customer or subscription"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cursor = conn.cursor()
        _upsert_pro_subscription(cursor, user_id, customer_id, sub_id, pe, ca)
        conn.commit()
        return jsonify({"success": True, "subscription_status": "active"})
    except Exception as e:
        conn.rollback()
        print(f"complete_checkout db: {e}", flush=True)
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/create-portal-session', methods=['POST', 'OPTIONS'])
@app.route('/create-portal-session', methods=['POST', 'OPTIONS'])
@require_auth
def create_portal_session(user_id):
    if request.method == 'OPTIONS':
        return '', 200
    if not stripe.api_key:
        return jsonify({"error": "STRIPE_SECRET_KEY not configured"}), 500
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT stripe_customer_id FROM user_settings WHERE user_id = %s",
            (user_id,)
        )
        row = cursor.fetchone()
        customer_id = row['stripe_customer_id'] if row else None
        if not customer_id:
            return jsonify({
                "error": "no_customer",
                "message": "Subscribe once from Upgrade so we can link your billing account."
            }), 400
        base = _frontend_base_url()
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{base}/",
        )
        return jsonify({"url": portal.url})
    except Exception as e:
        print(f"create_portal_session: {e}", flush=True)
        return jsonify(error=str(e)), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/cancel-subscription', methods=['POST', 'OPTIONS'])
@app.route('/cancel-subscription', methods=['POST', 'OPTIONS'])
@require_auth
def cancel_subscription_at_period_end(user_id):
    if request.method == 'OPTIONS':
        return '', 200
    if not stripe.api_key:
        return jsonify({"error": "STRIPE_SECRET_KEY not configured"}), 500
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT stripe_subscription_id FROM user_settings WHERE user_id = %s",
            (user_id,)
        )
        row = cursor.fetchone()
        sub_id = row['stripe_subscription_id'] if row else None
        if not sub_id:
            return jsonify({"error": "No active subscription on file"}), 400
        stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
        sub = stripe.Subscription.retrieve(sub_id)
        _apply_subscription_row(
            cursor, user_id, _subscription_is_entitled(sub.get('status', '')),
            period_end=sub.get('current_period_end'),
            cancel_at_end=True,
            sub_id=sub_id
        )
        conn.commit()
        return jsonify({"success": True, "cancel_at_period_end": True})
    except Exception as e:
        conn.rollback()
        print(f"cancel_subscription: {e}", flush=True)
        return jsonify(error=str(e)), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/listings', methods=['GET', 'OPTIONS'])
@app.route('/listings', methods=['GET', 'OPTIONS'])
@require_auth
def list_scraped_listings(user_id):
    if request.method == 'OPTIONS':
        return '', 200
    try:
        limit = min(max(int(request.args.get('limit', 40)), 1), 100)
        offset = max(int(request.args.get('offset', 0)), 0)
    except ValueError:
        return jsonify({"error": "invalid pagination"}), 400
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT title, price, link, platform, image_url, location, created_at
            FROM listings
            WHERE user_id = %s
            ORDER BY created_at DESC NULLS LAST
            LIMIT %s OFFSET %s
            """,
            (user_id, limit, offset),
        )
        rows = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) AS c FROM listings WHERE user_id = %s", (user_id,))
        total = cursor.fetchone()['c']
        out = []
        for r in rows:
            created = r['created_at']
            if hasattr(created, 'isoformat'):
                created = created.isoformat()
            price = r['price']
            if price is not None:
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    pass
            out.append({
                "title": r['title'],
                "price": price,
                "link": r['link'],
                "platform": r['platform'],
                "image_url": r.get('image_url'),
                "location": r.get('location'),
                "created_at": created,
            })
        return jsonify({"listings": out, "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        print(f"listings: {e}", flush=True)
        return jsonify(error=str(e)), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/listings/feedback', methods=['POST', 'OPTIONS'])
@app.route('/listings/feedback', methods=['POST', 'OPTIONS'])
@require_auth
def mark_listing_feedback(user_id):
    if request.method == 'OPTIONS':
        return '', 200
    data = request.get_json(silent=True) or {}
    link = (data.get('link') or '').strip()
    reason = (data.get('reason') or '').strip().lower()
    if not link:
        return jsonify({"error": "Missing listing link"}), 400
    if reason not in ('sold', 'not_a_deal'):
        return jsonify({"error": "Invalid feedback reason"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT title, price, link, platform, location, image_url, title_fingerprint
            FROM listings
            WHERE user_id = %s AND link = %s
            LIMIT 1
            """,
            (user_id, link)
        )
        row = cursor.fetchone()
        title = row['title'] if row else None
        price = row['price'] if row else None
        title_fp = row.get('title_fingerprint') if row else None
        if not title_fp and title:
            norm = ''.join(ch.lower() if ch.isalnum() else ' ' for ch in title)
            parts = [p for p in norm.split() if len(p) > 1][:10]
            title_fp = ' '.join(parts) if parts else None

        cursor.execute(
            """
            INSERT INTO listings_feedback (user_id, link, title, price, title_fingerprint, reason, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (user_id, link) DO UPDATE SET
                title = EXCLUDED.title,
                price = EXCLUDED.price,
                title_fingerprint = EXCLUDED.title_fingerprint,
                reason = EXCLUDED.reason
            """,
            (user_id, link, title, price, title_fp, reason)
        )
        cursor.execute(
            "DELETE FROM listings WHERE user_id = %s AND link = %s",
            (user_id, link)
        )
        conn.commit()
        return jsonify({"success": True})
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