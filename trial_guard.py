# -*- coding: utf-8 -*-
"""
Free trial: who gets one, and how repeat sign-ups are stopped (2026-11-20).

PixelFlip offers a TRIAL_DAYS-day (3) free trial, card required, on either plan,
to people it has never seen before. "Seen before" is checked on every signal
the web gives us, each stored ONLY as a keyed hash (HMAC-SHA256), never raw:

  BEFORE checkout  account  (trial_used_at set, or already a Stripe customer)
                   email    (normalised: Gmail dots and any +tag ignored)
                   IP       (an IPv6 address counts as its /64 network)
                   device   (a random id the dashboard keeps in the browser)
  AFTER checkout   card     (Stripe's card fingerprint: same number, any account)
                   prepaid  (prepaid cards never get a trial)
                   address  (normalised billing address: "Street" = "St",
                             "#2" = "Unit 2" = "Apt 2")

A match BEFORE checkout means no trial is offered: the person sees full billing
from day one and is never promised something they will not get. Card and
address only exist once typed into Stripe, so a match AFTER checkout ends the
trial on the spot and the first invoice is charged — the checkout page states
this rule above the button (TRIAL_DISCLOSURE).

Every completed checkout, trial or not, is recorded in trial_claims, so a
past customer who comes back on a new account does not get a trial either.

LIMITS, stated plainly: someone with a new email, a new card, a new billing
address, another network AND a clean browser gets a trial. Each of those costs
more than three free days of a $4.99 plan is worth, which is the point.
Websites cannot read a hardware device id: the device signal is a random id in
localStorage plus a cookie, which a private window or a cleared browser resets.
IP matching can refuse a stranger on a shared network (mobile carriers put many
customers behind one address); TRIAL_IP_WINDOW_DAYS limits how far back it looks.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re

import stripe


def _int_env(name, default):
    try:
        return max(0, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


TRIAL_DAYS = _int_env('TRIAL_DAYS', 3)                  # 0 turns trials off
# 0 = an IP that EVER had a trial blocks another (Bryan's call, 2026-11-20).
# Set e.g. 90 to forgive addresses a mobile carrier has since handed to someone else.
TRIAL_IP_WINDOW_DAYS = _int_env('TRIAL_IP_WINDOW_DAYS', 0)

# Serialises every trial decision, so two checkouts with the same card that
# finish at the same moment cannot both see "never used".
TRIAL_LOCK_KEY = 0x50465452

TRIAL_DISCLOSURE = (
    "Free for {days} days, then billed monthly. Cancel before the trial ends and you "
    "won't be charged. One free trial per person: if this card or billing address has "
    "been used for a PixelFlip trial before, billing starts today."
)

SIGNALS = ('card', 'address', 'email', 'device', 'ip')     # order = which one a denial names


# --------------------------------------------------------------------------- hashing
def _hash_key():
    for name in ('TRIAL_HASH_KEY', 'SECRET_KEY', 'SUPABASE_JWT_SECRET'):
        v = (os.getenv(name) or '').strip()
        if v:
            return v.encode('utf-8')
    return b'pixelflip-trial-signals'


def _h(kind, value):
    """Keyed hash of one normalised signal, or None when there is no value."""
    if not value:
        return None
    return hmac.new(_hash_key(), ('%s:%s' % (kind, value)).encode('utf-8'),
                    hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- normalisers
def normalize_email(email):
    """'J.Doe+deals@GoogleMail.com' -> 'jdoe@gmail.com'. None when not an email."""
    e = (email or '').strip().lower()
    if e.count('@') != 1:
        return None
    local, domain = e.split('@')
    if domain == 'googlemail.com':
        domain = 'gmail.com'
    local = local.split('+', 1)[0]          # sub-addressing: most providers ignore the tag
    if domain == 'gmail.com':
        local = local.replace('.', '')      # Gmail ignores dots
    if not local or '.' not in domain:
        return None
    return '%s@%s' % (local, domain)


def ip_key(ip):
    """An IPv4 address as-is; an IPv6 address as its /64 (one household or phone)."""
    try:
        addr = ipaddress.ip_address((ip or '').strip())
    except ValueError:
        return None
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network('%s/64' % addr, strict=False))
    return str(addr)


_ADDR_WORDS = {
    'street': 'st', 'avenue': 'ave', 'av': 'ave', 'road': 'rd', 'drive': 'dr',
    'boulevard': 'blvd', 'lane': 'ln', 'court': 'ct', 'place': 'pl', 'circle': 'cir',
    'highway': 'hwy', 'parkway': 'pkwy', 'terrace': 'ter', 'trail': 'trl',
    'apartment': 'apt', 'unit': 'apt', 'suite': 'apt', 'ste': 'apt', '#': 'apt',
    'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
    'northeast': 'ne', 'northwest': 'nw', 'southeast': 'se', 'southwest': 'sw',
}


def normalize_address(addr):
    """
    A billing address as one comparable string, or None without a street line.

    '123 Main Street, Apt #2' and '123 MAIN ST UNIT 2' compare equal; a US ZIP+4
    compares as its 5-digit ZIP. Units stay distinct (Apt 2 != Apt 3), so two
    flats in one building are two addresses.
    """
    a = addr or {}
    line1 = (a.get('line1') or '').strip()
    if not line1:
        return None
    country = (a.get('country') or '').strip().lower()
    postal = (a.get('postal_code') or '').strip().lower()
    if country == 'us':
        postal = postal[:5]
    text = ' '.join(x for x in (line1, a.get('line2'), a.get('city'), a.get('state'),
                                postal, country) if x).lower().replace('#', ' # ')
    tokens = [_ADDR_WORDS.get(tok, tok) for tok in re.findall(r'[a-z0-9]+|#', text)]
    out = []
    for tok in tokens:                       # 'apt apt 2' (from "Apt #2") -> 'apt 2'
        if not (tok == 'apt' and out and out[-1] == 'apt'):
            out.append(tok)
    return ' '.join(out)


_DEVICE_RE = re.compile(r'^[A-Za-z0-9-]{16,64}$')


def clean_device_id(device_id):
    d = (device_id or '').strip()
    return d if _DEVICE_RE.match(d) else None


def pre_checkout_signals(email=None, ip=None, device_id=None):
    """The hashes known before Stripe: email, IP, device. Missing ones are omitted."""
    sig = {'email': _h('email', normalize_email(email)),
           'ip': _h('ip', ip_key(ip)),
           'device': _h('device', clean_device_id(device_id))}
    return {k: v for k, v in sig.items() if v}


# --------------------------------------------------------------------------- database
# Table + columns: db_schema.TRIAL_TRACKING_DDL (migration 018).
_COLUMN = {'email': 'email_hash', 'ip': 'ip_hash', 'device': 'device_hash',
           'card': 'card_fingerprint', 'address': 'address_hash'}


def _field(row, name, idx):
    if row is None:
        return None
    return row[name] if isinstance(row, dict) else row[idx]


def account_eligible(row):
    """
    Has this ACCOUNT never had a trial and never been a customer?

    `row` is the user_settings row (dict); None = no row yet, i.e. brand new.
    """
    if TRIAL_DAYS <= 0:
        return False
    if not row:
        return True
    return not row.get('trial_used_at') and not (row.get('stripe_customer_id') or '').strip()


def prior_match(cursor, signals, exclude_subscription=None):
    """
    Which signal (if any) was already used by ANOTHER completed checkout.

    Returns a name from SIGNALS or None. The IP looks back TRIAL_IP_WINDOW_DAYS
    when that is set, otherwise forever.
    """
    present = [(k, v) for k, v in signals.items() if v and k in _COLUMN]
    if not present:
        return None
    clauses, args = [], []
    for k, v in present:
        if k == 'ip' and TRIAL_IP_WINDOW_DAYS:
            clauses.append("(ip_hash = %s AND created_at > NOW() - (%s * INTERVAL '1 day'))")
            args.extend([v, TRIAL_IP_WINDOW_DAYS])
        else:
            clauses.append('%s = %%s' % _COLUMN[k])
            args.append(v)
    cursor.execute(
        "SELECT email_hash, ip_hash, device_hash, card_fingerprint, address_hash "
        "FROM trial_claims WHERE subscription_id IS DISTINCT FROM %s AND ("
        + ' OR '.join(clauses) + ") LIMIT 20",
        [exclude_subscription] + args,
    )
    rows = cursor.fetchall() or []
    for name in SIGNALS:
        want = signals.get(name)
        if not want:
            continue
        idx = ('email', 'ip', 'device', 'card', 'address').index(name)
        if any(_field(r, _COLUMN[name], idx) == want for r in rows):
            return name
    return None


def _payment_method(sub, session):
    """The card used at checkout: the subscription's default, else the session's SetupIntent's."""
    pm = sub.get('default_payment_method')
    if pm and not isinstance(pm, str):
        return pm
    if not pm:
        si = (session or {}).get('setup_intent')
        if si:
            si_obj = si if not isinstance(si, str) else stripe.SetupIntent.retrieve(si)
            pm = si_obj.get('payment_method')
            if pm and not isinstance(pm, str):
                return pm
    return stripe.PaymentMethod.retrieve(pm) if pm else None


