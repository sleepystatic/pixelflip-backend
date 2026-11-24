# -*- coding: utf-8 -*-
"""
OfferUp description scanning — offline, over a fake curl_cffi session.

SINCE 2026-11-23 descriptions come from OfferUp's listing-details API: the
GetListingDetailByListingId request offerup.com's own logged-out page sends when
a visitor clicks a search result (observed in a real browser that day: HTTP 200,
JSON, description + postDate). The item pages this suite used to parse have
been Cloudflare-walled since ~2026-10; that path is now an opt-in fallback
(OFFERUP_ITEM_PAGES=1) and section 5 runs only if its fixtures exist.

  1. the request: the site's own operation, trimmed to listingId/description/
     postDate (no seller profile), direct connection, the search's warm session,
     process-wide OfferUp pacing, one lookup per listing (cache)
  2. failure modes: 403 drops the session and counts toward a breaker of its
     OWN (2 refusals -> stop for 1h); a reply about another listing fails
     closed; GraphQL errors / junk fail; a refusal is never cached; a blank
     body is an answer (''), not a failure; OFFERUP_LISTING_API=0 = no request
  3. _offerup_item_details on that path: one request for date+body, the date
     rides along free, blank body is '', refused + item pages off = no fetch
  4. END TO END through scrape_offerup_for_user: a body-only include keeps the
     listing, distance is checked BEFORE a lookup, no keywords / feature off /
     title-excluded = zero lookups, a listing dropped on its body is not looked
     up again next scan, the real post date feeds the age filter, the console
     says plainly when lookups are refused or switched off, and past the
     per-scan cap (OFFERUP_DESC_LOOKUPS_PER_SCAN, 10) a listing WAITS for the
     next scan instead of being decided on its title

The fixture (fixtures/offerup_listing_graphql.json) has the shape of the
observed reply to the trimmed query; its text is synthetic.
"""
import io
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Suite, bootstrap, fixture, fixture_glob

bootstrap()
import scraper_multi_user as S
import curl_cffi.requests as cffi_requests

t = Suite('offerup-description')
raw_fixture = fixture('offerup_listing_graphql.json')
if not raw_fixture:
    t.skip('fixtures/offerup_listing_graphql.json missing')
FIX = json.loads(raw_fixture)
ID_A = FIX['data']['listing']['listingId']
ID_B = '0f9e8d7c-6b5a-3c4d-8e9f-a1b2c3d4e5f6'
ID_FAR = '11111111-2222-3333-4444-555555555555'

os.environ.update({'OFFERUP_TERM_DELAY_SEC': '0', 'OFFERUP_ITEM_PAGE_DELAY_SEC': '0',
                   'SEARCH_CACHE_ENABLED': '0', 'MAX_LISTING_AGE_DAYS': '3650'})
for k in ('OFFERUP_ITEM_PAGES', 'OFFERUP_GRAPHQL_NO_PROXY', 'OFFERUP_LISTING_WALL_COOLDOWN_SEC',
          'OFFERUP_ITEM_WALL_FAILS', 'OFFERUP_DETAIL_CACHE_TTL_SEC', 'OFFERUP_FETCH_ITEM_POSTED'):
    os.environ.pop(k, None)


# ------------------------------------------------------------------ fake OfferUp
class FakeResp(object):
    def __init__(self, status, content):
        self.status_code = status
        self.content = content if isinstance(content, bytes) else content.encode('utf-8')
        self.text = self.content.decode('utf-8', 'replace')


def reply(lid, desc, post=None):
    return json.dumps({'data': {'listing': {'listingId': lid, 'description': desc,
                                            'postDate': post}}}).encode('utf-8')


NOW_ISO = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
REPLIES = {}
DEFAULT = {'status': None}           # e.g. 403 to refuse everything
POSTS, GETS = [], []
COUNT = {'sessions': 0, 'warm': 0}


