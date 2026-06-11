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
from psycopg2 import errorcodes
from psycopg2.extras import RealDictCursor
from functools import wraps
import requests
import re
from datetime import datetime, timedelta, timezone
import stripe

from dotenv import load_dotenv
load_dotenv()

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

# Web Push VAPID configuration
VAPID_PUBLIC_KEY = (os.getenv('VAPID_PUBLIC_KEY') or '').strip()
VAPID_PRIVATE_KEY = (os.getenv('VAPID_PRIVATE_KEY') or '').strip()
VAPID_CLAIMS_EMAIL = (os.getenv('VAPID_CLAIMS_EMAIL') or 'admin@pixelflip.app').strip()

_dsns = (os.getenv('SENTRY_DSN') or '').strip()
if _dsns:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration

        sentry_sdk.init(
            dsn=_dsns,
            integrations=[FlaskIntegration()],
            traces_sample_rate=float(os.getenv('SENTRY_TRACES_SAMPLE_RATE', '0') or '0'),
            profiles_sample_rate=float(os.getenv('SENTRY_PROFILES_SAMPLE_RATE', '0') or '0'),
            enable_tracing=bool(float(os.getenv('SENTRY_TRACES_SAMPLE_RATE', '0') or '0')),
        )
    except Exception as _se:
        print(f"Sentry init skipped: {_se}", flush=True)

try:
    from scraper_multi_user import SCRAPING_USERS
except Exception:
    SCRAPING_USERS = set()

# Health check tracking (set by scraper thread)
_health_last_scraper_cycle = None

def set_health_scraper_cycle(ts=None):
    """Called by scraper to report last successful cycle."""
    global _health_last_scraper_cycle
    _health_last_scraper_cycle = ts or time.time()

app = Flask(__name__)


def _frontend_base_url(origin_header=None):
    """
    Stripe success/cancel URLs and billing portal return_url.
    Set FRONTEND_URL in production. For local dev, if unset, we accept the
    browser Origin (e.g. http://localhost:3001) so cancel links match the port you use.
    """
    explicit = (os.getenv('FRONTEND_URL') or '').strip().rstrip('/')
    if explicit:
        return explicit
    oh = (origin_header or '').strip().rstrip('/')
    if oh.startswith('http://localhost:') or oh.startswith('http://127.0.0.1:'):
        return oh
    return 'http://localhost:3000'


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


