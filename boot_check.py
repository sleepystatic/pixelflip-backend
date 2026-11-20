"""
Boot self-check: what this deploy is, and whether its config can actually do
what the product advertises.

Runs once per process start on Render (RENDER=true, which Render sets on every
service) or wherever BOOT_SELF_CHECK=1; BOOT_SELF_CHECK=0 turns it off. It
prints `[boot] ...` lines to the Render log and keeps a summary that /health
returns, so a deploy can be verified from outside — the free tier has no shell
to run verify_stripe.py in, and the live Stripe config is invisible from a
machine holding test keys.

NEVER prints or returns a secret value: presence, key MODE (live/test), and
non-secret switches only. Price and coupon ids are not secrets.

Import is side-effect free; nothing runs until start() is called.
"""
import os
import platform
import threading
from datetime import datetime, timezone

_STARTED_AT = datetime.now(timezone.utc)
_lock = threading.Lock()
_started = False
_state = {
    # 'skipped' until start() runs: local runs and tests make no Stripe calls.
    'billing': {'mode': None, 'check': 'skipped', 'problems': 0},
}

# Non-secret switches worth seeing on every boot: they change behaviour, and
# render.yaml / the Render dashboard can disagree about them.
SWITCHES = (
    'MAX_ACTIVE_USERS', 'SCRAPE_MAX_WORKERS', 'BROWSER_MAX_CONCURRENT',
    'DESCRIPTION_SCAN_ENABLED', 'SEARCH_CACHE_ENABLED', 'SCRAPE_COALESCE_WINDOW_SEC',
    'SCRAPE_LEASE_SEC', 'MERCARI_ALLOW_CAPTURE', 'FB_LEAN_FETCH', 'LOG_MEMORY_ALWAYS',
    'PREBETA_ACTIVE', 'ENABLE_SCRAPER_THREAD', 'ENABLE_CLEANUP_THREAD',
    # Render sets this to "--preload ..." by default; render.yaml overrides it.
    # Printed so the first deploy shows which value gunicorn actually got.
    'GUNICORN_CMD_ARGS',
)

# (code, human explanation). Codes are what /health exposes.
WARNINGS = {
    'stripe_key_missing': "STRIPE_SECRET_KEY missing or unrecognised — checkout cannot start.",
    'stripe_test_key_on_render': "Render holds a TEST Stripe key — real customers cannot pay.",
    'stripe_prices_missing': "STRIPE_PRICE_BASIC_ID / STRIPE_PRICE_PRO_ID not both set — checkout fails for that plan.",
    'stripe_webhook_secret_missing': "STRIPE_WEBHOOK_SECRET missing — paid checkouts are never confirmed by webhook.",
    'supabase_service_role_missing': "SUPABASE_SERVICE_ROLE_KEY missing — password reset cannot work.",
    'mailgun_missing': "Mailgun not fully configured — no alert emails, no reset or verification codes.",
    'fb_proxy_missing': "No FB_PROXY_URL / FB_PROXY_LIST — Facebook from a datacenter IP returns successful-looking EMPTY pages.",
    'vision_missing': "Pro advertises the AI image filter, but GOOGLE_VISION_API_KEY / AI_IMAGE_FILTER_PLATFORMS is unset — it never runs.",
    'vapid_missing': "VAPID keys missing — web push alerts are off.",
    'twilio_missing': "Twilio not fully configured — SMS alerts are off.",
}


def _env(env):
    return os.environ if env is None else env


def _set(env, name):
    return bool((env.get(name) or '').strip())


def _all(env, *names):
    return all(_set(env, n) for n in names)


def enabled(env=None):
    env = _env(env)
    flag = (env.get('BOOT_SELF_CHECK') or '').strip().lower()
    if flag in ('0', 'false', 'no', 'off'):
        return False
    if flag in ('1', 'true', 'yes', 'on'):
        return True
    return (env.get('RENDER') or '').strip().lower() == 'true'


def stripe_mode(env=None):
    from verify_stripe import stripe_key_mode
    mode = stripe_key_mode(_env(env).get('STRIPE_SECRET_KEY'))
    return mode.lower() if mode else ('unrecognised' if _set(_env(env), 'STRIPE_SECRET_KEY') else 'unset')


def version_info(env=None):
    env = _env(env)
    commit = (env.get('RENDER_GIT_COMMIT') or env.get('GIT_COMMIT') or '').strip()
    return {
        'commit': commit[:12] or None,
        'branch': (env.get('RENDER_GIT_BRANCH') or '').strip() or None,
        'python': platform.python_version(),
        'started_at': _STARTED_AT.isoformat(timespec='seconds'),
    }


