from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import threading
import time
import os
import shutil
import json
import secrets
import hashlib
import hmac
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
    # is_user_scraping() applies the staleness guard; reading SCRAPING_USERS
    # directly would report a hung scrape as still running forever.
    from scraper_multi_user import (SCRAPING_USERS, is_user_scraping, set_user_scraping,
                                    coalesce_window_sec)
except Exception:
    SCRAPING_USERS = {}
    def is_user_scraping(_uid):
        return False
    def set_user_scraping(_uid, _active):
        pass
    def coalesce_window_sec():
        return 120

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
    browser Origin (e.g. http://localhost:3001) so cancel links match the port
    you use.

    The final fallback is the PRODUCTION dashboard, not localhost. It used to be
    http://localhost:3000, which meant an unset FRONTEND_URL on the server sent
    real customers to a dead address — and because success_url and cancel_url
    share this base, a paying customer was charged, redirected nowhere, and
    never hit /api/complete-checkout. A forgotten env var must degrade to a
    working link, never to localhost.
    """
    explicit = (os.getenv('FRONTEND_URL') or '').strip().rstrip('/')
    if explicit:
        return explicit
    oh = (origin_header or '').strip().rstrip('/')
    if oh.startswith('http://localhost:') or oh.startswith('http://127.0.0.1:'):
        return oh
    return 'https://dashboard.pixelflip.app'


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
        return {'max_search_terms': 10, 'ai_image_allowed': True, 'max_priority_terms': 3}
    if t == 'basic':
        return {'max_search_terms': 3, 'ai_image_allowed': False, 'max_priority_terms': 0}
    try:
        # Pre-beta / trial: allow a few terms before checkout; set to 0 in production if you require payment first.
        inactive_cap = int(os.getenv('MAX_SEARCH_TERMS_INACTIVE', '3'))
    except ValueError:
        inactive_cap = 3
    return {'max_search_terms': max(0, inactive_cap), 'ai_image_allowed': False,
            'max_priority_terms': 0}


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
    op = us.get('max_priority_terms_override')
    if op is not None:
        try:
            lim['max_priority_terms'] = max(0, int(op))
        except (TypeError, ValueError):
            pass
    return lim


# Scan cadence is a plan feature. Facebook no longer carries its own floor —
# the no-login Playwright path has no per-scrape cost, so it follows the same
# tier cadence as the other marketplaces. Keep in sync with the identical
# tables in scraper_multi_user.py.
PLAN_INTERVAL_FLOOR_MINUTES = {'pro': 5, 'basic': 10}
PLAN_INTERVAL_OPTIONS = {
    'pro': [5, 10, 15, 30, 60],
    'basic': [10, 15, 30, 60],
}
DEFAULT_INTERVAL_FLOOR_MINUTES = 10

# Priority terms: a Pro user picks up to 3 terms that may run at the 5-minute
# floor. Every other term, on every tier, sits at 10. Basic sees the control as
# an upsell but cannot enable it, so PLAN_MAX_PRIORITY_TERMS is 0 there.
#
# PLAN_STANDARD_FLOOR_MINUTES was 15 for Pro, which made the paid plan's default
# cadence SLOWER than Basic's 10. It is 10 everywhere now, and the only thing
# priority buys is the right to put 5 on up to three terms.
#
# Still a bandwidth lever, though a weaker one than at 15: 7 of 10 terms at 144
# scans/day instead of 288 is 1,872 rather than 2,880 scans/day per Pro account.
# Against the 2026-08-11 measurement of 110.6KB/term and $9.11/mo at full rate,
# that is ~$5.92/mo — derived from that measurement by arithmetic, not
# re-measured. It was $4.86 when the standard floor was 15.
#
# Keep in sync with the identical tables in scraper_multi_user.py AND with
# frontend/src/App.js.
PLAN_MAX_PRIORITY_TERMS = {'pro': 3, 'basic': 0}
PLAN_STANDARD_FLOOR_MINUTES = {'pro': 10}
DEFAULT_MAX_PRIORITY_TERMS = 0

# Cadence is per TERM now (user_search_terms.interval_minutes, migration 012).
# PLAN_INTERVAL_OPTIONS above is the per-term allowlist, and this is what a term
# gets when the client sends nothing.
DEFAULT_TERM_INTERVAL_MINUTES = 10

# Include keywords per term. Bounded because they are free user text checked
# against every title on every platform, and because a runaway paste should not
# become a permanent per-listing cost for everyone.
try:
    MAX_TERM_INCLUDES = max(1, int(os.getenv('MAX_TERM_INCLUDES', '20')))
except ValueError:
    MAX_TERM_INCLUDES = 20


def _term_interval_options_for_tier(tier):
    """The rates a single term may be set to on this tier."""
    return PLAN_INTERVAL_OPTIONS.get((tier or '').strip().lower(),
                                     PLAN_INTERVAL_OPTIONS['basic'])


def _sanitize_term_interval(value, tier):
    """
    Coerce a client-supplied per-term interval onto the tier's allowlist.

    Anything off-menu — junk, a missing field, or a crafted 5 on a tier that
    does not sell it — falls back to the default rather than erroring, so a
    stale or hostile client cannot lock a user out of saving their settings.
    Whether the user may have this MANY fast terms is a separate question,
    counted by the caller.
    """
    allowed = _term_interval_options_for_tier(tier)
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TERM_INTERVAL_MINUTES
    return v if v in allowed else DEFAULT_TERM_INTERVAL_MINUTES


def _term_due_map(conn, user_id, tier):
    """
    When each of this user's terms next comes due, as {term: epoch}.

    Shared by /api/status (the countdown) and /api/start (the armed message), so
    the two can never disagree about when the next scan is. They did once: START
    promised "shortly" while the countdown showed 25 minutes, and the resulting
    "the scanner is broken" hunt cost most of a session.

    Owns its cursor because callers hold different kinds — /api/start uses tuple
    rows, /api/status dict rows — and this needs names.

    Mirrors term_interval_minutes() in scraper_multi_user.py: the user's chosen
    rate, floored by what their plan allows. A countdown that promises a rate the
    scheduler will not honour is worse than no countdown.
    """
    has_fast = _max_priority_terms_for_tier(tier) > 0
    floor_min = (_interval_floor_for_tier(tier) if has_fast
                 else _standard_floor_for_tier(tier))
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "SELECT search_term, interval_minutes, "
            "EXTRACT(EPOCH FROM last_scraped_at) AS last_ts "
            "FROM user_search_terms WHERE user_id = %s;", (user_id,))
        out = {}
        for row in cur.fetchall():
            every = (int(row['interval_minutes'])
                     if row['interval_minutes'] is not None
                     else DEFAULT_TERM_INTERVAL_MINUTES)
            every = max(every, floor_min)
            # NULL last_scraped_at means never scanned, which is due now.
            last = float(row['last_ts']) if row['last_ts'] is not None else 0.0
            out[row['search_term']] = last + every * 60
        return out
    finally:
        cur.close()


def _resolve_term_intervals(thresholds_in, tier, max_priority, prev_interval=None):
    """
    Decide every term's stored interval_minutes for a settings save.

    Returns (intervals, demoted_count). Enforces both halves of the rule
    server-side, because the UI cannot be trusted to: which rates this tier may
    use at all, and how many terms may sit at its fastest one. Going over the
    cap demotes to the default rather than erroring, so a stale or hostile
    client cannot lock a user out of saving their settings.

    Terms ALREADY at the fastest rate outrank newly-raised ones, so setting a
    fourth term to 5 simply does not take — it never silently demotes a term the
    user chose earlier, which picking by name would.
    """
    prev_interval = prev_interval or {}
    max_priority = max(0, int(max_priority or 0))
    fastest_rate = _interval_floor_for_tier(tier)
    tier_has_fast = max_priority > 0

    out = {}
    for term, prices in (thresholds_in or {}).items():
        prices = prices or {}
        if 'interval' in prices:
            out[term] = _sanitize_term_interval(prices.get('interval'), tier)
        else:
            # A client still sending only the old boolean. Map it, so a
            # half-deployed frontend keeps working instead of silently resetting
            # every term to the default.
            out[term] = (fastest_rate
                         if (tier_has_fast and bool(prices.get('priority')))
                         else DEFAULT_TERM_INTERVAL_MINUTES)

    demoted = 0
    if tier_has_fast:
        wants_fast = sorted(t for t, v in out.items() if v <= fastest_rate)
        if len(wants_fast) > max_priority:
            already = [t for t in wants_fast if prev_interval.get(t) == fastest_rate]
            newly = [t for t in wants_fast if t not in already]
            granted = set((already + newly)[:max_priority])
            for t in wants_fast:
                if t not in granted:
                    out[t] = DEFAULT_TERM_INTERVAL_MINUTES
                    demoted += 1
    return out, demoted


def _max_priority_terms_for_tier(tier):
    return PLAN_MAX_PRIORITY_TERMS.get(
        (tier or '').strip().lower(), DEFAULT_MAX_PRIORITY_TERMS
    )


def _standard_floor_for_tier(tier):
    """
    Floor for a NON-priority term.

    Tiers with no priority feature fall back to their normal floor, so nothing
    changes for them — a Basic user's terms are all 'standard' but must not
    silently slow to 15 minutes.
    """
    t = (tier or '').strip().lower()
    return PLAN_STANDARD_FLOOR_MINUTES.get(t, _interval_floor_for_tier(t))


def _interval_floor_for_tier(tier):
    return PLAN_INTERVAL_FLOOR_MINUTES.get(
        (tier or '').strip().lower(), DEFAULT_INTERVAL_FLOOR_MINUTES
    )


def _effective_check_interval_minutes(us):
    """Clamp the user's chosen interval up to their plan's floor."""
    if not us:
        return DEFAULT_INTERVAL_FLOOR_MINUTES
    try:
        stored = int(us.get('check_interval_minutes') or DEFAULT_INTERVAL_FLOOR_MINUTES)
    except (TypeError, ValueError):
        stored = DEFAULT_INTERVAL_FLOOR_MINUTES
    return max(stored, _interval_floor_for_tier(_effective_plan_tier(us)))


def _plan_display_name(tier):
    t = (tier or 'inactive').lower()
    if t == 'basic':
        return 'Basic Scanner'
    if t == 'pro':
        return 'Pro Scanner'
    return None


# Email categories a recipient can opt out of individually. Transactional mail
# (receipts, password resets, billing failures) is deliberately NOT here: it is
# exempt from unsubscribe rules and must keep sending, or users miss things like
# a failed payment that costs them their account.
EMAIL_CATEGORIES = {
    'listing_alerts': True,    # the new-match digest — the core product
    'product_updates': True,   # new features, changed behaviour
    'marketing': True,         # tips, offers, promotions
}


def _normalize_notification_channels(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    out = {
        'email': bool(raw.get('email', True)),
        'sms': bool(raw.get('sms', False)),
        'push': bool(raw.get('push', False)),
    }
    for key, default in EMAIL_CATEGORIES.items():
        out[key] = bool(raw.get(key, default))
    return out


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
    "http://127.0.0.1:3001",           # Local Dev (Alternative)
    # Private LAN origins, so the dashboard can be opened on a phone during
    # development (http://192.168.x.x:3000). Without these the browser blocks
    # every API response and the app hangs on "verifying clearance".
    # Safe to leave enabled: auth is a bearer token, not a cookie, and
    # supports_credentials is False — CORS is not the security boundary here.
    # These addresses are unroutable from the public internet anyway.
    re.compile(r"^http://192\.168\.\d{1,3}\.\d{1,3}:\d+$"),
    re.compile(r"^http://10\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+$"),
    re.compile(r"^http://172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}:\d+$"),
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
        # Pooled: /api/status is polled every 2s per open dashboard, and a fresh
        # TLS handshake per poll both wasted ~287ms and burned a pooler slot.
        # The returned object proxies a real connection; .close() hands it back.
        from db_pool import get_pooled_connection
        conn = get_pooled_connection()
        try:
            from db_schema import (
                ensure_buyer_delivery_columns,
                ensure_push_subscription_column,
                ensure_per_term_exclusion_columns,
                ensure_tour_column,
                ensure_listing_uniqueness_per_user,
                ensure_priority_term_columns,
                ensure_term_interval_column,
                ensure_scraper_state_table,
                ensure_term_include_table,
            )
            ensure_buyer_delivery_columns(conn)
            ensure_push_subscription_column(conn)
            ensure_per_term_exclusion_columns(conn)
            ensure_tour_column(conn)
            ensure_listing_uniqueness_per_user(conn)
            ensure_priority_term_columns(conn)
            ensure_term_interval_column(conn)
            ensure_scraper_state_table(conn)
            ensure_term_include_table(conn)
        except Exception as schema_err:
            print(f"Schema ensure warning: {schema_err}", flush=True)
        return conn
    except Exception as e:
        print(f"Database connection error: {e}", flush=True)
        return None



# Verified-token cache.
#
# require_auth asked Supabase to verify on EVERY request, and /api/status polls
# every 2 seconds per open dashboard — 30 auth round-trips a minute per user
# before any real work happens. Each adds its own latency to the request and
# counts against Supabase's auth rate limits, so N friends with the dashboard
# open is N x 30/min spent purely re-answering the same question.
#
# The cost is that a signed-out token keeps working for up to the TTL. A minute
# is the usual trade and is far shorter than the token's own lifetime.
#
# The proper fix is verifying the JWT locally with SUPABASE_JWT_SECRET, which
# needs no network at all — deliberately not done here because it means adding a
# JWT library and getting algorithm/audience checks right, which is a bigger
# change than this problem warrants today.
_AUTH_CACHE = {}
_AUTH_CACHE_LOCK = threading.Lock()


def _auth_cache_ttl():
    try:
        return max(0, int(os.getenv('AUTH_CACHE_TTL_SEC', '60')))
    except ValueError:
        return 60


def _auth_cache_key(token):
    """Hashed, so a memory dump or log of this dict is not a pile of credentials."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _auth_cache_get(token):
    if _auth_cache_ttl() <= 0:
        return None
    key = _auth_cache_key(token)
    now = time.time()
    with _AUTH_CACHE_LOCK:
        hit = _AUTH_CACHE.get(key)
        if hit and hit[1] > now:
            return hit[0]
        if hit:
            _AUTH_CACHE.pop(key, None)
    return None


def _auth_cache_put(token, user_id):
    ttl = _auth_cache_ttl()
    if ttl <= 0:
        return
    now = time.time()
    with _AUTH_CACHE_LOCK:
        # Bounded. An unbounded dict keyed on tokens is a slow memory leak, and
        # anyone can mint new tokens by logging in repeatedly.
        if len(_AUTH_CACHE) >= 500:
            for k, v in list(_AUTH_CACHE.items()):
                if v[1] <= now:
                    _AUTH_CACHE.pop(k, None)
            if len(_AUTH_CACHE) >= 1000:
                _AUTH_CACHE.clear()
        _AUTH_CACHE[_auth_cache_key(token)] = (user_id, now + ttl)


def _invalidate_auth_cache(token):
    with _AUTH_CACHE_LOCK:
        _AUTH_CACHE.pop(_auth_cache_key(token), None)


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Allow browser preflight checks to pass without a token
        if request.method == 'OPTIONS':
            return '', 200

        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"error": "Missing token"}), 401

        # split(" ")[1] raised IndexError on a bare "Bearer", which the handler
        # below turned into a 500. A malformed header is a 401.
        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return jsonify({"error": "Missing token"}), 401

        cached_user = _auth_cache_get(token)
        if cached_user:
            return f(cached_user, *args, **kwargs)

        try:
            # Ask Supabase directly if the token is valid
            verify_url = f"{SUPABASE_URL}/auth/v1/user"
            response = requests.get(
                verify_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": SUPABASE_ANON_KEY
                },
                # Without this a hung Supabase connection pins the request
                # forever. Flask's threaded server has a finite worker pool, so
                # enough of them takes the whole API down — including /health,
                # which is what a monitor would be watching.
                timeout=float(os.getenv('AUTH_VERIFY_TIMEOUT_SEC', '10')),
            )

            if response.status_code != 200:
                print(f"🔒 Supabase Auth Rejected: {response.text}", flush=True)
                return jsonify({"error": "Invalid or expired token"}), 401

            user_data = response.json()
            user_id = user_data.get('id')

            if not user_id:
                return jsonify({"error": "User ID not found in token"}), 401

            _auth_cache_put(token, user_id)

        except requests.RequestException as e:
            # Supabase unreachable is OUR problem, not a bad token. Returning
            # 401 here would sign every user out on a network blip, because the
            # dashboard treats 401 as "session expired".
            print(f"🔒 Auth verification unreachable: {type(e).__name__}: {e}", flush=True)
            return jsonify({"error": "Auth service unavailable, retrying"}), 503
        except Exception as e:
            # Do not echo the exception to the client; it can carry internals.
            print(f"🔒 Auth Server Error: {type(e).__name__}: {e}", flush=True)
            return jsonify({"error": "Server auth error"}), 500

        return f(user_id, *args, **kwargs)

    return decorated


