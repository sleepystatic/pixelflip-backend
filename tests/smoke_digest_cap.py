# -*- coding: utf-8 -*-
"""
Email digest: capped, and honest about a first scan (handoff 1.3).

Bryan received ONE email containing 187 listings six minutes after arming the
scanner. SMS had capped itself at 8 since it was written; BOTH email paths — the
HTML digest and the plain-text fallback — rendered every match. These tests use
187 so the exact reported case is the one under test.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap

bootstrap()
import email_templates as ET
import listing_notifications as LN
import scraper_multi_user as S

t = Suite('digest-cap')

N = 187
LISTINGS = [{'title': 'Nintendo DS Lite #%d' % i, 'price': 20.0 + i,
             'link': 'https://example.invalid/i/%d' % i,
             'platform': 'Facebook', 'location': 'Stockton, CA'}
            for i in range(N)]
UID = 'digest-test-user'

os.environ.pop('NOTIFY_EMAIL_MAX_ITEMS', None)

t.section('1. The reported case: 187 listings in one email')
t.eq(ET.digest_max_items(), 20, 'default cap is 20 items')
subject, html, text = ET.build_listing_digest_email(LISTINGS, UID)

def distinct_listings(doc):
    """How many DIFFERENT listings appear. Each card repeats its link in two
    anchors, so counting raw occurrences double-counts."""
    return len(set(re.findall(r'example\.invalid/i/(\d+)', doc)))


t.eq(distinct_listings(html), 20, 'HTML renders 20 listing cards, not 187')
t.ok('and 167 more' in html, 'HTML says how many were withheld')

text_items = len(re.findall(r'^\s*\d+\. Nintendo', text, re.M))
t.eq(text_items, 20, 'plain text renders 20 entries, not 187')
t.ok('... and 167 more in the app.' in text, 'plain text says how many were withheld')
t.ok(str(N) in subject, 'the subject still reports the TRUE total (187)')
print("  subject: %r" % subject)

t.section('2. A normal-sized digest is untouched')
small = LISTINGS[:5]
subject_s, html_s, text_s = ET.build_listing_digest_email(small, UID)
t.eq(distinct_listings(html_s), 5, 'all 5 cards render')
t.ok('more in the app' not in html_s, 'no overflow note when nothing overflowed')
t.ok('more in the app' not in text_s, 'plain text has no overflow note either')

one = LISTINGS[:1]
subject_1, html_1, text_1 = ET.build_listing_digest_email(one, UID)
t.ok('1 new match' in subject_1, 'a single match still reads naturally (no plural)')
t.ok('more in the app' not in html_1, 'single match has no overflow note')

t.section('3. First-scan framing')
subj_f, html_f, text_f = ET.build_listing_digest_email(LISTINGS, UID, first_scan=True)
t.ok('first scan' in subj_f.lower(), 'subject names it as the first scan')
t.ok(str(N) in subj_f, 'and still reports the true total')
t.ok('first scan matched 187' in html_f, 'body explains the size')
# The half that matters: without it, the user has no reason to expect anything
# different tomorrow.
t.ok('only hear about genuinely new' in html_f,
     'body promises it will not repeat')
t.ok('first scan' in text_f.lower(), 'plain text carries the same framing')
t.eq(distinct_listings(html_f), 20, 'first scan is still capped')
print("  first-scan subject: %r" % subj_f)

# A normal scan must NOT claim to be a first scan.
t.ok('first scan' not in html.lower(), 'a normal digest never mentions a first scan')
t.ok('first scan' not in subject.lower(), 'nor does its subject')

t.section('4. The cap is configurable, and the fallback obeys it too')
os.environ['NOTIFY_EMAIL_MAX_ITEMS'] = '5'
t.eq(ET.digest_max_items(), 5, 'env var honoured')
_, html5, text5 = ET.build_listing_digest_email(LISTINGS, UID)
t.eq(distinct_listings(html5), 5, 'HTML honours the env cap')
t.ok('and 182 more' in html5, 'overflow count follows the cap')
for bad in ('0', '-3', 'abc', ''):
    os.environ['NOTIFY_EMAIL_MAX_ITEMS'] = bad
    t.ok(ET.digest_max_items() >= 1, 'cap=%r never drops below 1' % bad)
os.environ.pop('NOTIFY_EMAIL_MAX_ITEMS', None)

# The plain-text fallback is what sends when the HTML template raises — an
# uncapped fallback would reintroduce the flood exactly when something is
# already broken.
body = LN._format_batch_email_body(LISTINGS, '{title} ${price}\n{link}', '')
t.eq(len(re.findall(r'--- Match \d+ of 187 ---', body)), 20,
     'fallback body is capped at 20 blocks')
t.ok('... and 167 more in the app.' in body, 'fallback says how many were withheld')

t.section('5. first_scan is plumbed from the scraper, not invented downstream')
import inspect
t.ok('first_scan' in inspect.signature(LN.dispatch_scrape_digest_notifications).parameters,
     'dispatch accepts first_scan')
t.ok('first_scan' in inspect.signature(S._notify_scrape_digest).parameters,
     '_notify_scrape_digest accepts first_scan')
t.eq(inspect.signature(LN.dispatch_scrape_digest_notifications)
     .parameters['first_scan'].default, False, 'and defaults to False')
# The scraper derives it from seen_listings being empty.
src = inspect.getsource(S._scrape_for_user_impl)
t.ok('first_scan=not seen_listings' in src,
     'the scraper derives first_scan from an empty seen_listings')

captured = {}
_real = S.dispatch_scrape_digest_notifications if hasattr(S, 'dispatch_scrape_digest_notifications') else None


def fake_dispatch(user_id, listings, prefs, user_email=None, push_sub=None, first_scan=False):
    captured['first_scan'] = first_scan
    captured['n'] = len(listings)
    return []


import listing_notifications
_orig = listing_notifications.dispatch_scrape_digest_notifications
_orig_email = S.get_user_auth_email
listing_notifications.dispatch_scrape_digest_notifications = fake_dispatch
S.get_user_auth_email = lambda uid: 'x@example.invalid'
try:
    S._notify_scrape_digest(UID, LISTINGS, {'notification_channels': {'email': True}},
                            None, first_scan=True)
    t.eq(captured.get('first_scan'), True, 'first_scan reaches the dispatcher')
    captured.clear()
    S._notify_scrape_digest(UID, LISTINGS, {'notification_channels': {'email': True}},
                            None)
    t.eq(captured.get('first_scan'), False, 'and defaults to False when not a first scan')
finally:
    listing_notifications.dispatch_scrape_digest_notifications = _orig
    S.get_user_auth_email = _orig_email

t.done()