def _send_password_reset_code_email(to_email, code):
    mailgun_api_key = os.getenv('MAILGUN_API_KEY')
    mailgun_domain = os.getenv('MAILGUN_DOMAIN')
    mailgun_from = os.getenv('MAILGUN_FROM_EMAIL')
    mailgun_base_url = os.getenv('MAILGUN_BASE_URL', 'https://api.mailgun.net')

    if not all([mailgun_api_key, mailgun_domain, mailgun_from]):
        raise ValueError("Mailgun settings missing (MAILGUN_API_KEY/MAILGUN_DOMAIN/MAILGUN_FROM_EMAIL)")

    endpoint = f"{mailgun_base_url.rstrip('/')}/v3/{mailgun_domain}/messages"
    subject = "PixelFlip password reset code"
    text_body = (
        f"Your PixelFlip password reset code is: {code}\n\n"
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


def _ensure_password_reset_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_requests (
          email TEXT PRIMARY KEY,
          code_hash TEXT NOT NULL,
          expires_at TIMESTAMPTZ NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _admin_lookup_supabase_user_by_email(email):
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise ValueError("SUPABASE_SERVICE_ROLE_KEY is required for password reset")
    url = f"{SUPABASE_URL}/auth/v1/admin/users"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
    }
    page = 1
    per_page = 200
    while page <= 5:
        resp = requests.get(
            url,
            headers=headers,
            params={"page": page, "per_page": per_page},
            timeout=20,
        )
        if resp.status_code != 200:
            raise ValueError(f"Supabase admin users lookup failed ({resp.status_code})")
        data = resp.json() or {}
        users = data.get('users') or []
        if not users:
            break
        target = (email or '').strip().lower()
        for u in users:
            u_email = (u.get('email') or '').strip().lower()
            if u_email == target:
                return u
        if len(users) < per_page:
            break
        page += 1
    return None


def _epoch_from_last_scraped(value):
    """Normalize last_scraped_at / timestamp to unix seconds for countdown."""
    if value is None:
        return 0
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _subscription_is_entitled(status):
    return status in ('active', 'trialing')


def _first_stripe_price_id_from_env(*keys):
    """First non-empty Stripe Price id from env (supports comma-separated lists)."""
    for key in keys:
        raw = (os.getenv(key) or '').strip()
        for part in raw.split(','):
            p = part.strip()
            if p:
                return p
    return None


def _stripe_price_to_tier(price_id):
    """Map Stripe Price id to basic | pro (defaults to pro if unknown)."""
    if not price_id:
        return 'pro'
    pid = str(price_id).strip()
    basic_ids = {x.strip() for x in os.getenv('STRIPE_PRICE_BASIC_ID', '').split(',') if x.strip()}
    pro_ids = {x.strip() for x in os.getenv('STRIPE_PRICE_PRO_ID', '').split(',') if x.strip()}
    legacy = os.getenv('STRIPE_PRICE_ID', '').strip()
    if pid in basic_ids:
        return 'basic'
    if pid in pro_ids:
        return 'pro'
    if legacy and pid == legacy:
        return 'pro'
    return 'pro'


def _effective_plan_tier(us):
    if not us:
        return 'inactive'
    pt = (us.get('plan_tier') or '').strip().lower()
    if pt in ('basic', 'pro'):
        return pt
    if us.get('is_pro'):
        return 'pro'
    return 'inactive'


def _plan_limits(tier):
    t = (tier or 'inactive').lower()
    if t == 'pro':
        return {'max_search_terms': 10, 'ai_image_allowed': True}
    if t == 'basic':
        return {'max_search_terms': 3, 'ai_image_allowed': False}
    try:
        # Pre-beta / trial: allow a few terms before checkout; set to 0 in production if you require payment first.
        inactive_cap = int(os.getenv('MAX_SEARCH_TERMS_INACTIVE', '3'))
    except ValueError:
        inactive_cap = 3
    return {'max_search_terms': max(0, inactive_cap), 'ai_image_allowed': False}


def _effective_limits(us):
    """Tier defaults, with optional per-row overrides from Supabase (set manually or via admin)."""
    tier = _effective_plan_tier(us)
    lim = dict(_plan_limits(tier))
    if not us:
        return lim
    o = us.get('max_search_terms_override')
    if o is not None:
        try:
            lim['max_search_terms'] = max(0, int(o))
        except (TypeError, ValueError):
            pass
    oa = us.get('ai_image_allowed_override')
    if oa is not None:
        lim['ai_image_allowed'] = bool(oa)
    return lim


def _effective_check_interval_minutes(us):
    """Pro + Facebook Marketplace (Bright Data) enforces at least a 30-minute poll interval."""
    if not us:
        return 10
    try:
        stored = int(us.get('check_interval_minutes') or 10)
    except (TypeError, ValueError):
        stored = 10
    plat = us.get('platforms') or {}
    if isinstance(plat, str):
        try:
            plat = json.loads(plat)
        except Exception:
            plat = {}
    plat = plat or {}
    if _effective_plan_tier(us) == 'pro' and plat.get('facebook'):
        return max(stored, 30)
    return stored


def _plan_display_name(tier):
    t = (tier or 'inactive').lower()
    if t == 'basic':
        return 'Basic Scanner'
    if t == 'pro':
        return 'Pro Scanner'
    return None


def _normalize_notification_channels(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        'email': bool(raw.get('email', True)),
        'sms': bool(raw.get('sms', False)),
        'push': bool(raw.get('push', False)),
    }


def _sanitize_contact_phone(raw):
    """Return None for cleared field; digits-only or +E.164-ish string; raises ValueError if invalid."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = re.sub(r'\D', '', s)
    if len(digits) < 10:
        raise ValueError('Phone number must include at least 10 digits')
    if s.startswith('+'):
        return '+' + digits[:15]
    return digits[:15]


def _billing_config_response():
    def _f(key, default):
        try:
            return float(os.getenv(key, str(default)))
        except ValueError:
            return float(default)

    prebeta = os.getenv('PREBETA_ACTIVE', '').lower() in ('1', 'true', 'yes')
    return {
        'prebeta_active': prebeta,
        'price_basic_standard': _f('DISPLAY_PRICE_BASIC_STANDARD', 9.99),
        'price_pro_standard': _f('DISPLAY_PRICE_PRO_STANDARD', 19.99),
        'price_basic_prebeta': _f('DISPLAY_PRICE_BASIC_PREBETA', 4.99),
        'price_pro_prebeta': _f('DISPLAY_PRICE_PRO_PREBETA', 9.99),
    }


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


def _upsert_pro_subscription(cursor, user_id, customer_id, sub_id, period_end, cancel_at_end, plan_tier='pro'):
    """
    Upsert subscription after Checkout or webhook.
    plan_tier: basic | pro — controls AI image access and search-term limits.
    """
    platforms = json.dumps({"craigslist": True, "offerup": True, "mercari": True, "facebook": False})
    plan_tier = (plan_tier or 'pro').lower()
    if plan_tier not in ('basic', 'pro'):
        plan_tier = 'pro'
    is_pro = plan_tier == 'pro'
    ai_on = is_pro
    cursor.execute(
        """
        INSERT INTO user_settings (
            user_id, zip_code, search_radius, platforms, ai_enabled,
            check_interval_minutes, ai_strictness,
            is_pro, subscription_status, plan_tier,
            stripe_customer_id, stripe_subscription_id,
            subscription_current_period_end, subscription_cancel_at_period_end
        )
        VALUES (
            %s, '95212', 25, %s::jsonb, %s, 10, 'balanced',
            %s, 'active', %s, %s, %s, %s, %s
        )
        ON CONFLICT (user_id) DO UPDATE SET
            is_pro = EXCLUDED.is_pro,
            ai_enabled = EXCLUDED.ai_enabled,
            subscription_status = 'active',
            plan_tier = EXCLUDED.plan_tier,
            stripe_customer_id = EXCLUDED.stripe_customer_id,
            stripe_subscription_id = EXCLUDED.stripe_subscription_id,
            subscription_current_period_end = EXCLUDED.subscription_current_period_end,
            subscription_cancel_at_period_end = EXCLUDED.subscription_cancel_at_period_end
        """,
        (user_id, platforms, ai_on, is_pro, plan_tier, customer_id, sub_id, period_end, cancel_at_end),
    )

_ALLOWED_PLATFORMS = frozenset(('craigslist', 'offerup', 'mercari', 'facebook'))


def _parse_platform_filters(req):
    """Single `platform` or comma-separated `platforms` → normalized list (empty = no filter)."""
    multi = (req.args.get('platforms') or '').strip().lower()
    single = (req.args.get('platform') or '').strip().lower()
    raw = multi or single
    if not raw:
        return []
    out = [p for p in re.split(r'[\s,]+', raw) if p]
    bad = [p for p in out if p not in _ALLOWED_PLATFORMS]
    if bad:
        raise ValueError(f"invalid platform: {bad}")
    return out


def _user_settings_buyer_prefs(us):
    if not us:
        return True, True
    return bool(us.get('buyer_include_local', True)), bool(us.get('buyer_include_shipping', True))


def _coerce_buyer_prefs_from_post(data, prev_row):
    pl, ps = _user_settings_buyer_prefs(prev_row)
    if 'buyer_include_local' in data:
        pl = bool(data.get('buyer_include_local'))
    if 'buyer_include_shipping' in data:
        ps = bool(data.get('buyer_include_shipping'))
    if not pl and not ps:
        raise ValueError('Select at least one: local or shipping.')
    return pl, ps


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
for _o in (os.getenv("CORS_ORIGINS") or "").split(","):
    _o = _o.strip()
    if _o and _o not in _cors_origins:
        _cors_origins.append(_o)

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
        conn = psycopg2.connect(DATABASE_URL)
        try:
            from db_schema import ensure_buyer_delivery_columns, ensure_push_subscription_column
            ensure_buyer_delivery_columns(conn)
            ensure_push_subscription_column(conn)
        except Exception as schema_err:
            print(f"Schema ensure warning: {schema_err}", flush=True)
        return conn
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
# Looks like: { "user_id_123": [{"ts": 1712345678.9, "time": "…", "message": "…", "type": "info"}] }
user_logs = {}


def add_log(user_id, message, log_type="info"):
    """Saves a log to the specific user's buffer to be sent to React.
    `ts` is UTC epoch seconds so the browser can format in the viewer's local timezone."""
    if user_id not in user_logs:
        user_logs[user_id] = []

    now_utc = datetime.now(timezone.utc)
    ts = now_utc.timestamp()
    # Legacy `time` kept as UTC label so old cached clients are not misleading vs local `ts` display.
    time_utc = now_utc.strftime("%I:%M:%S %p UTC")
    user_logs[user_id].append({"ts": ts, "time": time_utc, "message": message, "type": log_type})

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

        # Pass the bridge and health callback into the scraper
        run_scraper(log_callback=log_bridge, health_callback=set_health_scraper_cycle)

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
def root_status():
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
    return jsonify({
        "ok": True,
        "scraper_running": bool(scraper_status.get('running')),
        "scraper_error": scraper_status.get('error'),
        "scraper_thread_started": bool(_scraper_thread_started),
        "cleanup_thread_started": bool(_cleanup_thread_started),
        "scrape_provider": "scrapingbee",
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
                "scraping_in_progress": False,
                "recent_activity": []
            })

        # 1. Permission & State (Basic tier is is_pro=False but subscription_status may still be 'active')
        is_pro = bool(us.get('is_pro'))
        ss_raw = (us.get('subscription_status') or '').strip().lower()
        subscription_entitled = ss_raw in ('active', 'trialing') or is_pro
        is_running = us.get('is_active', False)

        # 2. Timer Logic (Always active)
        interval_min = _effective_check_interval_minutes(us)
        interval_secs = int(interval_min) * 60

        last_raw = us.get('last_scraped_at')
        last_scraped = _epoch_from_last_scraped(last_raw)

        # Calculate when the next check SHOULD be (countdown starts after last scrape *finished*)
        next_check_timestamp = last_scraped + interval_secs

        current_now = time.time()
        scraping_in_progress = user_id in SCRAPING_USERS

        # While a scrape is running, do not advance the countdown target — the UI pauses until the run completes.
        if scraping_in_progress:
            next_check_timestamp_out = None
        else:
            next_check_timestamp_out = next_check_timestamp
            # If we are overdue, keep showing "SCANNING..." until a run finishes and stamps last_scraped_at.
            # This avoids countdown flicker (e.g., 0:01 -> SCANNING) during long/active cycles.
            if current_now > next_check_timestamp_out:
                next_check_timestamp_out = None

        # 3. Stats
        cursor.execute("SELECT COUNT(*) AS c FROM listings WHERE user_id = %s", (user_id,))
        listings_count = cursor.fetchone()['c']

        return jsonify({
            "status": "running" if is_running else "stopped",
            "running": is_running,
            "subscription_status": "active" if subscription_entitled else "inactive",
            "listings_count": listings_count,
            "last_scrape_duration_ms": us.get('last_scrape_duration_ms') or 0,
            "items_scanned_today": 0,
            "matches_found_today": 0,
            "next_check_timestamp": next_check_timestamp_out,
            "scraping_in_progress": scraping_in_progress,
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
                    "platforms": {"craigslist": True, "offerup": True, "mercari": True, "facebook": False},
                    "zip_code": "95212",
                    "distance": 25,
                    "check_interval": 10,
                    "thresholds": terms,
                    "excluded_keywords": exclusions,
                    "ai_detection": False,
                    "strictness": 3,
                    "subscription_status": "inactive",
                    "is_pro": False,
                    "plan_tier": "inactive",
                    "plan_name": None,
                    "max_search_terms": _plan_limits('inactive')['max_search_terms'],
                    "ai_image_allowed": False,
                    "subscription_current_period_end": None,
                    "subscription_cancel_at_period_end": False,
                    "notifications": _normalize_notification_channels(None),
                    "contact_phone": "",
                    "buyer_include_local": True,
                    "buyer_include_shipping": True,
                    "billing": _billing_config_response(),
                })

            strict_map = {'lenient': 1, 'balanced': 2, 'strict': 3}
            is_pro = bool(us.get('is_pro'))
            sub_display = 'active' if is_pro else (us.get('subscription_status') or 'inactive')
            if is_pro:
                sub_display = 'active'
            pt = _effective_plan_tier(us)
            limits = _effective_limits(us)
            ai_allowed = limits['ai_image_allowed']
            ai_show = bool(us.get('ai_enabled')) and ai_allowed
            return jsonify({
                "platforms": us['platforms'] if us['platforms'] else {"craigslist": True, "offerup": True,
                                                                      "mercari": True, "facebook": False},
                "zip_code": us['zip_code'],
                "distance": us['search_radius'],
                "check_interval": us['check_interval_minutes'],
                "thresholds": terms,
                "excluded_keywords": exclusions,
                "ai_detection": ai_show,
                "strictness": strict_map.get(us['ai_strictness'], 2),
                "subscription_status": sub_display,
                "is_pro": is_pro,
                "plan_tier": pt,
                "plan_name": _plan_display_name(pt),
                "max_search_terms": limits['max_search_terms'],
                "ai_image_allowed": ai_allowed,
                "subscription_current_period_end": us.get('subscription_current_period_end'),
                "subscription_cancel_at_period_end": bool(us.get('subscription_cancel_at_period_end')),
                "notifications": _normalize_notification_channels(us.get('notification_channels')),
                "contact_phone": str(us.get('contact_phone') or ''),
                "buyer_include_local": _user_settings_buyer_prefs(us)[0],
                "buyer_include_shipping": _user_settings_buyer_prefs(us)[1],
                "billing": _billing_config_response(),
            })

        elif request.method == 'POST':
            data = request.json
            cursor.execute("SELECT * FROM user_settings WHERE user_id = %s;", (user_id,))
            us_row = cursor.fetchone()
            limits = _effective_limits(us_row)
            thresholds_in = data.get('thresholds') or {}
            if len(thresholds_in) > limits['max_search_terms']:
                return jsonify({
                    "error": f"Your plan allows up to {limits['max_search_terms']} search terms.",
                }), 400

            strict_map = {1: 'lenient', 2: 'balanced', 3: 'strict'}
            strict_text = strict_map.get(data.get('strictness', 2), 'balanced')

            want_ai = bool(data.get('ai_detection', False))
            if not limits['ai_image_allowed']:
                want_ai = False

            prev_nc = None
            prev_phone = None
            if us_row:
                prev_nc = us_row.get('notification_channels')
                prev_phone = us_row.get('contact_phone')
            if 'notifications' in data:
                nc_json = _normalize_notification_channels(data.get('notifications'))
            else:
                nc_json = _normalize_notification_channels(prev_nc)
            try:
                if 'contact_phone' in data:
                    phone = _sanitize_contact_phone(data.get('contact_phone'))
                else:
                    phone = prev_phone
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

            plat = dict(data.get('platforms') or {})
            tier_eff = _effective_plan_tier(us_row) if us_row else 'inactive'
            if tier_eff != 'pro':
                plat['facebook'] = False
            try:
                check_iv = int(data.get('check_interval', 10))
            except (TypeError, ValueError):
                check_iv = 10
            if tier_eff == 'pro' and plat.get('facebook'):
                check_iv = max(check_iv, 30)

            try:
                bl_buy, bs_buy = _coerce_buyer_prefs_from_post(data, us_row)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

            # UPSERT Core Settings (buyer prefs require migration 006)
            cursor.execute("""
                INSERT INTO user_settings (
                    user_id, zip_code, search_radius, platforms, ai_enabled,
                    check_interval_minutes, ai_strictness, notification_channels, contact_phone,
                    buyer_include_local, buyer_include_shipping
                )
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    zip_code = EXCLUDED.zip_code, search_radius = EXCLUDED.search_radius, platforms = EXCLUDED.platforms,
                    ai_enabled = EXCLUDED.ai_enabled, check_interval_minutes = EXCLUDED.check_interval_minutes, ai_strictness = EXCLUDED.ai_strictness,
                    notification_channels = EXCLUDED.notification_channels, contact_phone = EXCLUDED.contact_phone,
                    buyer_include_local = EXCLUDED.buyer_include_local, buyer_include_shipping = EXCLUDED.buyer_include_shipping;
            """, (
            user_id, data.get('zip_code', '95212'), data.get('distance', 25), json.dumps(plat),
            want_ai, check_iv, strict_text,
            json.dumps(nc_json), phone, bl_buy, bs_buy))

            # REPLACE Search Terms
            # POST: Save both max and min to the database
            cursor.execute("DELETE FROM user_search_terms WHERE user_id = %s;", (user_id,))
            for term, prices in thresholds_in.items():
                cursor.execute(
                    "INSERT INTO user_search_terms (user_id, search_term, max_price, min_price) VALUES (%s, %s, %s, %s);",
                    (user_id, term, prices.get('max', 0), prices.get('min', 0))
                )

            # REPLACE Exclusions
            cursor.execute("DELETE FROM user_exclusions WHERE user_id = %s;", (user_id,))
            for keyword in data.get('excluded_keywords', []):
                cursor.execute("INSERT INTO user_exclusions (user_id, keyword) VALUES (%s, %s);", (user_id, keyword))

            conn.commit()
            out = dict(data)
            out['platforms'] = plat
            out['check_interval'] = check_iv
            out['ai_detection'] = want_ai
            out['notifications'] = nc_json
            out['contact_phone'] = phone or ''
            out['buyer_include_local'] = bl_buy
            out['buyer_include_shipping'] = bs_buy
            return jsonify({"success": True, "settings": out})

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/account-contact', methods=['POST', 'OPTIONS'])
@app.route('/account-contact', methods=['POST', 'OPTIONS'])
@require_auth
def update_account_contact(user_id):
    """Update only contact_phone (optional SMS number) without posting full scanner settings."""
    if request.method == 'OPTIONS':
        return '', 200
    data = request.get_json(silent=True) or {}
    try:
        phone = _sanitize_contact_phone(data.get('contact_phone'))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
    cursor = conn.cursor()
    try:
        default_platforms = json.dumps({"craigslist": True, "offerup": True, "mercari": True, "facebook": False})
        cursor.execute(
            """
            INSERT INTO user_settings (
                user_id, zip_code, search_radius, platforms, ai_enabled,
                check_interval_minutes, ai_strictness, contact_phone
            )
            VALUES (%s, '95212', 25, %s::jsonb, FALSE, 10, 'balanced', %s)
            ON CONFLICT (user_id) DO UPDATE SET contact_phone = EXCLUDED.contact_phone
            """,
            (user_id, default_platforms, phone),
        )
        conn.commit()
        return jsonify({"success": True, "contact_phone": phone or ""})
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
        cursor.execute(
            """
            UPDATE user_settings
            SET is_active = FALSE,
                last_scraped_at = NOW()
            WHERE user_id = %s;
            """,
            (user_id,)
        )
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
                sub = stripe.Subscription.retrieve(sub_id, expand=['items.data.price'])
                pe = sub.get('current_period_end')
                ca = bool(sub.get('cancel_at_period_end'))
                items = sub.get('items', {}).get('data') or []
                price_id = items[0].get('price', {}).get('id') if items else None
                tier = _stripe_price_to_tier(price_id)
                _upsert_pro_subscription(cursor, user_id, customer_id, sub_id, pe, ca, plan_tier=tier)
            conn.commit()

        elif etype == 'customer.subscription.updated':
            sub = event['data']['object']
            customer_id = sub.get('customer')
            status = sub.get('status') or ''
            pe = sub.get('current_period_end')
            ca = bool(sub.get('cancel_at_period_end'))
            entitled = _subscription_is_entitled(status)
            items = sub.get('items', {}).get('data') or []
            price_id = items[0].get('price', {}).get('id') if items else None
            tier = _stripe_price_to_tier(price_id) if entitled else 'inactive'
            is_pro = tier == 'pro'
            cursor.execute(
                "SELECT user_id FROM user_settings WHERE stripe_customer_id = %s LIMIT 1",
                (customer_id,)
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    """
                    UPDATE user_settings SET
                        is_pro = %s,
                        subscription_status = %s,
                        plan_tier = %s,
                        ai_enabled = %s,
                        subscription_current_period_end = %s,
                        subscription_cancel_at_period_end = %s,
                        stripe_subscription_id = COALESCE(%s, stripe_subscription_id)
                    WHERE user_id = %s
                    """,
                    (
                        is_pro,
                        'active' if entitled else 'inactive',
                        tier,
                        is_pro,
                        pe,
                        ca,
                        sub.get('id'),
                        row['user_id'],
                    ),
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
                        plan_tier = 'inactive',
                        ai_enabled = FALSE,
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


@app.route('/api/billing/config', methods=['GET', 'OPTIONS'])
@app.route('/billing/config', methods=['GET', 'OPTIONS'])
def billing_config():
    """Display prices + pre-beta flag for the dashboard (no auth)."""
    if request.method == 'OPTIONS':
        return '', 200
    return jsonify(_billing_config_response())


@app.route('/api/create-checkout-session', methods=['POST', 'OPTIONS'])
@app.route('/create-checkout-session', methods=['POST', 'OPTIONS'])
@require_auth
def create_checkout(user_id):
    if request.method == 'OPTIONS':
        return '', 200
    data = request.get_json(silent=True) or {}
    plan = (data.get('plan') or 'pro').lower()
    if plan == 'basic':
        price_id = _first_stripe_price_id_from_env('STRIPE_PRICE_BASIC_ID', 'STRIPE_PRICE_ID')
    else:
        price_id = _first_stripe_price_id_from_env('STRIPE_PRICE_PRO_ID', 'STRIPE_PRICE_ID')
    basic_direct = _first_stripe_price_id_from_env('STRIPE_PRICE_BASIC_ID')
    pro_direct = _first_stripe_price_id_from_env('STRIPE_PRICE_PRO_ID')
    legacy = _first_stripe_price_id_from_env('STRIPE_PRICE_ID')
    if legacy and not basic_direct and not pro_direct:
        print(
            "Stripe: STRIPE_PRICE_BASIC_ID and STRIPE_PRICE_PRO_ID are unset; "
            "every checkout uses STRIPE_PRICE_ID. Set both price ids so Basic vs Pro map to different Stripe prices.",
            flush=True,
        )
    if not price_id:
        return jsonify({
            "error": "Stripe price not configured. Set STRIPE_PRICE_BASIC_ID and STRIPE_PRICE_PRO_ID (or legacy STRIPE_PRICE_ID).",
        }), 500
    if not stripe.api_key:
        return jsonify({"error": "STRIPE_SECRET_KEY not configured"}), 500
    print(
        "[Stripe checkout] "
        f"plan={plan!r} resolved_price_id={price_id!r} "
        f"basic_explicit={basic_direct!r} pro_explicit={pro_direct!r} legacy_fallback={legacy!r}",
        flush=True,
    )
    base = _frontend_base_url(request.headers.get('Origin'))
    try:
        checkout_session = stripe.checkout.Session.create(
            mode='subscription',
            line_items=[{'price': price_id, 'quantity': 1}],
            client_reference_id=user_id,
            metadata={'user_id': user_id, 'plan': plan},
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
    sub_id = sub_raw if isinstance(sub_raw, str) else sub_raw.get('id')

    if not customer_id or not sub_id:
        return jsonify({"error": "Missing customer or subscription"}), 400

    sub = stripe.Subscription.retrieve(sub_id, expand=['items.data.price'])
    pe = sub.get('current_period_end')
    ca = bool(sub.get('cancel_at_period_end'))
    items = sub.get('items', {}).get('data') or []
    price_id = items[0].get('price', {}).get('id') if items else None
    tier = _stripe_price_to_tier(price_id)

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cursor = conn.cursor()
        _upsert_pro_subscription(cursor, user_id, customer_id, sub_id, pe, ca, plan_tier=tier)
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
        base = _frontend_base_url(request.headers.get('Origin'))
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
    q = (request.args.get('q') or '').strip()
    try:
        platform_list = _parse_platform_filters(request)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    sort = (request.args.get('sort') or 'newest').strip().lower()
    if sort not in ('newest', 'oldest', 'saved_newest', 'saved_oldest'):
        return jsonify({"error": "invalid sort"}), 400
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            where_clauses = ["user_id = %s"]
            params = [user_id]
            if q:
                where_clauses.append("LOWER(title) LIKE %s")
                params.append(f"%{q.lower()}%")
            if platform_list:
                where_clauses.append("LOWER(TRIM(platform)) = ANY(%s)")
                params.append(platform_list)
            where_sql = " AND ".join(where_clauses)
            if sort == 'oldest':
                order_sql = "listed_at ASC NULLS LAST, created_at ASC"
            elif sort == 'saved_newest':
                order_sql = "created_at DESC"
            elif sort == 'saved_oldest':
                order_sql = "created_at ASC"
            else:
                order_sql = "listed_at DESC NULLS LAST, created_at DESC"
            cursor.execute(
                f"""
                SELECT title, price, link, platform, image_url, location, created_at, listed_at
                FROM listings
                WHERE {where_sql}
                ORDER BY {order_sql}
                LIMIT %s OFFSET %s
                """,
                tuple(params + [limit, offset]),
            )
        except psycopg2.ProgrammingError as e:
            conn.rollback()
            if e.pgcode != errorcodes.UNDEFINED_COLUMN and 'listed_at' not in str(e):
                raise
            where_clauses = ["user_id = %s"]
            params = [user_id]
            if q:
                where_clauses.append("LOWER(title) LIKE %s")
                params.append(f"%{q.lower()}%")
            if platform_list:
                where_clauses.append("LOWER(TRIM(platform)) = ANY(%s)")
                params.append(platform_list)
            where_sql = " AND ".join(where_clauses)
            if sort in ('oldest', 'saved_oldest'):
                order_sql = "created_at ASC NULLS LAST"
            else:
                order_sql = "created_at DESC NULLS LAST"
            cursor.execute(
                f"""
                SELECT title, price, link, platform, image_url, location, created_at
                FROM listings
                WHERE {where_sql}
                ORDER BY {order_sql}
                LIMIT %s OFFSET %s
                """,
                tuple(params + [limit, offset]),
            )
        rows = cursor.fetchall()
        where_clauses = ["user_id = %s"]
        count_params = [user_id]
        if q:
            where_clauses.append("LOWER(title) LIKE %s")
            count_params.append(f"%{q.lower()}%")
        if platform_list:
            where_clauses.append("LOWER(TRIM(platform)) = ANY(%s)")
            count_params.append(platform_list)
        cursor.execute(
            f"SELECT COUNT(*) AS c FROM listings WHERE {' AND '.join(where_clauses)}",
            tuple(count_params),
        )
        total = cursor.fetchone()['c']
        out = []
        for r in rows:
            created = r['created_at']
            if hasattr(created, 'isoformat'):
                created = created.isoformat()
            listed = r.get('listed_at')
            if listed is not None and hasattr(listed, 'isoformat'):
                listed = listed.isoformat()
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
                "listed_at": listed,
            })
        return jsonify({"listings": out, "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        print(f"listings: {e}", flush=True)
        return jsonify(error=str(e)), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/request-password-reset-code', methods=['POST', 'OPTIONS'])
@app.route('/request-password-reset-code', methods=['POST', 'OPTIONS'])
def request_password_reset_code():
    if request.method == 'OPTIONS':
        return '', 200
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email:
        return jsonify({"success": False, "error": "Valid email is required"}), 400
    code = f"{secrets.randbelow(1000000):06d}"
    code_hash = hashlib.sha256(code.encode('utf-8')).hexdigest()
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database error"}), 500
    cursor = None
    try:
        cursor = conn.cursor()
        _ensure_password_reset_table(cursor)
        cursor.execute(
            """
            INSERT INTO password_reset_requests (email, code_hash, expires_at, created_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (email) DO UPDATE SET
                code_hash = EXCLUDED.code_hash,
                expires_at = EXCLUDED.expires_at,
                created_at = NOW()
            """,
            (email, code_hash, expires_at)
        )
        _send_password_reset_code_email(email, code)
        conn.commit()
        # Always return success-like response to avoid user enumeration.
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        msg = str(e)
        if "Mailgun send failed" in msg:
            return jsonify({"success": False, "error": f"{msg}. Verify Mailgun configuration."}), 400
        return jsonify({"success": False, "error": msg}), 500
    finally:
        if cursor:
            cursor.close()
        conn.close()


@app.route('/api/reset-password-with-code', methods=['POST', 'OPTIONS'])
@app.route('/reset-password-with-code', methods=['POST', 'OPTIONS'])
def reset_password_with_code():
    if request.method == 'OPTIONS':
        return '', 200
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    code = (data.get('code') or '').strip()
    new_password = (data.get('new_password') or '').strip()
    if not email or not code or not new_password:
        return jsonify({"success": False, "error": "Email, code, and new password are required"}), 400
    if len(new_password) < 8:
        return jsonify({"success": False, "error": "Password must be at least 8 characters"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database error"}), 500
    cursor = None
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        _ensure_password_reset_table(cursor)
        cursor.execute(
            """
            SELECT email, code_hash, expires_at, (expires_at > NOW()) AS is_valid
            FROM password_reset_requests
            WHERE email = %s
            LIMIT 1
            """,
            (email,)
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"success": False, "error": "No reset request found"}), 400
        if not row.get('is_valid'):
            return jsonify({"success": False, "error": "Verification code expired"}), 400
        code_hash = hashlib.sha256(code.encode('utf-8')).hexdigest()
        if row['code_hash'] != code_hash:
            return jsonify({"success": False, "error": "Invalid verification code"}), 400

        user = _admin_lookup_supabase_user_by_email(email)
        if not user:
            # Avoid account enumeration.
            cursor.execute("DELETE FROM password_reset_requests WHERE email = %s", (email,))
            conn.commit()
            return jsonify({"success": True})

        admin_url = f"{SUPABASE_URL}/auth/v1/admin/users/{user.get('id')}"
        admin_headers = {
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Content-Type": "application/json"
        }
        resp = requests.put(
            admin_url,
            headers=admin_headers,
            json={"password": new_password},
            timeout=20,
        )
        if resp.status_code not in (200, 201):
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            msg = payload.get('msg') or payload.get('error_description') or payload.get('message') or 'Password reset failed'
            return jsonify({"success": False, "error": msg}), resp.status_code

        cursor.execute("DELETE FROM password_reset_requests WHERE email = %s", (email,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if cursor:
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
    if reason not in ('sold', 'not_a_deal', 'false_positive', 'just_remove'):
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


@app.route('/api/listings/clear-all', methods=['POST', 'OPTIONS'])
@app.route('/listings/clear-all', methods=['POST', 'OPTIONS'])
@require_auth
def clear_all_scraped_listings(user_id):
    if request.method == 'OPTIONS':
        return '', 200
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM listings WHERE user_id = %s", (user_id,))
        n = cursor.rowcount
        conn.commit()
        return jsonify({"success": True, "deleted": int(n)})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/push/vapid-public-key', methods=['GET', 'OPTIONS'])
@app.route('/push/vapid-public-key', methods=['GET', 'OPTIONS'])
def push_vapid_public_key():
    """Return VAPID public key for frontend to subscribe to push notifications."""
    if request.method == 'OPTIONS':
        return '', 200
    if not VAPID_PUBLIC_KEY:
        return jsonify({"error": "Push notifications not configured"}), 503
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route('/api/push/subscribe', methods=['POST', 'OPTIONS'])
@app.route('/push/subscribe', methods=['POST', 'OPTIONS'])
@require_auth
def push_subscribe(user_id):
    """Save push subscription for the authenticated user."""
    if request.method == 'OPTIONS':
        return '', 200
    data = request.json
    subscription = data.get('subscription')
    if not subscription:
        return jsonify({"error": "No subscription provided"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE user_settings
            SET push_subscription = %s::jsonb
            WHERE user_id = %s
            """,
            (json.dumps(subscription), user_id)
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/push/unsubscribe', methods=['POST', 'OPTIONS'])
@app.route('/push/unsubscribe', methods=['POST', 'OPTIONS'])
@require_auth
def push_unsubscribe(user_id):
    """Remove push subscription for the authenticated user."""
    if request.method == 'OPTIONS':
        return '', 200
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE user_settings
            SET push_subscription = NULL
            WHERE user_id = %s
            """,
            (user_id,)
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/health', methods=['GET', 'OPTIONS'])
@app.route('/health', methods=['GET', 'OPTIONS'])
def health_check():
    """
    Health check endpoint for Render/downtime monitoring.
    Returns DB connectivity and scraper cycle timestamp (if available).
    """
    if request.method == 'OPTIONS':
        return '', 200

    status = {"status": "ok", "checks": {}}
    code = 200

    # DB check
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            status["checks"]["database"] = "ok"
        except Exception as e:
            status["checks"]["database"] = f"error: {e}"
            status["status"] = "degraded"
            code = 503
        finally:
            conn.close()
    else:
        status["checks"]["database"] = "unreachable"
        status["status"] = "error"
        code = 503

    # Scraper cycle check
    global _health_last_scraper_cycle
    if _health_last_scraper_cycle:
        age_sec = time.time() - _health_last_scraper_cycle
        status["checks"]["scraper_last_cycle_age_seconds"] = round(age_sec, 1)
        status["checks"]["scraper_last_cycle_at"] = datetime.fromtimestamp(
            _health_last_scraper_cycle, tz=timezone.utc
        ).isoformat()
    else:
        status["checks"]["scraper_last_cycle"] = "not yet recorded"

    return jsonify(status), code


if __name__ == '__main__':
    # Start scraper thread
    scraper_thread = threading.Thread(target=start_background_scraper, daemon=True)
    scraper_thread.start()

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)