# ==========================================
# IN-MEMORY LOG BUFFER (User-Specific)
# ==========================================
# Looks like: { "user_id_123": [{"ts": 1712345678.9, "time": "…", "message": "…", "type": "info"}] }
user_logs = {}


try:
    # 50 was far too small: one scan that matches 30 listings logs a line each,
    # which evicts the platform errors printed earlier in the same scan — the
    # single most useful thing in the console. At ~150 bytes a line, 500 entries
    # is well under a megabyte per active user. /api/status serves this
    # incrementally (see `since`), so a bigger buffer costs no extra bandwidth.
    MAX_USER_LOGS = max(50, int(os.getenv('MAX_USER_LOGS', '500')))
except ValueError:
    MAX_USER_LOGS = 500


def add_log(user_id, message, log_type="info"):
    """
    Record a console line for this user.

    Writes go to the scrape_logs table, not a process dict, so the API can serve
    logs produced by a scraper running in a DIFFERENT process. That is what
    makes the worker/API split possible — and it also removes the failure mode
    where two backend processes each kept their own buffer and the dashboard
    read whichever one happened to win the port.

    Batched by scrape_logs, so this stays cheap despite being per-line.
    """
    import scrape_logs
    scrape_logs.add_log(user_id, message, log_type)


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
                # Console lines are now rows, so they need retention too or the
                # table grows without bound.
                try:
                    import scrape_logs
                    purged = scrape_logs.purge_old()
                    if purged:
                        print(f"🧹 Cleanup removed {purged} old log lines", flush=True)
                except Exception as log_err:
                    print(f"log purge warning: {log_err}", flush=True)
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