class FakeSession(object):
    def __init__(self, *a, **k):
        COUNT['sessions'] += 1
        self.warm = False

    def get(self, url, **k):
        COUNT['warm'] += 1
        self.warm = True
        return FakeResp(200, b'User-agent: *\n')

    def post(self, url, json=None, proxies=None, headers=None, **k):
        assert self.warm, 'POST without the warm-up cookie'
        POSTS.append({'url': url, 'body': json, 'proxies': proxies, 'headers': headers or {}})
        if DEFAULT['status']:
            return FakeResp(DEFAULT['status'], b'<!doctype html><title>Access blocked | OfferUp</title>')
        lid = (json or {}).get('variables', {}).get('listingId')
        status, body = REPLIES.get(lid, (200, reply(lid, 'Generic body text.', NOW_ISO)))
        return FakeResp(status, body)

    def close(self):
        pass


cffi_requests.Session = FakeSession
cffi_requests.get = lambda url, **k: GETS.append(url) or FakeResp(403, b'walled')

_real_paced = S._paced
PACED = []


@contextmanager
def rec_paced(platform, cached=None):
    PACED.append(platform)
    with _real_paced(platform, cached=cached) as slot:
        yield slot


S._paced = rec_paced


def reset():
    S._OU_DETAIL_CACHE.clear()
    S._OU_LISTING_WALL.update(fails=0, until=0.0)
    S._OU_ITEM_WALL.update(fails=0, until=0.0)
    S._offerup_drop_curl_session()
    REPLIES.clear()
    REPLIES[ID_A] = (200, raw_fixture.encode('utf-8'))
    DEFAULT['status'] = None
    del POSTS[:], GETS[:], PACED[:]
    COUNT.update(sessions=0, warm=0)
    os.environ['OFFERUP_LISTING_API'] = '1'


# =============================================================================
t.section('1. The request: OfferUp\'s own operation, trimmed, paced, cached')
reset()
ok, post_raw, desc = S._offerup_listing_via_graphql(ID_A)
t.eq(ok, True, 'answered')
t.ok(isinstance(desc, str) and desc.startswith('Works great'), 'description is the plain string')
t.eq(post_raw, '2026-11-23T23:08:27.211Z', 'postDate passed through raw')
t.eq(len(POSTS), 1, 'exactly one request')
p = POSTS[0]
t.eq(p['url'], 'https://offerup.com/api/graphql', 'the same endpoint as the search')
t.eq(p['body'].get('operationName'), 'GetListingDetailByListingId', "the site's own operation name")
t.eq(p['body'].get('variables'), {'listingId': ID_A}, 'variables: the listing id only')
q = p['body'].get('query') or ''
fields = set(re.findall(r'^\s+(\w+)\s*$', q, re.M))
t.eq(fields, {'listingId', 'description', 'postDate'}, 'selects ONLY id, description and postDate')
t.ok(not any(w in q for w in ('owner', 'profile', 'phone', 'email', 'merchant')),
     'asks for nothing about the seller')
t.eq(p['proxies'], None, 'direct connection (OFFERUP_GRAPHQL_NO_PROXY default), like the search')
t.eq(p['headers'].get('origin'), 'https://offerup.com', 'browser-like origin header')
t.eq(PACED, ['offerup'], "takes a turn in OfferUp's process-wide pacing")

S._offerup_listing_via_graphql(ID_A)
t.eq(len(POSTS), 1, 'same listing again -> served from the cache, no request')
t.eq(PACED, ['offerup'], '...and no pacing turn spent on a cache hit')
S._offerup_listing_via_graphql(ID_B)
t.eq(len(POSTS), 2, 'another listing -> one more request')
t.eq((COUNT['sessions'], COUNT['warm']), (1, 1), 'the warm session is reused (one warm-up)')

# =============================================================================
t.section('2. Failure modes and the breaker')
reset()
REPLIES['x1'] = (403, b'<title>Access blocked | OfferUp</title>')
t.eq(S._offerup_listing_via_graphql('x1'), (False, None, None), '403 -> no answer')
t.eq(getattr(S._OU_SESSION_TL, 'state', None), None, '...and the session is dropped (re-warm next time)')
t.ok('x1' not in S._OU_DETAIL_CACHE, '...and the refusal is NOT cached')
t.ok(not S._ou_item_wall_open(S._OU_LISTING_WALL), 'one refusal does not open the breaker')
REPLIES['x2'] = (403, b'blocked')
S._offerup_listing_via_graphql('x2')
t.ok(S._ou_item_wall_open(S._OU_LISTING_WALL), 'two refusals in a row open it')
t.ok(not S._ou_item_wall_open(), "...the item-page breaker is separate and stays shut")
n = len(POSTS)
t.eq(S._offerup_listing_via_graphql(ID_B), (False, None, None), 'breaker open -> no answer')
t.eq(len(POSTS), n, '...and NO request')
t.ok(3000 <= S._OU_LISTING_WALL['until'] - __import__('time').time() <= 3700,
     'cooldown is about an hour (OFFERUP_LISTING_WALL_COOLDOWN_SEC default)')
