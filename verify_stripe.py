"""
Pre-flight check for Stripe config. Run it before taking real payments.

    .venv\\Scripts\\python.exe verify_stripe.py

Reads whatever is in .env, so it works for test or live. Every id below is
PER-MODE: a test-mode value against a live key raises "No such ..." and
create_checkout returns 500 — checkout breaks entirely, for every plan, rather
than merely losing the discount. This catches that in ten seconds instead of
from a customer.

The same check also runs on every Render boot (boot_check.py), so the LIVE
config — which this machine's test keys cannot see — is verified in the Render
log and summarised in /health.

Prints no secret values. Price, coupon and endpoint ids are not secrets.
"""
import os
import sys

import requests

API = 'https://api.stripe.com/v1'
REQUIRED_EVENTS = {'checkout.session.completed', 'customer.subscription.updated',
                   'customer.subscription.deleted'}


def stripe_key_mode(key):
    """'LIVE', 'TEST' or None, from the key prefix alone (no network)."""
    key = (key or '').strip()
    if key.startswith(('sk_live', 'rk_live')):
        return 'LIVE'
    if key.startswith(('sk_test', 'rk_test')):
        return 'TEST'
    return None


def _error_message(r):
    try:
        return ((r.json() or {}).get('error') or {}).get('message') or r.text
    except Exception:
        return r.text


def check_stripe(env=None, emit=print, http_get=None, timeout=25, service_url=None):
    """
    Resolve every Stripe id this deploy depends on. Returns (mode, problems).

    `emit` receives the human-readable lines. `problems` is empty when checkout,
    the pre-beta discount and the webhook will all work in this key's mode.
    `service_url` (e.g. RENDER_EXTERNAL_URL) adds a check that a webhook
    endpoint actually points at this service.
    """
    env = os.environ if env is None else env
    http_get = http_get or requests.get
    key = (env.get('STRIPE_SECRET_KEY') or '').strip()
    mode = stripe_key_mode(key)
    problems = []

    emit(f"stripe key mode : {mode or 'UNRECOGNISED — expected sk_live_ or sk_test_'}")
    if not mode:
        problems.append('STRIPE_SECRET_KEY missing or unrecognised')
        return mode, problems
    auth = (key, '')
    expect_live = mode == 'LIVE'

    def get(label, url, params=None):
        try:
            return http_get(url, auth=auth, params=params, timeout=timeout)
        except Exception as e:
            problems.append(f"{label}: request failed ({type(e).__name__})")
            return None

    def check(label, url):
        r = get(label, url)
        if r is None:
            return None
        if r.status_code != 200:
            # This used to read `(problems if required else print)(...)`, which
            # CALLS the list and dies with a TypeError — on exactly the failure
            # this script exists to report (a test id under a live key).
            problems.append(f"{label}: {r.status_code} {_error_message(r)[:110]}")
            return None
        d = r.json() or {}
        extra = ''
        if 'unit_amount' in d and d.get('unit_amount') is not None:
            rec = (d.get('recurring') or {}).get('interval')
            extra = f"  {d['unit_amount'] / 100:.2f} {str(d.get('currency', '')).upper()}" + (f"/{rec}" if rec else '')
        if 'percent_off' in d or 'amount_off' in d:
            off = (f"{d.get('percent_off')}% off" if d.get('percent_off') is not None
                   else f"{(d.get('amount_off') or 0) / 100:.2f} off")
            extra = f"  {off}, duration={d.get('duration')}"
        emit(f"  OK    {label}{extra}")
        if d.get('livemode') is not None and d['livemode'] != expect_live:
            problems.append(f"{label}: livemode={d['livemode']} but key is {mode}")
        if d.get('active') is False:
            problems.append(f"{label}: archived (active=false) — checkout cannot use it")
        if d.get('valid') is False:
            problems.append(f"{label}: coupon is no longer valid")
        return d

    emit("prices")
    for name in ('STRIPE_PRICE_BASIC_ID', 'STRIPE_PRICE_PRO_ID'):
        pid = (env.get(name) or '').strip()
        if not pid:
            problems.append(f"{name} is not set")
            continue
        check(f"{name} ({pid})", f"{API}/prices/{pid}")

    emit("coupon")
    coupon = (env.get('STRIPE_PREBETA_COUPON_ID') or '').strip()
    if not coupon:
        emit("  ----  STRIPE_PREBETA_COUPON_ID unset — checkout will show the manual "
             "promotion-code field instead. Safe, just not automatic.")
    else:
        check(f"STRIPE_PREBETA_COUPON_ID ({coupon})", f"{API}/coupons/{coupon}")

    emit("webhook")
    whsec = (env.get('STRIPE_WEBHOOK_SECRET') or '').strip()
    if not whsec.startswith('whsec_'):
        problems.append("STRIPE_WEBHOOK_SECRET missing or malformed (must start whsec_)")
    else:
        # The secret cannot be validated offline, but the endpoints registered on
        # this key can: one must be enabled, receive the three events the
        # entitlement code depends on, and (when we know our own URL) point here.
        r = get('webhook endpoints', f"{API}/webhook_endpoints", params={'limit': 20})
        if r is not None and r.status_code == 200:
            eps = (r.json() or {}).get('data', []) or []
            usable = []
            for ep in eps:
                events = set(ep.get('enabled_events') or [])
                missing = sorted(REQUIRED_EVENTS - events) if '*' not in events else []
                emit(f"  {'OK  ' if not missing and ep.get('status') == 'enabled' else 'WARN'}  "
                     f"endpoint {ep.get('url')}  status={ep.get('status')}"
                     + (f"  missing events: {missing}" if missing else ''))
                if ep.get('status') == 'enabled' and not missing:
                    usable.append(ep)
            if not eps:
                problems.append(f"no webhook endpoints registered in {mode} mode")
            elif not usable:
                problems.append("no ENABLED webhook endpoint receives checkout.session.completed "
                                "+ customer.subscription.updated/deleted")
            elif service_url:
                base = service_url.rstrip('/')
                if not any(str(ep.get('url') or '').startswith(base) for ep in usable):
                    # A custom domain in front of the service is legitimate, so
                    # this is a warning to look at, not a hard failure.
                    emit(f"  WARN  no enabled endpoint points at {base} — fine only if a "
                         f"custom domain fronts this service")
        elif r is not None:
            problems.append(f"could not list webhook endpoints: {r.status_code}")

    return mode, problems


def main():
    from dotenv import load_dotenv
    load_dotenv()
    mode, problems = check_stripe()
    print()
    if problems:
        print(f"*** {len(problems)} PROBLEM(S) — do not take payments yet ***")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("All Stripe config resolves correctly for", mode, "mode.")


if __name__ == '__main__':
    main()