def _end_trial_now(sub_id):
    stripe.Subscription.modify(sub_id, trial_end='now', proration_behavior='none')


def settle_after_checkout(cursor, user_id, sub, session):
    """
    Decide a completed checkout's trial. Idempotent: the webhook and the
    dashboard's return trip both call this for the same subscription.

    Returns 'granted', 'no_trial' or 'denied:<signal>'. Raises when a TRIAL
    cannot be verified (Stripe unreachable), so the caller fails and Stripe
    retries the webhook, rather than granting a trial it could not check.
    """
    sub_id = sub.get('id')
    cursor.execute('SELECT pg_advisory_xact_lock(%s)', (TRIAL_LOCK_KEY,))
    cursor.execute('SELECT outcome FROM trial_claims WHERE subscription_id = %s', (sub_id,))
    done = cursor.fetchone()
    if done:
        return _field(done, 'outcome', 0)

    trialing = (sub.get('status') or '') == 'trialing'
    meta = (session or {}).get('metadata') or {}
    signals = {'email': meta.get('sig_email'), 'ip': meta.get('sig_ip'),
               'device': meta.get('sig_device')}
    card = {}
    try:
        pm = _payment_method(sub, session)
        card = (pm or {}).get('card') or {}
        signals['card'] = card.get('fingerprint')
        signals['address'] = _h('address', normalize_address(
            ((pm or {}).get('billing_details') or {}).get('address')))
    except Exception:
        if trialing:
            raise                      # cannot verify the card: no unchecked trial
        # A paying customer without a trial: record what we have, never block them.

    if not trialing:
        outcome = 'no_trial'
    elif card.get('funding') == 'prepaid':
        outcome = 'denied:prepaid'
    elif not signals.get('card'):
        outcome = 'denied:no_card'
    else:
        hit = prior_match(cursor, signals, exclude_subscription=sub_id)
        outcome = 'denied:%s' % hit if hit else 'granted'
    if outcome.startswith('denied:'):
        _end_trial_now(sub_id)

    cursor.execute(
        "INSERT INTO trial_claims (user_id, subscription_id, checkout_session, email_hash, "
        "ip_hash, device_hash, card_fingerprint, address_hash, outcome) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (user_id, sub_id, (session or {}).get('id'), signals.get('email'), signals.get('ip'),
         signals.get('device'), signals.get('card'), signals.get('address'), outcome),
    )
    cursor.execute(
        "UPDATE user_settings SET "
        "trial_used_at = CASE WHEN %s THEN COALESCE(trial_used_at, NOW()) ELSE trial_used_at END, "
        "trial_outcome = %s, subscription_trial_end = %s WHERE user_id = %s",
        (trialing, outcome, sub.get('trial_end') if outcome == 'granted' else None, user_id),
    )
    if outcome.startswith('denied:'):
        reason = outcome.split(':', 1)[1]
        why = {'prepaid': 'prepaid card', 'no_card': 'no card on the subscription'}.get(
            reason, '%s matched an earlier checkout' % reason)
        print('[trial] %s: trial for %s ended at checkout (%s); first invoice charged now'
              % (str(user_id)[:8], sub_id, why), flush=True)
    return outcome