S._OU_LISTING_WALL['until'] = 0.0
ok, _, _ = S._offerup_listing_via_graphql(ID_B)
t.ok(ok and S._OU_LISTING_WALL['fails'] == 0, 'after the cooldown one success closes it')

reset()
REPLIES['mine'] = (200, reply('someone-else', 'A stranger\'s text', NOW_ISO))
t.eq(S._offerup_listing_via_graphql('mine'), (False, None, None),
     'a reply about ANOTHER listing fails closed')
t.ok('mine' not in S._OU_DETAIL_CACHE, '...and is not cached')
REPLIES['err'] = (200, json.dumps({'errors': [{'message': 'Cannot query field "postDate"'}],
                                   'data': None}).encode())
t.eq(S._offerup_listing_via_graphql('err')[0], False, 'GraphQL errors (a schema change) -> no answer')
t.ok(S._ou_item_wall_open(S._OU_LISTING_WALL), '...and they count toward the breaker')
reset()
REPLIES['junk'] = (200, b'<html>not json</html>')
t.eq(S._offerup_listing_via_graphql('junk')[0], False, 'unparseable 200 -> no answer')
REPLIES['blank'] = (200, reply('blank', '   ', NOW_ISO))
t.eq(S._offerup_listing_via_graphql('blank'), (True, NOW_ISO, ''),
     'a blank body is an ANSWER (the seller wrote nothing), returned as ""')
t.ok('blank' in S._OU_DETAIL_CACHE, '...and cached')
reset()
os.environ['OFFERUP_LISTING_API'] = '0'
t.eq(S._offerup_listing_via_graphql(ID_A), (False, None, None), 'OFFERUP_LISTING_API=0 -> no answer')
t.eq(len(POSTS) + COUNT['warm'], 0, '...and not a single request')

# =============================================================================
t.section('3. _offerup_item_details on the API path')
reset()
link_a = 'https://offerup.com/item/detail/%s' % ID_A
at, body = S._offerup_item_details(link_a, 0, want_posted=True, want_description=True)
t.ok(isinstance(at, datetime) and at.tzinfo is not None, 'date+body: aware datetime')
t.eq(at, datetime(2026, 11, 23, 23, 8, 27, 211000, tzinfo=timezone.utc), '...the listing\'s real postDate')
t.ok(isinstance(body, str) and 'cobalt' in body, '...and its body')
t.eq(len(POSTS), 1, 'both fields cost ONE request')
reset()
at, body = S._offerup_item_details(link_a, 0, want_posted=True)
t.ok(isinstance(at, datetime) and body is None, 'date only -> date, no body')
reset()
at, body = S._offerup_item_details(link_a, 0, want_description=True)
t.ok(isinstance(at, datetime), 'body only -> the date still rides along free')
t.ok(body and 'cobalt' in body, '...with the body')
reset()
REPLIES['blank'] = (200, reply('blank', '', NOW_ISO))
t.eq(S._offerup_item_details('https://offerup.com/item/detail/blank', 0, want_description=True)[1], '',
     'blank body -> "" (read, empty), not None (unavailable)')
reset()
DEFAULT['status'] = 403
t.eq(S._offerup_item_details(link_a, 0, want_description=True), (None, None),
     'API refused + item pages off -> (None, None)')
t.eq(GETS, [], '...and the walled item page is NOT requested')
reset()
t.eq(S._offerup_item_details('https://offerup.com/nope', 0, want_description=True), (None, None),
     'non-item link -> (None, None)')
t.eq(S._offerup_item_details(link_a, 0), (None, None), 'neither wanted -> (None, None)')
t.eq(len(POSTS), 0, '...with no request')