def _env_flag_true(name):
    return (os.getenv(name, '') or '').strip().lower() in ('1', 'true', 'yes', 'on')


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
        "scrape_provider": "playwright",
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

        # Every term carries its own cadence now, so a single user-level
        # countdown would tick to zero and then sit there while nothing
        # happened. The scan the user is waiting for is the SOONEST term, so the
        # countdown targets that — and `next_check_terms` says how many terms
        # that scan will actually cover, which is what makes one timer honest
        # instead of ambiguous. This runs for every tier, not just the ones with
        # a fast tier, because mixed cadences are now the normal case.
        next_check_terms = None
        next_check_scope = None
        try:
            due_at = _term_due_map(conn, user_id, _effective_plan_tier(us))
            if due_at:
                soonest = min(due_at.values())
                next_check_timestamp = soonest
                # Terms due within the coalescing window ride along in the same
                # scan. Imported rather than re-guessed: this used to assume a
                # 5-second window while the scheduler used none, so the UI
                # promised groupings that never happened.
                next_check_terms = sum(1 for r in due_at.values()
                                       if r <= soonest + coalesce_window_sec())
                next_check_scope = ('all terms'
                                    if next_check_terms >= len(due_at)
                                    else f'{next_check_terms} of {len(due_at)} terms')
        except Exception as e:
            # A countdown is not worth failing /api/status over.
            print(f"[status] per-term countdown failed for {user_id[:8]}: {e}", flush=True)

        current_now = time.time()
        scraping_in_progress = is_user_scraping(user_id)

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

        # Console log delivery. This endpoint is polled every 2s, so shipping the
        # whole buffer each time would waste real bandwidth now that it holds
        # hundreds of lines. A client that passes `since` (the newest ts it
        # already has) gets only what is new; omitting it returns a full
        # snapshot, which is what a fresh page load wants.
        import scrape_logs
        activity_partial = False
        since_ts = None
        since_raw = request.args.get('since')
        if since_raw:
            try:
                since_ts = float(since_raw)
                activity_partial = True
            except (TypeError, ValueError):
                since_ts = None  # malformed cursor: fall back to a full snapshot
        activity = scrape_logs.get_logs(user_id, since_ts=since_ts, limit=MAX_USER_LOGS)

        return jsonify({
            "status": "running" if is_running else "stopped",
            "running": is_running,
            "subscription_status": "active" if subscription_entitled else "inactive",
            "listings_count": listings_count,
            "last_scrape_duration_ms": us.get('last_scrape_duration_ms') or 0,
            "items_scanned_today": 0,
            "matches_found_today": 0,
            "next_check_timestamp": next_check_timestamp_out,
            # What the next scan will cover, so one timer can say "3 priority"
            # vs "all terms" instead of leaving the user guessing. None for tiers
            # without priority terms, where every scan covers everything.
            "next_check_terms": next_check_terms,
            "next_check_scope": next_check_scope,
            "scraping_in_progress": scraping_in_progress,
            "recent_activity": activity,
            # True when `activity` is a delta the client should APPEND. False
            # means it is a full snapshot to replace with.
            "activity_partial": activity_partial,
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

            # NULL min/max means "any price" — an unset bound, not zero. Keep it
            # as None all the way to the client so the input renders empty.
            cursor.execute("SELECT search_term, max_price, min_price, is_priority, "
                           "interval_minutes "
                           "FROM user_search_terms WHERE user_id = %s;",
                           (user_id,))
            terms = {}
            for row in cursor.fetchall():
                every = (int(row['interval_minutes'])
                         if row.get('interval_minutes') is not None
                         else DEFAULT_TERM_INTERVAL_MINUTES)
                terms[row['search_term']] = {
                    'max': float(row['max_price']) if row['max_price'] is not None else None,
                    'min': float(row['min_price']) if row['min_price'] is not None else None,
                    'exclusions': [],
                    # OR'd positive filter. Empty means "no filter".
                    'includes': [],
                    'interval': every,
                    # Derived from the interval rather than read from the column,
                    # so a frontend still on the boolean renders correctly
                    # against the new backend.
                    'priority': every == 5,
                }

            # Exclusions are per search term. Rows with a NULL search_term are
            # pre-migration leftovers and are ignored (and cleared on next save).
            cursor.execute(
                "SELECT keyword, search_term FROM user_exclusions "
                "WHERE user_id = %s AND search_term IS NOT NULL;", (user_id,)
            )
            exclusions = []
            for row in cursor.fetchall():
                term = row.get('search_term')
                if term in terms:
                    terms[term]['exclusions'].append(row['keyword'])

            # Include keywords (migration 014). Tolerated missing so a partially
            # migrated database still serves settings instead of 500-ing.
            try:
                cursor.execute(
                    "SELECT keyword, search_term FROM user_includes "
                    "WHERE user_id = %s AND search_term IS NOT NULL;", (user_id,)
                )
                for row in cursor.fetchall():
                    term = row.get('search_term')
                    if term in terms:
                        terms[term]['includes'].append(row['keyword'])
            except Exception as e:
                conn.rollback()
                print(f"[settings] include keywords unavailable: {e}", flush=True)

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
                    "max_priority_terms": _plan_limits('inactive')['max_priority_terms'],
                    # Served rather than duplicated client-side: the option table
                    # lived in three files and drifting one silently broke the
                    # others. The frontend keeps a fallback for an offline load.
                    "term_interval_options": _term_interval_options_for_tier('inactive'),
                    "fastest_term_interval": _interval_floor_for_tier('inactive'),
                    "default_term_interval": DEFAULT_TERM_INTERVAL_MINUTES,
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
                "max_priority_terms": limits['max_priority_terms'],
                # Served rather than duplicated client-side: the option table
                # lived in three files and drifting one silently broke the
                # others. The frontend keeps a fallback for an offline load.
                "term_interval_options": _term_interval_options_for_tier(pt),
                "fastest_term_interval": _interval_floor_for_tier(pt),
                "default_term_interval": DEFAULT_TERM_INTERVAL_MINUTES,
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
                check_iv = int(data.get('check_interval', DEFAULT_INTERVAL_FLOOR_MINUTES))
            except (TypeError, ValueError):
                check_iv = DEFAULT_INTERVAL_FLOOR_MINUTES
            # Enforce the plan floor server-side: the dropdown already limits the
            # options, but the API must not trust the client to respect them.
            check_iv = max(check_iv, _interval_floor_for_tier(tier_eff))

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
            #
            # The rows are deleted and rewritten, which would also discard each
            # term's last_scraped_at and restart every countdown on any settings
            # save. Capture them first and carry them across for terms that
            # survive — otherwise a user editing a price re-triggers a full scan
            # of all their terms, and an autosaving dashboard makes that
            # constant.
            cursor.execute(
                "SELECT search_term, last_scraped_at, interval_minutes "
                "FROM user_search_terms WHERE user_id = %s;",
                (user_id,),
            )
            prev_scraped = {}
            prev_interval = {}
            for r in cursor.fetchall():
                prev_scraped[r['search_term']] = r['last_scraped_at']
                prev_interval[r['search_term']] = r['interval_minutes']

            cursor.execute("DELETE FROM user_search_terms WHERE user_id = %s;", (user_id,))

            def _price_or_none(v):
                """'' / None / non-numeric all mean 'no bound', stored as NULL."""
                if v is None or v == '':
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            # Cadence is per term now, and BOTH halves of the rule are enforced
            # HERE rather than in the UI: which rates a term may use, and how
            # many terms may sit at the plan's fastest one. A crafted POST would
            # otherwise buy a Basic account ten 5-minute terms. Going over the
            # cap demotes rather than erroring, so a stale client cannot lock a
            # user out of saving their settings.
            max_priority = max(0, int(limits.get('max_priority_terms', 0)))
            fastest_rate = _interval_floor_for_tier(tier_eff)
            tier_has_fast = max_priority > 0
            requested_interval, demoted = _resolve_term_intervals(
                thresholds_in, tier_eff, max_priority, prev_interval)
            if demoted:
                print(f"[settings] {user_id[:8]} asked for {demoted} term(s) over the "
                      f"{fastest_rate}m cap of {max_priority} — demoted to "
                      f"{DEFAULT_TERM_INTERVAL_MINUTES}m", flush=True)

            # A brand-new term has no last_scraped_at, which makes it due at once:
            # it fires its own scrape seconds after being added and then sits
            # permanently out of phase with everything else, so each term ends up
            # paying its own full browser startup. Give a new term a synthetic
            # stamp that lands it on the user's NEXT group scan instead.
            #
            # A user with no existing terms still scans immediately, which is the
            # right first-run behaviour — there is no group to join yet.
            soonest_due = None
            for t, stamped in prev_scraped.items():
                if stamped is None:
                    continue
                mins = int(prev_interval.get(t) or DEFAULT_TERM_INTERVAL_MINUTES)
                ready = stamped + timedelta(minutes=mins)
                if soonest_due is None or ready < soonest_due:
                    soonest_due = ready

            for term, prices in thresholds_in.items():
                prices = prices or {}
                every = requested_interval[term]
                stamp = prev_scraped.get(term)
                if term not in prev_scraped and soonest_due is not None:
                    # Due exactly when the rest of the group is.
                    stamp = soonest_due - timedelta(minutes=every)
                cursor.execute(
                    "INSERT INTO user_search_terms "
                    "(user_id, search_term, max_price, min_price, is_priority, "
                    "interval_minutes, last_scraped_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s);",
                    (user_id, term, _price_or_none(prices.get('max')),
                     _price_or_none(prices.get('min')),
                     # Derived from the interval, and still written so a rollback
                     # to the boolean-reading scheduler marks the right terms.
                     bool(tier_has_fast and every <= fastest_rate),
                     every, stamp)
                )

            # REPLACE Exclusions — all per-term. The DELETE also clears any
            # pre-migration global rows, so saving settings once cleans them out.
            cursor.execute("DELETE FROM user_exclusions WHERE user_id = %s;", (user_id,))
            for term, prices in thresholds_in.items():
                for keyword in ((prices or {}).get('exclusions') or []):
                    kw = str(keyword).strip()
                    if kw:
                        cursor.execute(
                            "INSERT INTO user_exclusions (user_id, keyword, search_term) VALUES (%s, %s, %s);",
                            (user_id, kw, term),
                        )

            # REPLACE Includes, same shape as exclusions above. Capped per term
            # because these are unbounded user text and every one of them is
            # checked against every title on every platform.
            cursor.execute("DELETE FROM user_includes WHERE user_id = %s;", (user_id,))
            for term, prices in thresholds_in.items():
                seen_inc = set()
                for keyword in ((prices or {}).get('includes') or [])[:MAX_TERM_INCLUDES]:
                    kw = str(keyword).strip()
                    # Case-insensitive de-dupe: 'Blue' and 'blue' filter
                    # identically, so storing both is pure waste.
                    if kw and kw.lower() not in seen_inc:
                        seen_inc.add(kw.lower())
                        cursor.execute(
                            "INSERT INTO user_includes (user_id, keyword, search_term) VALUES (%s, %s, %s);",
                            (user_id, kw, term),
                        )

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


_UNSUB_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{title} · PixelFlip</title></head>
<body style="margin:0;background:#F7FAFC;font-family:'SF Mono',SFMono-Regular,Consolas,Menlo,monospace;">
  <div style="max-width:520px;margin:64px auto;padding:0 16px;">
    <div style="background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);background-color:#764ba2;
                border-radius:12px 12px 0 0;padding:24px;">
      <div style="font-size:21px;font-weight:700;color:#fff;letter-spacing:-0.4px;">PixelFlip</div>
    </div>
    <div style="background:#fff;border:1px solid #E2E8F0;border-top:none;border-radius:0 0 12px 12px;
                padding:32px 24px;text-align:center;">
      <h1 style="font-size:18px;color:#2D3748;margin:0 0 12px;">{title}</h1>
      <p style="font-size:14px;line-height:1.6;color:#718096;margin:0 0 24px;">{body}</p>
      <a href="{app_url}/settings" style="display:inline-block;background:#764ba2;color:#fff;
         font-size:14px;font-weight:600;text-decoration:none;padding:12px 26px;border-radius:8px;">
        Manage alert preferences
      </a>
    </div>
  </div>
</body></html>"""


_UNSUB_PAGE_RAW = """<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{title} · PixelFlip</title></head>
<body style="margin:0;background:#F7FAFC;font-family:'SF Mono',SFMono-Regular,Consolas,Menlo,monospace;">
  <div style="max-width:520px;margin:56px auto;padding:0 16px;">
    <div style="background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);background-color:#764ba2;
                border-radius:12px 12px 0 0;padding:24px;">
      <div style="font-size:21px;font-weight:700;color:#fff;letter-spacing:-0.4px;">PixelFlip</div>
    </div>
    <div style="background:#fff;border:1px solid #E2E8F0;border-top:none;border-radius:0 0 12px 12px;
                padding:28px 24px;text-align:center;">
      <h1 style="font-size:18px;color:#2D3748;margin:0 0 16px;">{title}</h1>
      {body}
    </div>
  </div>
</body></html>"""


def _unsub_response(title, body, status=200, raw_body=False):
    """`raw_body=True` passes HTML through instead of wrapping it as a paragraph."""
    # dashboard., not app. — that is the subdomain that actually resolves.
    app_url = (os.getenv('FRONTEND_URL') or 'https://dashboard.pixelflip.app').rstrip('/')
    if raw_body:
        html = _UNSUB_PAGE_RAW.format(title=title, body=body)
    else:
        html = _UNSUB_PAGE.format(title=title, body=body, app_url=app_url)
    return Response(html, status=status, mimetype='text/html')


def _get_notification_channels(user_id):
    """Current channel/category prefs for a user, or None on DB failure."""
    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT notification_channels FROM user_settings WHERE user_id = %s", (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            return {}
        current = row[0] if not isinstance(row, dict) else row.get('notification_channels')
        return _normalize_notification_channels(current)
    except Exception as e:
        print(f'[unsubscribe] read error: {e}', flush=True)
        return None
    finally:
        cursor.close()
        conn.close()


def _update_notification_channels(user_id, updates):
    """Merge `updates` into the user's stored prefs. Returns True on success."""
    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT notification_channels FROM user_settings WHERE user_id = %s", (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            return False
        current = row[0] if not isinstance(row, dict) else row.get('notification_channels')
        channels = _normalize_notification_channels(current)
        channels.update(updates)
        cursor.execute(
            "UPDATE user_settings SET notification_channels = %s::jsonb WHERE user_id = %s",
            (json.dumps(channels), user_id),
        )
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f'[unsubscribe] write error: {e}', flush=True)
        return None
    finally:
        cursor.close()
        conn.close()


_PREF_LABELS = [
    ('listing_alerts', 'Listing alerts',
     'New marketplace matches from your saved searches. This is the core PixelFlip alert.'),
    ('product_updates', 'Product updates',
     'New features and meaningful changes to how PixelFlip works.'),
    ('marketing', 'Tips & offers',
     'Reselling tips, occasional promotions and product news.'),
]


def _pref_form(user_id, token, channels, saved=False):
    """Preference centre: choose which email types to receive."""
    rows = []
    for key, label, desc in _PREF_LABELS:
        checked = 'checked' if channels.get(key, True) else ''
        rows.append(f'''
        <label style="display:block;text-align:left;border:1px solid #E2E8F0;border-radius:8px;
                      padding:14px 16px;margin-bottom:10px;cursor:pointer;">
          <input type="checkbox" name="cat" value="{key}" {checked}
                 style="margin-right:10px;vertical-align:top;margin-top:3px;" />
          <span style="font-weight:600;color:#2D3748;font-size:14px;">{label}</span>
          <div style="margin-left:24px;font-size:12px;color:#718096;line-height:1.5;">{desc}</div>
        </label>''')

    banner = ''
    if saved:
        banner = ('<div style="background:#F0FFF4;border:1px solid #9AE6B4;color:#22543D;'
                  'border-radius:8px;padding:11px;margin-bottom:18px;font-size:13px;">'
                  'Your preferences have been saved.</div>')

    # dashboard., not app. — that is the subdomain that actually resolves.
    app_url = (os.getenv('FRONTEND_URL') or 'https://dashboard.pixelflip.app').rstrip('/')
    body = f'''
      {banner}
      <p style="font-size:13px;line-height:1.6;color:#718096;margin:0 0 18px;text-align:left;">
        Choose which emails you'd like to receive. Unchecking everything stops all
        marketing and alert email. Account and billing notices are always sent.
      </p>
      <form method="POST" action="/unsubscribe">
        <input type="hidden" name="uid" value="{_esc_attr(user_id)}" />
        <input type="hidden" name="token" value="{_esc_attr(token)}" />
        <input type="hidden" name="form" value="1" />
        {''.join(rows)}
        <button type="submit"
                style="width:100%;background:#764ba2;color:#fff;border:none;border-radius:8px;
                       padding:13px;font-size:14px;font-weight:600;cursor:pointer;
                       font-family:inherit;margin-top:6px;">
          Save preferences
        </button>
      </form>
      <form method="POST" action="/unsubscribe" style="margin-top:10px;">
        <input type="hidden" name="uid" value="{_esc_attr(user_id)}" />
        <input type="hidden" name="token" value="{_esc_attr(token)}" />
        <input type="hidden" name="form" value="1" />
        <button type="submit"
                style="width:100%;background:transparent;color:#718096;border:1px solid #CBD5E0;
                       border-radius:8px;padding:11px;font-size:13px;cursor:pointer;
                       font-family:inherit;">
          Unsubscribe from all
        </button>
      </form>
      <div style="margin-top:16px;font-size:12px;">
        <a href="{app_url}/settings" style="color:#667eea;">Open full settings</a>
      </div>'''
    return _unsub_response('Email preferences', body, raw_body=True)


def _esc_attr(v):
    return (str(v or '').replace('&', '&amp;').replace('"', '&quot;')
            .replace('<', '&lt;').replace('>', '&gt;'))


@app.route('/unsubscribe', methods=['GET', 'POST'])
@app.route('/api/unsubscribe', methods=['GET', 'POST'])
def unsubscribe():
    """
    Email preference centre + one-click unsubscribe. Deliberately
    unauthenticated: a recipient must be able to opt out straight from the
    email, and CAN-SPAM plus the Gmail/Yahoo bulk-sender rules require it to
    work in one step. A signed token stands in for a login.

    Three cases:
      * POST with List-Unsubscribe=One-Click — Gmail's native control. Must opt
        the user out immediately, with no page and no confirmation step.
      * GET  — a human clicked the footer link: show the preference centre.
      * POST from that form — save the chosen categories.
    """
    form = request.form if request.form else {}
    user_id = (request.args.get('uid') or form.get('uid') or '').strip()
    token = (request.args.get('token') or form.get('token') or '').strip()

    if not user_id or not token:
        return _unsub_response('Invalid link', 'This unsubscribe link is missing information.', 400)

    try:
        from email_templates import make_unsubscribe_token
        expected = make_unsubscribe_token(user_id)
    except Exception as e:
        print(f'[unsubscribe] token build failed: {e}', flush=True)
        return _unsub_response('Something went wrong',
                               'We could not process this request. Please try again.', 500)

    # Constant-time compare so the token can't be guessed byte by byte.
    if not hmac.compare_digest(token, expected):
        return _unsub_response('Invalid link',
                               'This unsubscribe link is not valid or has expired.', 400)

    # Gmail/Yahoo one-click: opt out of everything opt-out-able, immediately.
    if request.method == 'POST' and not form.get('form'):
        updates = {k: False for k in EMAIL_CATEGORIES}
        updates['email'] = False
        if _update_notification_channels(user_id, updates) is None:
            return _unsub_response('Something went wrong',
                                   'We could not update your preferences.', 500)
        return _unsub_response(
            'Unsubscribed',
            'You will no longer receive alert or marketing email from PixelFlip. '
            'Your searches keep running, and you can turn email back on any time.',
        )

    if request.method == 'POST':
        selected = set(request.form.getlist('cat'))
        updates = {k: (k in selected) for k in EMAIL_CATEGORIES}
        # The email channel stays on only while at least one category is wanted.
        updates['email'] = bool(selected)
        if _update_notification_channels(user_id, updates) is None:
            return _unsub_response('Something went wrong',
                                   'We could not update your preferences.', 500)
        channels = _get_notification_channels(user_id) or updates
        return _pref_form(user_id, token, channels, saved=True)

    channels = _get_notification_channels(user_id)
    if channels is None:
        return _unsub_response('Something went wrong',
                               'We could not load your preferences.', 500)
    return _pref_form(user_id, token, channels or dict(EMAIL_CATEGORIES))


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
        # Conditional, so rowcount tells us whether this was a real OFF->ON
        # arming or a START pressed on a scanner that was already running.
        cursor.execute("UPDATE user_settings SET is_active = TRUE "
                       "WHERE user_id = %s AND is_active IS DISTINCT FROM TRUE;",
                       (user_id,))
        armed_now = cursor.rowcount > 0
        conn.commit()

        # Do NOT clear last_scraped_at here.
        #
        # An earlier version did, so arming always forced an immediate scan. That
        # made STOP/START reset every countdown: stopping with 4:20 left and
        # starting again 15 seconds later began a fresh interval instead of
        # resuming at 4:05 — and it handed anyone a "re-scan every marketplace on
        # demand" button, which is how we get rate-limited.
        #
        # Time passes whether or not the scanner is on. The schedule is simply
        # last_scraped_at + the term's interval, and STOP is not an event in it.
        # A user who has never scanned still starts at once: their terms carry a
        # NULL last_scraped_at, which is already due.
        #
        # The complaint that produced the clearing was really about the message
        # below lying — it said "shortly" while the real wait was minutes away.
        # Telling the truth fixes that without touching the schedule.

        # Immediate console feedback. Starting is only a flag flip — the scraper
        # picks it up on its next cycle, so without this the console stayed
        # completely silent for up to SCRAPE_CYCLE_SLEEP_SEC and START read as
        # broken. Also drop the is_user_active cache so a scrape already in
        # flight for this user notices the change at its next checkpoint rather
        # than up to the TTL later.
        try:
            import scrape_logs
            message = "Scanner is already running."
            if armed_now:
                message = "Scanner armed — your first scan begins shortly."
                try:
                    cursor.execute("SELECT * FROM user_settings WHERE user_id = %s;",
                                   (user_id,))
                    cols = [d[0] for d in cursor.description]
                    us_row = dict(zip(cols, cursor.fetchone() or ()))
                    due_at = _term_due_map(conn, user_id, _effective_plan_tier(us_row))
                    if due_at:
                        wait = int(min(due_at.values()) - time.time())
                        if wait > 0:
                            mins, secs = divmod(wait, 60)
                            message = (f"Scanner armed — next scan in {mins}m {secs:02d}s, "
                                       "picking up where the countdown left off.")
                except Exception as e:
                    # A countdown is not worth failing START over; the generic
                    # message is still true, just vaguer.
                    print(f"[start] could not compute next-due for {user_id[:8]}: {e}",
                          flush=True)
            scrape_logs.add_log(user_id, message, "info")
        except Exception:
            pass
        try:
            from scraper_multi_user import _invalidate_active_cache
            _invalidate_active_cache(user_id)
        except Exception:
            pass
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
        # Do NOT touch last_scraped_at here. Stopping is not scraping, and
        # stamping it started the countdown from the moment the user hit STOP —
        # so pressing START again meant waiting a full interval for a scan that
        # had never run. That produced "ghost scrapes": a fresh timestamp, a
        # ticking timer, no listings and no logs.
        cursor.execute(
            "UPDATE user_settings SET is_active = FALSE WHERE user_id = %s;",
            (user_id,)
        )
        conn.commit()
        # is_user_active() caches for a few seconds so the per-listing check in
        # the result loop is not 295 fresh TLS connections. Drop that entry now
        # or STOP could take the whole TTL to be noticed.
        try:
            from scraper_multi_user import _invalidate_active_cache
            _invalidate_active_cache(user_id)
        except Exception:
            pass
        # An in-flight scrape only notices is_active between platforms, so the
        # dashboard could sit on "SCRAPING..." long after Stop was pressed.
        # Clearing the flag here makes the UI reflect the user's intent
        # immediately; the running scrape still exits at its next checkpoint.
        try:
            set_user_scraping(user_id, False)
        except Exception:
            pass
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
            json={"password": new_password},
            # Every other outbound call here is bounded; this one was not, and a
            # hung Supabase connection would pin the worker indefinitely.
            timeout=20,
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

    # Stripe never auto-applies a coupon to an API-created Checkout Session.
    # Restricting a coupon to specific products only limits where it is VALID —
    # it does not attach it to anything. A session gets a discount exactly two
    # ways, and they are mutually exclusive:
    #   discounts=[...]            -> applied for the customer, no code to type
    #   allow_promotion_codes=True -> customer types a promotion code
    # Set STRIPE_PREBETA_COUPON_ID to the coupon id (looks like 'AbC123xY', not
    # the name) for silent pre-beta pricing; unset it when pre-beta ends and
    # checkout falls back to the manual promotion-code field.
    prebeta_coupon = (os.getenv('STRIPE_PREBETA_COUPON_ID') or '').strip()
    discount_kwargs = (
        {'discounts': [{'coupon': prebeta_coupon}]}
        if prebeta_coupon
        else {'allow_promotion_codes': True}
    )
    print(f"[Stripe checkout] discount={'coupon ' + prebeta_coupon if prebeta_coupon else 'promo-code field'}",
          flush=True)

    try:
        checkout_session = stripe.checkout.Session.create(
            mode='subscription',
            line_items=[{'price': price_id, 'quantity': 1}],
            client_reference_id=user_id,
            metadata={'user_id': user_id, 'plan': plan},
            success_url=f"{base}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/?checkout=canceled",
            **discount_kwargs,
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


def _iso_utc(value):
    """
    Serialize a datetime with an explicit UTC offset.

    `listings.created_at` is `timestamp WITHOUT time zone`, so psycopg2 hands
    back a naive datetime and a bare .isoformat() emits no offset at all:

        2026-08-04T19:11:26.704634

    ECMA-262 parses an offsetless date-TIME string as LOCAL time (only
    date-only forms default to UTC). So `new Date(...)` in the browser read
    every UTC timestamp as though it were the viewer's wall clock and shifted
    each listing into the future by their whole UTC offset — "Found just now"
    on an hour-old listing, "6h ago" on one from the night before.

    Note this is NOT what Flask's default datetime serializer does; jsonify
    would emit an RFC-822 string ending in GMT, which parses correctly. The bug
    only existed because this endpoint stringified the value first.

    The column holds UTC (NOW() with the DB on UTC), so stamping UTC on a naive
    value is correct. Already-aware values like listed_at (TIMESTAMPTZ) keep
    their own offset.
    """
    if value is None or not hasattr(value, 'isoformat'):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


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
            created = _iso_utc(r['created_at'])
            listed = _iso_utc(r.get('listed_at'))
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
                # Craigslist and Facebook report a real post time. Mercari's
                # search payload has none at all (probed 2026-08-13), so its
                # date is inferred from the listing photo's upload stamp — close,
                # but not the same thing. Flagged so the UI can show it as
                # approximate rather than quietly overstating precision.
                "listed_at_approx": bool(listed) and (r.get('platform') == 'Mercari'),
            })
        return jsonify({"listings": out, "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        print(f"listings: {e}", flush=True)
        return jsonify(error=str(e)), 500
    finally:
        cursor.close()
        conn.close()


SUPPORT_INBOX_EMAIL = os.getenv('SUPPORT_INBOX_EMAIL', 'support@pixelflip.app')
_SUPPORT_COOLDOWN_SECONDS = 60
_support_last_sent = {}


@app.route('/api/support/message', methods=['POST', 'OPTIONS'])
@app.route('/support/message', methods=['POST', 'OPTIONS'])
@require_auth
def submit_support_message(user_id):
    """
    Where Flip's help chat hands off when it has no canned answer.

    Authenticated deliberately. This endpoint sends mail, so leaving it open
    would make it a spam relay pointed at our own inbox — and since the caller
    is logged in we can read their address from auth.users instead of trusting
    a field in the request body that anyone could forge.
    """
    data = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    if len(message) < 5:
        return jsonify({"success": False, "error": "Add a little more detail so we can help."}), 400
    if len(message) > 4000:
        message = message[:4000] + '\n\n[truncated]'

    # Per-user cooldown: auth stops strangers, not an impatient user clicking
    # send five times because the first one appeared to do nothing.
    now = time.time()
    elapsed = now - _support_last_sent.get(user_id, 0)
    if elapsed < _SUPPORT_COOLDOWN_SECONDS:
        wait = int(_SUPPORT_COOLDOWN_SECONDS - elapsed) or 1
        return jsonify({
            "success": False,
            "error": f"Message already sent — you can send another in {wait}s.",
        }), 429

    try:
        from scraper_multi_user import get_user_auth_email
        sender = get_user_auth_email(user_id) or '(address unavailable)'
    except Exception:
        sender = '(address unavailable)'

    subject = f'[PixelFlip help] {message.splitlines()[0][:60]}'
    body = (
        f'From:    {sender}\n'
        f'User ID: {user_id}\n'
        f'Sent:    {datetime.now(timezone.utc).isoformat()}\n'
        f'{"-" * 52}\n\n'
        f'{message}\n'
    )

    # Log before sending: if Mailgun is down, the message still exists
    # somewhere rather than disappearing with the user's only copy of it.
    print(f"📮 support message from {sender} ({len(message)} chars)", flush=True)

    try:
        from listing_notifications import send_mailgun_email
        ok, err = send_mailgun_email(SUPPORT_INBOX_EMAIL, subject, body)
    except Exception as e:
        ok, err = False, str(e)

    if not ok:
        print(f"❌ support message send failed: {err}\n{body}", flush=True)
        return jsonify({
            "success": False,
            "error": f"Could not send that. Email us directly at {SUPPORT_INBOX_EMAIL}.",
        }), 502

    _support_last_sent[user_id] = now
    return jsonify({"success": True})


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


@app.route('/api/push/test', methods=['POST', 'OPTIONS'])
@app.route('/push/test', methods=['POST', 'OPTIONS'])
@require_auth
def push_test(user_id):
    """
    Fire a test notification at the caller's stored subscription.

    Push can't be verified from an automated browser (Playwright-launched
    Chrome can't register with FCM), so this gives the user a one-click way to
    confirm the whole chain — subscription stored, VAPID signing, delivery,
    service worker display — from a real browser.
    """
    if request.method == 'OPTIONS':
        return '', 200
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT push_subscription FROM user_settings WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        sub = row.get('push_subscription') if row else None
        if isinstance(sub, str):
            try:
                sub = json.loads(sub)
            except Exception:
                sub = None
        if not sub or not sub.get('endpoint'):
            return jsonify({
                "success": False,
                "error": "No push subscription saved. Enable push notifications first."
            }), 400

        from listing_notifications import send_web_push
        ok, err = send_web_push(
            sub,
            'PixelFlip test',
            'Push notifications are working — real alerts will look like this.',
            '/',
        )
        if not ok:
            return jsonify({"success": False, "error": err or 'send failed'}), 502
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/tour', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/tour', methods=['GET', 'POST', 'OPTIONS'])
@require_auth
def tour_progress(user_id):
    """
    Which tour sections this user has completed.

    Stored server-side rather than in localStorage so a user who onboards on
    their phone doesn't get the tour again on desktop. Shape is
    {"intro": true, "first_scan": false} — a dict, so new sections can be
    added later and shown only to people who haven't seen that one.
    """
    if request.method == 'OPTIONS':
        return '', 200
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if request.method == 'GET':
            cursor.execute("SELECT has_seen_tour FROM user_settings WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            seen = (row or {}).get('has_seen_tour') or {}
            if isinstance(seen, str):
                try:
                    seen = json.loads(seen)
                except Exception:
                    seen = {}
            return jsonify({"seen": seen})

        data = request.get_json(silent=True) or {}
        section = (data.get('section') or '').strip()
        if not section:
            return jsonify({"error": "section required"}), 400

        # Merge rather than replace, so completing one section can't wipe the
        # record of another.
        cursor.execute(
            """
            UPDATE user_settings
            SET has_seen_tour = COALESCE(has_seen_tour, '{}'::jsonb) || %s::jsonb
            WHERE user_id = %s
            RETURNING has_seen_tour
            """,
            (json.dumps({section: bool(data.get('done', True))}), user_id),
        )
        row = cursor.fetchone()
        conn.commit()
        return jsonify({"success": True, "seen": (row or {}).get('has_seen_tour') or {}})
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


def _preflight_or_die(port):
    """
    Refuse to start if this process would be a broken or duplicate backend.

    Both checks exist because their failure modes are silent and expensive.

    1. Duplicate instance. A second app.py still starts its own scraper thread.
       Both processes then scrape the same users, both stamp last_scraped_at,
       and console logs live in per-process memory — so the dashboard shows the
       console of whichever process won the port while the other one does the
       actual scraping. The symptom is a "ghost scrape": no output, no results
       on screen, and a burned interval, with rows quietly landing in the DB.
       Exiting here is far better than running invisibly.

    2. Wrong interpreter. System Python has no Playwright/patchright, so every
       browser scraper returns an empty string instantly while Craigslist keeps
       working (it only needs requests). That reads exactly like the
       marketplaces blocking you, and it has burned several debugging sessions.
    """
    import socket
    import sys

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(('0.0.0.0', port))
    except OSError:
        print(
            f"\n{'=' * 68}\n"
            f"  REFUSING TO START — port {port} is already in use.\n\n"
            f"  Another app.py is almost certainly running. A second one would\n"
            f"  start its own scraper thread, burn scan intervals, and log to a\n"
            f"  console the dashboard never reads.\n\n"
            f"  Find it:  Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\"\n"
            f"{'=' * 68}\n",
            flush=True,
        )
        sys.exit(1)
    finally:
        probe.close()

    missing = [
        name for name in ('playwright', 'patchright')
        if __import__('importlib.util', fromlist=['util']).find_spec(name) is None
    ]
    if missing:
        print(
            f"\n{'=' * 68}\n"
            f"  REFUSING TO START — missing: {', '.join(missing)}\n\n"
            f"  Running interpreter:\n    {sys.executable}\n\n"
            f"  Every browser scraper would return nothing instantly while\n"
            f"  Craigslist kept working, which looks like an anti-bot block.\n\n"
            f"  Start it with the venv interpreter instead:\n"
            f"    .\\.venv\\Scripts\\python.exe app.py\n"
            f"{'=' * 68}\n",
            flush=True,
        )
        sys.exit(1)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))

    # Must run BEFORE the scraper thread starts — the thread is what does the
    # damage when a second instance slips through.
    _preflight_or_die(port)

    # DISABLE_SCRAPER_THREAD=1 makes this process API-only. Set it on the Render
    # WEB service once worker.py runs as its own Background Worker — otherwise
    # both processes run the scrape loop, every user gets scraped twice, and
    # each interval is burned twice. That is the ghost-scrape failure again, now
    # split across two services where no in-process guard can detect it.
    if _env_flag_true('DISABLE_SCRAPER_THREAD'):
        print("Scraper thread disabled (DISABLE_SCRAPER_THREAD=1) — API only. "
              "The scrape loop is expected to run in a separate worker service.",
              flush=True)
    elif _scraper_thread_started:
        # ensure_scraper_thread_started() already runs at import time and starts
        # the loop when ENABLE_SCRAPER_THREAD=1. Starting another here would give
        # this single process TWO scraper threads competing over the same users.
        # ENABLE_SCRAPER_THREAD exists for gunicorn, where __main__ never runs.
        print("Scraper thread already started via ENABLE_SCRAPER_THREAD "
              "(that flag is for gunicorn; it is not needed with `python app.py`).",
              flush=True)
    else:
        scraper_thread = threading.Thread(target=start_background_scraper, daemon=True)
        scraper_thread.start()

    app.run(host='0.0.0.0', port=port, debug=False)