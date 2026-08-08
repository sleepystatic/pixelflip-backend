"""
New-listing alerts: Mailgun (email), Twilio (SMS), Web Push.

Env (Twilio): TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER (E.164, e.g. +15551234567)

Env (Mailgun — same as app.py): MAILGUN_API_KEY, MAILGUN_DOMAIN, MAILGUN_FROM_EMAIL, optional MAILGUN_BASE_URL

Env (Web Push): VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_CLAIMS_EMAIL (default: admin@pixelflip.app)

Templates (optional):
  NEW_LISTING_SMS_TEMPLATE — per listing when only one match; batch uses a short summary
  NEW_LISTING_EMAIL_SUBJECT — default: "PixelFlip: new match" (batch adds count)
  NEW_LISTING_EMAIL_TEMPLATE — per-listing block in batch emails
  NEW_LISTING_EMAIL_BATCH_HEADER — optional header line; default mentions count
  NEW_LISTING_PUSH_TITLE — default: "PixelFlip: new match"
  NEW_LISTING_PUSH_BODY — default: "{title} ${price}"
"""

import json
import os
import re
import requests

def _listing_context(listing):
    title = (listing.get('title') or '')[:500]
    price = listing.get('price')
    price_s = f'{float(price):.2f}' if price is not None and isinstance(price, (int, float)) else str(price or '')
    link = (listing.get('link') or '')[:2000]
    platform = listing.get('platform') or ''
    location = listing.get('location') or ''
    return {
        'title': title,
        'price': price_s,
        'link': link,
        'platform': platform,
        'location': location,
    }

def _format_listing_message(template, listing):
    ctx = _listing_context(listing)
    try:
        return (template or '').format(**ctx)
    except Exception:
        return (
            f"{ctx['title']}\n${ctx['price']}\n{ctx['link']}\n"
            f"{ctx['platform']}\n{ctx['location']}"
        )


def _format_batch_email_body(listings, item_template, header_template):
    n = len(listings)
    header = (header_template or '').format(count=n)
    if not header:
        header = f'PixelFlip found {n} new listing{"s" if n != 1 else ""} this scan:\n'
    blocks = []
    for i, listing in enumerate(listings, 1):
        block = _format_listing_message(item_template, listing).strip()
        blocks.append(f'--- Match {i} of {n} ---\n{block}')
    return header.rstrip() + '\n\n' + '\n\n'.join(blocks)


def _format_batch_sms_body(listings, item_template):
    n = len(listings)
    if n == 1:
        return _format_listing_message(item_template, listings[0])
    lines = [f'PixelFlip: {n} new matches']
    max_lines = 8
    for listing in listings[:max_lines]:
        ctx = _listing_context(listing)
        lines.append(f"• {ctx['title'][:48]} ${ctx['price']}")
        lines.append(ctx['link'][:120])
    if n > max_lines:
        lines.append(f'…and {n - max_lines} more in the app.')
    body = '\n'.join(lines)
    return body[:1600]


def send_twilio_sms(to_number, body):
    account_sid = (os.getenv('TWILIO_ACCOUNT_SID') or '').strip()
    auth_token = (os.getenv('TWILIO_AUTH_TOKEN') or '').strip()
    from_num = (os.getenv('TWILIO_FROM_NUMBER') or '').strip()
    if not (account_sid and auth_token and from_num and to_number and body):
        return False, 'Twilio env or destination incomplete'
    url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json'
    r = requests.post(
        url,
        auth=(account_sid, auth_token),
        data={
            'From': from_num,
            'To': to_number.strip(),
            'Body': body[:1600],
        },
        timeout=25,
    )
    if r.status_code >= 300:
        return False, (r.text or '')[:300]
    return True, None


