"""
HTML email templates for PixelFlip listing alerts.

Every builder returns (subject, html_body, text_body). Always send both parts:
a text alternative measurably improves inbox placement, and some clients
(watches, screen readers, plain-text preferences) never render the HTML.

Design notes for anyone editing the markup below:
  * Layout is <table>-based and CSS is inlined. Outlook renders with Word's
    HTML engine, which ignores float/flex/grid and most <style> rules.
  * Max width 600px — the widest that fits every common preview pane.
  * Images always carry alt text and explicit width; many clients block images
    by default, so the email must read correctly with none of them loaded.
"""

import hashlib
import hmac
import ipaddress
import os
from urllib.parse import quote, urlencode, urlparse

BRAND_PRIMARY = '#764ba2'
BRAND_SECONDARY = '#667eea'
BRAND_DARK = '#2D3748'
BRAND_LIGHT = '#F7FAFC'
FONT_STACK = "'SF Mono', SFMono-Regular, Consolas, 'Liberation Mono', Menlo, monospace"


_LOCAL_HOSTNAMES = {'localhost', '0.0.0.0', '::1'}


def _public_base_url(value):
    """
    Return `value` only if a stranger's inbox could actually reach it.

    Dev values leak into outgoing mail constantly: the same .env drives local
    runs and the deploy, so BACKEND_PUBLIC_URL=http://localhost:5000 silently
    ships an unsubscribe link that is dead for every recipient — which is both a
    CAN-SPAM problem and a spam-filter signal. Treat anything pointing at this
    machine or a private LAN as unset so the public default wins instead.
    """
    url = (value or '').strip().rstrip('/')
    if not url:
        return None
    host = (urlparse(url).hostname or '').lower()
    if not host or host in _LOCAL_HOSTNAMES or host.endswith('.local'):
        return None
    try:
        # Covers loopback (127/8) and RFC-1918, i.e. the phone-testing case.
        if ipaddress.ip_address(host).is_private:
            return None
    except ValueError:
        pass  # a real hostname rather than a literal IP
    return url


def _app_url():
    """Where the user's dashboard lives (the React frontend)."""
    # The fallback must be a host that exists: DNS has `dashboard`, not `app`.
    # A forgotten FRONTEND_URL should degrade to a working link, not a dead one.
    return _public_base_url(os.getenv('FRONTEND_URL')) or 'https://dashboard.pixelflip.app'


def _api_url():
    """
    Public base URL of the Flask backend, which is what actually serves
    /unsubscribe. This is NOT the same host as the frontend — pointing the
    unsubscribe link at FRONTEND_URL sends recipients to a route the React app
    does not have, i.e. a dead unsubscribe link.
    """
    return (
        _public_base_url(os.getenv('BACKEND_PUBLIC_URL'))
        or _public_base_url(os.getenv('PUBLIC_API_URL'))
        or _public_base_url(os.getenv('FRONTEND_URL'))
        or 'https://api.pixelflip.app'
    )


def _company_address():
    """CAN-SPAM requires a real physical postal address in every commercial email."""
    return (os.getenv('COMPANY_POSTAL_ADDRESS') or '').strip()


def make_unsubscribe_token(user_id):
    """
    Signed token so an unsubscribe link works from the email without a login.

    Uses the app's existing secret. One-click unsubscribe must NOT require the
    recipient to authenticate — both CAN-SPAM and Gmail/Yahoo bulk-sender rules
    expect it to work in a single step.
    """
    secret = (os.getenv('SUPABASE_JWT_SECRET') or os.getenv('SECRET_KEY') or '').encode()
    return hmac.new(secret, str(user_id).encode(), hashlib.sha256).hexdigest()[:32]


def build_unsubscribe_url(user_id):
    q = urlencode({'uid': str(user_id), 'token': make_unsubscribe_token(user_id)})
    return f'{_api_url()}/unsubscribe?{q}'