def checkout_kwargs(offer, signals):
    """Extra stripe.checkout.Session.create arguments for the trial decision."""
    meta = {'trial_offered': '1' if offer else '0'}
    for k in ('email', 'ip', 'device'):
        if signals.get(k):
            meta['sig_' + k] = signals[k]
    kw = {'metadata': meta}
    if offer:
        kw.update(
            subscription_data={
                'trial_period_days': TRIAL_DAYS,
                # No card on file when the trial ends = cancel, never "free forever".
                'trial_settings': {'end_behavior': {'missing_payment_method': 'cancel'}},
            },
            payment_method_collection='always',
            # The full address is one of the repeat-trial signals.
            billing_address_collection='required',
            custom_text={'submit': {'message': TRIAL_DISCLOSURE.format(days=TRIAL_DAYS)}},
        )
    return kw


def describe(outcome):
    """User-facing sentence for the dashboard after checkout."""
    if outcome == 'granted':
        return None
    if outcome and outcome.startswith('denied:'):
        return ("Your free trial wasn't applied because this card, billing address or "
                "account has been used for a PixelFlip trial before, so your subscription "
                "started today.")
    return None


def dumps_for_log(signals):
    """Which signals were present, for a log line. Never the values."""
    return json.dumps(sorted(k for k, v in signals.items() if v))