# =============================================================================
t.section('4. END TO END: scrape_offerup_for_user')
OLD_ISO = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
ITEMS = [
    {'title': 'Nintendo Switch OLED bundle', 'price': 150.0, 'image_url': None,
     'link': 'https://offerup.com/item/detail/%s' % ID_A, 'city': 'Stockton, CA'},
    {'title': 'Nintendo Switch Lite handheld', 'price': 150.0, 'image_url': None,
     'link': 'https://offerup.com/item/detail/%s' % ID_B, 'city': 'Stockton, CA'},
    {'title': 'Nintendo Switch far away', 'price': 150.0, 'image_url': None,
     'link': 'https://offerup.com/item/detail/%s' % ID_FAR, 'city': 'Redding, CA'},
]
COORDS = {'Stockton, CA': (37.96, -121.29), 'Redding, CA': (40.59, -122.39)}
LOG = []
_o = {}


def patch():
    _o.update(gql=S._offerup_fetch_via_graphql, ai=S.check_image_with_ai, act=S.is_user_active,
              zip=S._zip_to_latlon, city=S.city_to_latlon)
    S._offerup_fetch_via_graphql = lambda *a, **k: ([dict(i) for i in ITEMS], None)
    S.check_image_with_ai = lambda *a, **k: True
    S.is_user_active = lambda *a, **k: True
    S._zip_to_latlon = lambda *a, **k: (38.0, -121.26)
    S.city_to_latlon = lambda city, *a, **k: COORDS.get(city)


def unpatch():
    S._offerup_fetch_via_graphql = _o['gql']
    S.check_image_with_ai = _o['ai']
    S.is_user_active = _o['act']
    S._zip_to_latlon = _o['zip']
    S.city_to_latlon = _o['city']


def scan(cfg, scan_description=True, keep_cache=False):
    if not keep_cache:
        S._OU_DETAIL_CACHE.clear()
    del POSTS[:], LOG[:]
    patch()
    try:
        return S.scrape_offerup_for_user(
            'ou-test', '95212', 25, {'switch': cfg}, [], False, 'balanced',
            debug=True, log_callback=lambda u, m, k='info': LOG.append((k, m)),
            known_links=set(), scan_description=scan_description)
    finally:
        unpatch()


def looked_up():
    return sorted(p['body']['variables']['listingId'] for p in POSTS)


def links(res):
    return sorted(r['link'].rsplit('/', 1)[-1] for r in res)


reset()
REPLIES[ID_B] = (200, reply(ID_B, 'Some scratches on the screen, no dock.', NOW_ISO))
base = {'min': None, 'max': None}
t.ok('cobalt' in FIX['data']['listing']['description'].lower()
     and all('cobalt' not in i['title'].lower() for i in ITEMS),
     "fixture: 'cobalt' is in A's body and in no title (what makes the include test real)")
res = scan(dict(base, exclusions=[], includes=['cobalt']))
t.eq(links(res), [ID_A], "A: include 'cobalt' (only in A's BODY) keeps A, drops B")
t.eq(looked_up(), sorted([ID_A, ID_B]), 'A: one lookup per nearby candidate...')
t.ok(ID_FAR not in looked_up(), '...and NONE for the listing beyond the radius (distance runs first)')
a_row = [r for r in res if ID_A in r['link']][0]
t.eq(a_row.get('listed_at'), datetime(2026, 11, 23, 23, 8, 27, 211000, tzinfo=timezone.utc),
     "A: listed_at is the listing's REAL post date, not a first-seen stamp")
t.ok(any('descriptions: 2 read' in m for k, m in LOG), 'A: the debug line counts both reads')

res = scan(dict(base, exclusions=[], includes=['cobalt']), keep_cache=True)
t.eq(links(res), [ID_A], 'A again (next scan): same outcome')
t.eq(POSTS, [], '...with ZERO lookups: B, dropped on its body, is not looked up every scan')

res = scan(dict(base, exclusions=[], includes=['cobalt']), scan_description=False)
t.eq(len(POSTS), 0, 'B: description scanning off -> zero lookups')
t.eq(links(res), [], '...and title-only include drops both')

res = scan(dict(base, exclusions=[], includes=[]))
t.eq(len(POSTS), 0, 'C: no keywords -> zero lookups')
t.eq(links(res), sorted([ID_A, ID_B]), '...both nearby listings kept, the far one dropped')

res = scan(dict(base, exclusions=['nintendo'], includes=[]))
t.eq((len(POSTS), links(res)), (0, []), 'D: title-excluded -> dropped with zero lookups')