def _esc(value):
    return (
        str(value or '')
        .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def _fmt_price(price):
    try:
        return f'${float(price):,.0f}'
    except (TypeError, ValueError):
        return str(price or '—')


def _listing_card(listing):
    title = _esc(listing.get('title'))[:140]
    price = _fmt_price(listing.get('price'))
    link = _esc(listing.get('link'))
    platform = _esc(listing.get('platform'))
    location = _esc(listing.get('location') or '')
    image = _esc(listing.get('image_url') or '')

    image_cell = ''
    if image:
        image_cell = f'''
              <td width="96" valign="top" style="padding-right:16px;">
                <a href="{link}" style="text-decoration:none;">
                  <img src="{image}" alt="{title}" width="96" height="96"
                       style="width:96px;height:96px;object-fit:cover;border-radius:8px;
                              border:1px solid #E2E8F0;display:block;" />
                </a>
              </td>'''

    meta = ' · '.join(p for p in (platform, location) if p)

    return f'''
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
             style="margin-bottom:14px;background:#FFFFFF;border:1px solid #E2E8F0;border-radius:10px;">
        <tr>
          <td style="padding:16px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
              <tr>{image_cell}
                <td valign="top">
                  <div style="font-family:{FONT_STACK};font-size:15px;line-height:1.4;
                              font-weight:600;color:{BRAND_DARK};margin-bottom:6px;">
                    <a href="{link}" style="color:{BRAND_DARK};text-decoration:none;">{title}</a>
                  </div>
                  <div style="font-family:{FONT_STACK};font-size:20px;font-weight:700;
                              color:{BRAND_PRIMARY};margin-bottom:6px;">{price}</div>
                  <div style="font-family:{FONT_STACK};font-size:12px;color:#718096;
                              margin-bottom:12px;">{meta}</div>
                  <a href="{link}"
                     style="display:inline-block;background:{BRAND_PRIMARY};color:#FFFFFF;
                            font-family:{FONT_STACK};font-size:13px;font-weight:600;
                            text-decoration:none;padding:9px 18px;border-radius:6px;">
                    View listing &rarr;
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>'''


def build_listing_digest_email(listings, user_id, user_email=None):
    """Build the new-match digest. Returns (subject, html, text)."""
    n = len(listings)
    plural = '' if n == 1 else 'es'
    subject = f'PixelFlip: {n} new match{plural}'

    app_url = _app_url()
    unsub_url = build_unsubscribe_url(user_id)
    prefs_url = f'{app_url}/settings'
    address = _company_address()

    # Preheader: the grey preview line clients show after the subject. Padding
    # characters stop the client from pulling in footer text instead.
    cheapest = min(
        (l for l in listings if isinstance(l.get('price'), (int, float))),
        key=lambda l: l['price'], default=None,
    )
    preheader = (
        f"Best find: {_esc(cheapest.get('title'))[:60]} at {_fmt_price(cheapest.get('price'))}"
        if cheapest else 'Fresh matches from your saved searches.'
    )

    cards = ''.join(_listing_card(l) for l in listings)

    address_block = (
        f'<div style="margin-top:10px;color:#A0AEC0;">{_esc(address)}</div>'
        if address else ''
    )

    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="color-scheme" content="light" />
<title>{_esc(subject)}</title>
</head>
<body style="margin:0;padding:0;background:{BRAND_LIGHT};">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">
    {preheader}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
  </div>

  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
         style="background:{BRAND_LIGHT};padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600"
               style="width:100%;max-width:600px;">

          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,{BRAND_SECONDARY} 0%,{BRAND_PRIMARY} 100%);
                       background-color:{BRAND_PRIMARY};border-radius:12px 12px 0 0;padding:26px 24px;">
              <div style="font-family:{FONT_STACK};font-size:21px;font-weight:700;
                          color:#FFFFFF;letter-spacing:-0.4px;">PixelFlip</div>
              <div style="font-family:{FONT_STACK};font-size:13px;color:rgba(255,255,255,0.88);
                          margin-top:5px;">
                {n} new match{plural} from your saved searches
              </div>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="background:#FFFFFF;padding:24px;">
              {cards}
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td align="center" style="padding-top:6px;">
                    <a href="{app_url}"
                       style="display:inline-block;background:{BRAND_DARK};color:#FFFFFF;
                              font-family:{FONT_STACK};font-size:14px;font-weight:600;
                              text-decoration:none;padding:13px 30px;border-radius:8px;">
                      Open dashboard
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#FFFFFF;border-radius:0 0 12px 12px;border-top:1px solid #E2E8F0;
                       padding:20px 24px 26px;font-family:{FONT_STACK};font-size:12px;
                       line-height:1.6;color:#718096;text-align:center;">
              <div>You're receiving this because you enabled listing alerts on PixelFlip.</div>
              <div style="margin-top:14px;">
                <a href="{prefs_url}" style="color:{BRAND_SECONDARY};text-decoration:underline;">
                  Manage alert preferences
                </a>
                &nbsp;&nbsp;|&nbsp;&nbsp;
                <a href="{unsub_url}"
                   style="color:#718096;text-decoration:underline;">Unsubscribe</a>
              </div>
              {address_block}
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>'''

    lines = [f'PixelFlip — {n} new match{plural} from your saved searches', '']
    for i, l in enumerate(listings, 1):
        meta = ' · '.join(p for p in (l.get('platform') or '', l.get('location') or '') if p)
        lines += [
            f"{i}. {l.get('title') or ''}",
            f"   {_fmt_price(l.get('price'))}{('  ' + meta) if meta else ''}",
            f"   {l.get('link') or ''}",
            '',
        ]
    lines += [
        f'Open your dashboard: {app_url}',
        '',
        "You're receiving this because you enabled email alerts on PixelFlip.",
        f'Manage preferences: {prefs_url}',
        f'Unsubscribe: {unsub_url}',
    ]
    if address:
        lines += ['', address]

    return subject, html, '\n'.join(lines)
