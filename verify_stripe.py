"""
Pre-flight check for Stripe config. Run it before taking real payments.

    .venv\\Scripts\\python.exe verify_stripe.py

Reads whatever is in .env, so it works for test or live. Every id below is
PER-MODE: a test-mode value against a live key raises "No such ..." and
create_checkout returns 500 — checkout breaks entirely, for every plan, rather
than merely losing the discount. This catches that in ten seconds instead of
from a customer.

Prints no secret values.
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

KEY = (os.getenv('STRIPE_SECRET_KEY') or '').strip()
MODE = 'LIVE' if KEY.startswith('sk_live') else 'TEST' if KEY.startswith('sk_test') else None
AUTH = (KEY, '')
API = 'https://api.stripe.com/v1'

problems = []


def check(label, url, required=True, expect_live=None):
    try:
        r = requests.get(url, auth=AUTH, timeout=25)
    except Exception as e:
        problems.append(f"{label}: request failed ({e})")
        return None
    if r.status_code == 200:
        d = r.json()
        extra = ''
        if 'unit_amount' in d:
            extra = f"  {d['unit_amount'] / 100:.2f} {d.get('currency', '').upper()}"
        if 'percent_off' in d:
            extra = f"  {d.get('percent_off')}% off, duration={d.get('duration')}"
        print(f"  OK    {label}{extra}")
        if expect_live is not None and d.get('livemode') is not None and d['livemode'] != expect_live:
            problems.append(f"{label}: livemode={d['livemode']} but key is {MODE}")
        return d
    msg = (r.json().get('error') or {}).get('message', r.text)[:110]
    (problems if required else print)(f"{label}: {r.status_code} {msg}")
    if not required:
        print(f"  WARN  {label}: {msg}")
    return None


print(f"stripe key mode : {MODE or 'UNRECOGNISED — expected sk_live_ or sk_test_'}")
if not MODE:
    sys.exit(1)
expect_live = MODE == 'LIVE'
print()

print("prices")
for name in ('STRIPE_PRICE_BASIC_ID', 'STRIPE_PRICE_PRO_ID'):
    pid = (os.getenv(name) or '').strip()
    if not pid:
        problems.append(f"{name} is not set")
        continue
    check(f"{name} ({pid})", f"{API}/prices/{pid}", expect_live=expect_live)

print("\ncoupon")
coupon = (os.getenv('STRIPE_PREBETA_COUPON_ID') or '').strip()
if not coupon:
    print("  ----  STRIPE_PREBETA_COUPON_ID unset — checkout will show the manual")
    print("        promotion-code field instead. Safe, just not automatic.")
else:
    check(f"STRIPE_PREBETA_COUPON_ID ({coupon})", f"{API}/coupons/{coupon}")

print("\nwebhook")
whsec = (os.getenv('STRIPE_WEBHOOK_SECRET') or '').strip()
if not whsec.startswith('whsec_'):
    problems.append("STRIPE_WEBHOOK_SECRET missing or malformed (must start whsec_)")
else:
    # The secret cannot be validated offline, but its mode can be sanity-checked
    # against the endpoints registered on this key.
    r = requests.get(f"{API}/webhook_endpoints", auth=AUTH, params={'limit': 10}, timeout=25)
    if r.ok:
        eps = r.json().get('data', [])
        if not eps:
            problems.append(f"no webhook endpoints registered in {MODE} mode")
        for ep in eps:
            print(f"  OK    endpoint {ep.get('url')}  status={ep.get('status')}")
            missing = {'checkout.session.completed', 'customer.subscription.updated',
                       'customer.subscription.deleted'} - set(ep.get('enabled_events') or [])
            if missing and '*' not in (ep.get('enabled_events') or []):
                problems.append(f"endpoint {ep.get('url')} is missing events: {sorted(missing)}")
    else:
        problems.append(f"could not list webhook endpoints: {r.status_code}")

print()
if problems:
    print(f"*** {len(problems)} PROBLEM(S) — do not take payments yet ***")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)
print("All Stripe config resolves correctly for", MODE, "mode.")