res = scan(dict(base, exclusions=['scratches'], includes=[]))
t.eq(links(res), [ID_A], "E: exclude 'scratches' (only in B's body) drops B, keeps A")

os.environ['MAX_LISTING_AGE_DAYS'] = '7'
REPLIES[ID_A] = (200, reply(ID_A, 'Fresh listing.', NOW_ISO))
REPLIES[ID_B] = (200, reply(ID_B, 'Nothing special.', OLD_ISO))
res = scan(dict(base, exclusions=['zzzz'], includes=[]))
t.eq(links(res), [ID_A], 'F: a real post date 30 days old now feeds the 7-day age filter')
os.environ['MAX_LISTING_AGE_DAYS'] = '3650'

reset()
REPLIES[ID_B] = (200, reply(ID_B, 'Some scratches on the screen, no dock.', NOW_ISO))
os.environ['OFFERUP_DESC_LOOKUPS_PER_SCAN'] = '1'
res = scan(dict(base, exclusions=[], includes=['cobalt']))
t.eq((looked_up(), links(res)), ([ID_A], [ID_A]),
     'J: cap 1 -> one live lookup; the second listing WAITS (not decided title-only)')
t.ok(any(k == 'info' and '1 more listing needs a description check' in m for k, m in LOG),
     'J: ...and the console says it will be checked next scan')
res = scan(dict(base, exclusions=[], includes=['cobalt']), keep_cache=True)
t.eq(looked_up(), [ID_B], 'J: next scan: the cached one is free (not counted), the waiting one is looked up')
t.eq(links(res), [ID_A], '...and decided on its body (dropped: no cobalt)')
os.environ.pop('OFFERUP_DESC_LOOKUPS_PER_SCAN')

reset()
DEFAULT['status'] = 403
res = scan(dict(base, exclusions=[], includes=['cobalt']))
t.eq(links(res), [], 'G: lookups refused -> title-only fallback')
t.ok(any(k == 'error' and 'descriptions unavailable' in m for k, m in LOG),
     'G: ...said in RED (the user must be able to tell)')

reset()
os.environ['OFFERUP_LISTING_API'] = '0'
res = scan(dict(base, exclusions=[], includes=['cobalt']))
t.eq(len(POSTS), 0, 'H: switched off -> zero lookups')
t.ok(any(k == 'info' and 'switched off' in m for k, m in LOG), 'H: said once, as plain info')
t.ok(not any(k == 'error' and 'descriptions unavailable' in m for k, m in LOG), 'H: ...not in red')

reset()
REPLIES[ID_A] = (200, reply(ID_A, '', NOW_ISO))
REPLIES[ID_B] = (200, reply(ID_B, '', NOW_ISO))
scan(dict(base, exclusions=['zzzz'], includes=[]))
t.ok(not any(k == 'error' for k, m in LOG), 'I: sellers who wrote nothing are not an outage (no red line)')

# =============================================================================
t.section('5. Legacy item-page fallback (OFFERUP_ITEM_PAGES=1)')
PAGES = {}
for path in fixture_glob('ou_item_*.html'):
    PAGES[os.path.basename(path)[len('ou_item_'):-len('.html')]] = io.open(path, encoding='utf-8').read()
if len(PAGES) < 2:
    print('  NOTE: no ou_item_*.html fixtures (the pages are walled, so they cannot be refreshed);')
    print('        the opt-in item-page fallback is not exercised here. Sections 1-4 cover the default path.')
else:
    reset()
    os.environ['OFFERUP_LISTING_API'] = '0'
    os.environ['OFFERUP_ITEM_PAGES'] = '1'
    iid = sorted(PAGES)[0]
    cffi_requests.get = lambda url, **k: (GETS.append(url) or
                                          FakeResp(200, PAGES[iid]) if iid in url else FakeResp(404, b''))
    at, body = S._offerup_item_details('https://offerup.com/item/detail/%s' % iid, 0,
                                       want_posted=True, want_description=True)
    t.ok(isinstance(at, datetime) and isinstance(body, str) and body, 'item page: date and body')
    t.eq(len(GETS), 1, 'item page: one fetch for both')
    os.environ.pop('OFFERUP_ITEM_PAGES')

t.done()