def send_mailgun_email(to_email, subject, text_body, html_body=None, unsubscribe_url=None):
    """
    Send via Mailgun. Always includes the text part; adds the HTML part when given
    so clients pick whichever they can render.

    `unsubscribe_url` adds List-Unsubscribe headers. Gmail and Yahoo require
    one-click unsubscribe for bulk senders, and having it materially improves
    inbox placement — the header is what puts the native "Unsubscribe" control
    next to the sender name.
    """
    mailgun_api_key = os.getenv('MAILGUN_API_KEY')
    mailgun_domain = os.getenv('MAILGUN_DOMAIN')
    mailgun_from = os.getenv('MAILGUN_FROM_EMAIL')
    mailgun_base_url = os.getenv('MAILGUN_BASE_URL', 'https://api.mailgun.net')
    if not all([mailgun_api_key, mailgun_domain, mailgun_from, to_email]):
        return False, 'Mailgun env or email incomplete'
    endpoint = f"{mailgun_base_url.rstrip('/')}/v3/{mailgun_domain}/messages"
    payload = {
        'from': mailgun_from,
        'to': [to_email],
        'subject': subject[:998],
        'text': text_body[:50000],
    }
    if html_body:
        payload['html'] = html_body[:200000]
    if unsubscribe_url:
        payload['h:List-Unsubscribe'] = f'<{unsubscribe_url}>'
        payload['h:List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
    r = requests.post(
        endpoint,
        auth=('api', mailgun_api_key),
        data=payload,
        timeout=25,
    )
    if r.status_code >= 300:
        try:
            payload = r.json()
            msg = payload.get('message') or payload.get('error') or r.text
        except Exception:
            msg = r.text
        return False, str(msg)[:400]
    return True, None


def normalize_sms_destination(raw_phone):
    """Best-effort E.164 for US if user stored 10 digits without +."""
    if not raw_phone:
        return None
    s = str(raw_phone).strip()
    if s.startswith('+'):
        return s
    digits = re.sub(r'\D', '', s)
    if len(digits) == 10:
        return '+1' + digits
    if len(digits) == 11 and digits.startswith('1'):
        return '+' + digits
    if len(digits) >= 10:
        return '+' + digits
    return None


def send_web_push(subscription, title, body, url=None):
    """
    Send web push notification using pywebpush.
    subscription: dict with endpoint, keys (p256dh, auth)
    Returns (success: bool, error_message: str or None)
    """
    vapid_private = (os.getenv('VAPID_PRIVATE_KEY') or '').strip()
    vapid_claims_email = (os.getenv('VAPID_CLAIMS_EMAIL') or 'admin@pixelflip.app').strip()
    if not vapid_private:
        return False, 'VAPID_PRIVATE_KEY not configured'
    if not subscription or not subscription.get('endpoint'):
        return False, 'Invalid subscription'

    try:
        from pywebpush import webpush, WebPushException
        payload = json.dumps({
            'title': title,
            'body': body,
            'url': url or '/',
            'icon': '/apple_touch_icon.png',
            'badge': '/favicon-32x32.png',
        })
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=vapid_private,
            vapid_claims={'sub': f'mailto:{vapid_claims_email}'},
            ttl=3600,
        )
        return True, None
    except WebPushException as e:
        return False, str(e)[:200]
    except Exception as e:
        return False, str(e)[:200]


def dispatch_scrape_digest_notifications(user_id, listings, prefs, user_email=None, push_sub=None):
    """
    One email and/or one SMS and/or one Web Push per scrape when new listings were saved.
    `listings` is a non-empty list of listing dicts from the same scan.
    `push_sub`: optional push subscription dict (endpoint, keys) from user_settings
    """
    if not listings:
        return []

    ch = prefs.get('notification_channels') or {}
    phone_raw = (prefs.get('contact_phone') or '').strip()
    to_sms = normalize_sms_destination(phone_raw) if ch.get('sms') else None

    sms_template = os.getenv(
        'NEW_LISTING_SMS_TEMPLATE',
        'PixelFlip: {title} ${price}\n{link}',
    )
    email_subject_single = os.getenv('NEW_LISTING_EMAIL_SUBJECT', 'PixelFlip: new match')
    email_item_template = os.getenv(
        'NEW_LISTING_EMAIL_TEMPLATE',
        'New listing match\n\n'
        'Title: {title}\n'
        'Price: ${price}\n'
        'Platform: {platform}\n'
        'Location: {location}\n'
        'Link: {link}\n',
    )
    email_batch_header = os.getenv(
        'NEW_LISTING_EMAIL_BATCH_HEADER',
        'PixelFlip found {count} new listings from your latest scan:\n',
    )
    push_title_template = os.getenv('NEW_LISTING_PUSH_TITLE', 'PixelFlip: new match')
    push_body_template = os.getenv('NEW_LISTING_PUSH_BODY', '{title} ${price}')

    n = len(listings)
    if n == 1:
        email_subject = email_subject_single
        push_title = push_title_template
        first = listings[0]
        push_body = _format_listing_message(push_body_template, first)
        push_url = first.get('link', '/')
    else:
        email_subject = f'PixelFlip: {n} new matches'
        push_title = f'PixelFlip: {n} new matches'
        push_body = f'Found {n} new listings matching your search'
        push_url = '/'

    errors = []

    if ch.get('sms'):
        if not to_sms:
            errors.append('SMS enabled but no valid phone')
        else:
            body = _format_batch_sms_body(listings, sms_template)
            ok, err = send_twilio_sms(to_sms, body)
            if not ok:
                errors.append(f'SMS: {err}')

    if ch.get('email'):
        to_mail = (user_email or '').strip()
        if not to_mail:
            errors.append('Email enabled but no user email')
        else:
            try:
                from email_templates import build_listing_digest_email, build_unsubscribe_url
                subject_html, html_body, text_body = build_listing_digest_email(
                    listings, user_id, user_email=to_mail,
                )
                ok, err = send_mailgun_email(
                    to_mail, subject_html, text_body,
                    html_body=html_body,
                    unsubscribe_url=build_unsubscribe_url(user_id),
                )
            except Exception as e:
                # Never lose an alert to a template bug — fall back to plain text.
                print(f'[email] HTML template failed ({e}); sending plain text', flush=True)
                text_body = _format_batch_email_body(listings, email_item_template, email_batch_header)
                ok, err = send_mailgun_email(to_mail, email_subject, text_body)
            if not ok:
                errors.append(f'Email: {err}')

    if ch.get('push') and push_sub:
        ok, err = send_web_push(push_sub, push_title, push_body, push_url)
        if not ok:
            errors.append(f'Push: {err}')

    return errors


def dispatch_new_listing_notifications(user_id, listing, prefs, user_email=None):
    """Backward-compatible wrapper: one listing → one digest of size 1."""
    return dispatch_scrape_digest_notifications(user_id, [listing], prefs, user_email=user_email)