def warning_codes(env=None):
    """Config problems that silently disable an advertised feature. No network."""
    env = _env(env)
    codes = []
    mode = stripe_mode(env)
    if mode in ('unset', 'unrecognised'):
        codes.append('stripe_key_missing')
    elif mode == 'test' and (env.get('RENDER') or '').strip().lower() == 'true':
        codes.append('stripe_test_key_on_render')
    if not _all(env, 'STRIPE_PRICE_BASIC_ID', 'STRIPE_PRICE_PRO_ID'):
        codes.append('stripe_prices_missing')
    if not (env.get('STRIPE_WEBHOOK_SECRET') or '').strip().startswith('whsec_'):
        codes.append('stripe_webhook_secret_missing')
    if not _set(env, 'SUPABASE_SERVICE_ROLE_KEY'):
        codes.append('supabase_service_role_missing')
    if not _all(env, 'MAILGUN_API_KEY', 'MAILGUN_DOMAIN', 'MAILGUN_FROM_EMAIL'):
        codes.append('mailgun_missing')
    # Facebook deliberately never falls back to PROXY_URL (scraper _proxy_for).
    if not (_set(env, 'FB_PROXY_URL') or _set(env, 'FB_PROXY_LIST') or _set(env, 'FB_NO_PROXY')):
        codes.append('fb_proxy_missing')
    if not _all(env, 'GOOGLE_VISION_API_KEY', 'AI_IMAGE_FILTER_PLATFORMS'):
        codes.append('vision_missing')
    if not _all(env, 'VAPID_PUBLIC_KEY', 'VAPID_PRIVATE_KEY'):
        codes.append('vapid_missing')
    if not _all(env, 'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER'):
        codes.append('twilio_missing')
    return codes


def config_report(env=None):
    """Human-readable lines for the boot log. Presence and switches only."""
    env = _env(env)

    def mark(*names):
        return 'set' if _all(env, *names) else ('PARTIAL' if any(_set(env, n) for n in names) else 'MISSING')

    v = version_info(env)
    lines = [
        f"version: commit={v['commit'] or 'unknown'} branch={v['branch'] or 'unknown'} "
        f"python={v['python']} pid={os.getpid()}",
        "config: "
        f"database={mark('DATABASE_URL')} "
        f"supabase={mark('SUPABASE_URL', 'SUPABASE_ANON_KEY', 'SUPABASE_SERVICE_ROLE_KEY')} "
        f"stripe={stripe_mode(env).upper()} "
        f"(prices={mark('STRIPE_PRICE_BASIC_ID', 'STRIPE_PRICE_PRO_ID')}, "
        f"coupon={mark('STRIPE_PREBETA_COUPON_ID')}, webhook={mark('STRIPE_WEBHOOK_SECRET')}) "
        f"mailgun={mark('MAILGUN_API_KEY', 'MAILGUN_DOMAIN', 'MAILGUN_FROM_EMAIL')} "
        f"push={mark('VAPID_PUBLIC_KEY', 'VAPID_PRIVATE_KEY')} "
        f"sms={mark('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER')} "
        f"fb_proxy={'set' if (_set(env, 'FB_PROXY_URL') or _set(env, 'FB_PROXY_LIST')) else 'MISSING'} "
        f"mercari_proxy={'set' if (_set(env, 'MERCARI_PROXY_URL') or _set(env, 'MERCARI_PROXY_LIST')) else ('shared PROXY_URL' if _set(env, 'PROXY_URL') else 'none')} "
        f"vision={mark('GOOGLE_VISION_API_KEY', 'AI_IMAGE_FILTER_PLATFORMS')}",
        "switches: " + ' '.join(
            f"{k}={(env.get(k) or '').strip() or 'default'}" for k in SWITCHES),
    ]
    for code in warning_codes(env):
        lines.append(f"WARNING {code}: {WARNINGS[code]}")
    return lines


def _run_stripe(env, emit):
    from verify_stripe import check_stripe
    try:
        def stripe_line(line):
            line = line.strip()
            emit(line if line.startswith('stripe') else 'stripe ' + line)

        mode, problems = check_stripe(env, emit=stripe_line,
                                      timeout=20, service_url=(env.get('RENDER_EXTERNAL_URL') or None))
        for p in problems:
            emit(f"stripe PROBLEM: {p}")
        result = {'mode': (mode or 'unrecognised').lower(),
                  'check': 'ok' if not problems else 'problems', 'problems': len(problems)}
        emit("stripe: " + ("all ids resolve — payments can be taken" if not problems
                            else f"{len(problems)} problem(s) — DO NOT take payments until fixed"))
    except Exception as e:
        result = {'mode': stripe_mode(env), 'check': 'error', 'problems': 0}
        emit(f"stripe check failed to run: {type(e).__name__}")
    with _lock:
        _state['billing'] = result


def start(env=None, emit=None, background=True):
    """Print the report and verify Stripe. Idempotent; returns False when disabled."""
    global _started
    env = _env(env)
    if not enabled(env):
        return False
    with _lock:
        if _started:
            return True
        _started = True
        _state['billing'] = {'mode': stripe_mode(env), 'check': 'pending', 'problems': 0}
    emit = emit or (lambda line: print('[boot] ' + line, flush=True))
    for line in config_report(env):
        emit(line)
    if background:
        threading.Thread(target=_run_stripe, args=(env, emit), daemon=True,
                         name='boot-stripe-check').start()
    else:
        _run_stripe(env, emit)
    return True


def health_summary(env=None):
    """What /health exposes: codes and modes, never values."""
    with _lock:
        billing = dict(_state['billing'])
    if billing.get('mode') is None:
        billing['mode'] = stripe_mode(env)
    return {'billing': billing, 'config_warnings': warning_codes(env)}
