# -*- coding: utf-8 -*-
"""
Re-fetch the HTML fixtures the description suites parse. THE ONLY SCRIPT HERE
THAT TOUCHES A MARKETPLACE.

    .venv\\Scripts\\python.exe tests\\refresh_fixtures.py             # all four
    .venv\\Scripts\\python.exe tests\\refresh_fixtures.py craigslist  # just one

You should almost never need this. Fixtures are committed precisely so the
suites run offline and deterministically; refresh only when a platform changes
its markup and a suite starts failing for real.

Traffic is deliberately bounded — one search plus two item pages per platform.
Mercari and Facebook are the sensitive ones: Mercari is Cloudflare-fronted and
Facebook shares the residential proxy production uses, and both have served
blocks in response to over-probing. Do not loop this.
"""
import io
import json
import os
import re
import sys

from _harness import FIXTURES, bootstrap

bootstrap()
import scraper_multi_user as S

PER_PLATFORM = 2


def save(name, text):
    if not os.path.isdir(FIXTURES):
        os.makedirs(FIXTURES)
    path = os.path.join(FIXTURES, name)
    io.open(path, 'w', encoding='utf-8').write(text)
    print("    saved %s (%d KB)" % (name, len(text.encode('utf-8', 'replace')) // 1024))


def clear(prefix):
    for f in os.listdir(FIXTURES) if os.path.isdir(FIXTURES) else []:
        if f.startswith(prefix) and f.endswith('.html'):
            os.remove(os.path.join(FIXTURES, f))


# --------------------------------------------------------------- craigslist
def refresh_craigslist():
    from bs4 import BeautifulSoup
    sess = S._craigslist_session()
    url = ("https://stockton.craigslist.org/search/sss?query=nintendo"
           "&sort=date&postal=95212&search_distance=25")
    r = sess.get(url, timeout=25)
    print("  search HTTP %s (%d KB)" % (r.status_code, len(r.content) // 1024))
    if r.status_code != 200:
        return False
    soup = BeautifulSoup(r.content, 'html.parser')
    links = []
    for it in soup.find_all('li', class_='cl-static-search-result'):
        a = it.find('a')
        h = (a.get('href') if a else None) or ''
        if h.startswith('/'):
            h = 'https://stockton.craigslist.org' + h
        if h and h not in links:
            links.append(h)
    saved = 0
    for link in links:
        if saved >= PER_PLATFORM:
            break
        d = sess.get(link, timeout=25)
        if d.status_code != 200:
            continue
        # Only keep pages that actually carry a body, or the suite proves nothing.
        soup2 = BeautifulSoup(d.content, 'html.parser')
        if not S._craigslist_description_from_soup(soup2):
            continue
        save("cl_item_%s.html" % link.rstrip('/').rsplit('/', 1)[-1][:40], d.text)
        saved += 1
    return saved > 0


# ----------------------------------------------------------------- facebook
def refresh_facebook():
    os.environ.setdefault('FB_LEAN_FETCH', '1')
    region = None
    try:
        match = S.fb_region_for_zip('95212')
        region = match[0] if match else None
    except Exception:
        pass
    q = 'nintendo+ds+lite'
    url = ("https://www.facebook.com/marketplace/%s/search/?query=%s" % (region, q)
           if region else "https://www.facebook.com/marketplace/search/?query=%s" % q)
    sess = S._FbLeanSession()
    saved = 0
    try:
        html = sess.fetch(url)
        if S._fb_html_is_blocked(html, lean=True):
            # Mirror the scrape loop: IPRoyal's pool contains exits that cannot
            # reach facebook.com at all (measured 2026-09-28 — the same tunnel
            # served example.com fine and refused facebook.com with
            # ERR_TUNNEL_CONNECTION_FAILED, while a rotated session returned
            # 463KB). Without this the refresher reports a Facebook outage that
            # production would simply have rotated past.
            rotated = S._rotate_sticky_session(os.getenv('FB_PROXY_URL'))
            if rotated:
                print("  exit IP cannot reach Facebook — rotating sticky session")
                sess.rotate(rotated)
                html = sess.fetch(url)
        print("  search %d KB" % (len(html or '') // 1024))
        items = S._fb_collect_from_json(html)
        print("  rows: %d" % len(items))
        for item in items:
            if saved >= PER_PLATFORM:
                break
            iid = S._fb_listing_id_from_link(item.get('link') or '')
            if not iid:
                continue
            page = sess.fetch(S._fb_item_url(iid))
            if not page or S._fb_html_is_blocked(page, lean=True):
                continue
            if not S._fb_description_from_item_html(page, iid):
                continue
            save("fb_item_%s.html" % iid, page)
            saved += 1
    finally:
        sess.close()
    return saved > 0


# ------------------------------------------------------------------ offerup
def refresh_offerup():
    from curl_cffi import requests as cffi
    coords = S._zip_to_latlon('95212') or (38.0, -121.26)
    items, err = S._offerup_fetch_via_graphql(
        'nintendo', coords[0], coords[1],
        force_no_proxy=S._env_flag('OFFERUP_GRAPHQL_NO_PROXY', True))
    if err or not items:
        print("  graphql failed: %r" % (err,))
        return False
    print("  rows: %d" % len(items))
    proxies = None
    px = S._get_proxy('offerup')
    if px:
        n = S._normalize_proxy(px)
        proxies = {'http': n, 'https': n}
    saved = 0
    # Stop after a short run of refusals instead of walking the whole feed. A
    # 403 streak means OfferUp is rate-limiting (its antibot is rate-based, not
    # IP-based, so another candidate will not help), and grinding through 40
    # more candidates deepens exactly the throttle we are hitting. Come back
    # after a cooldown.
    consecutive_failures = 0
    for it in items:
        if saved >= PER_PLATFORM:
            break
        if consecutive_failures >= 3:
            print("    3 refusals in a row — OfferUp is throttling; stopping."
                  "\n    Retry after a cooldown (tens of minutes).")
            break
        link = it.get('link') or ''
        if '/item/' not in link:
            continue
        r = cffi.get(link, headers={'User-Agent': S._patchright_clean_ua()},
                     proxies=proxies,
                     impersonate=(os.getenv('OFFERUP_TLS_IMPERSONATE') or 'chrome').strip(),
                     timeout=int(os.getenv('OFFERUP_HTTP_TIMEOUT_SEC', '30')))
        iid = link.rstrip('/').rsplit('/', 1)[-1]
        if r.status_code != 200:
            print("    %s: HTTP %s — skipping" % (iid[:12], r.status_code))
            consecutive_failures += 1
            continue
        m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            print("    %s: no __NEXT_DATA__ (throttled shell?) — skipping" % iid[:12])
            continue
        # Report what the payload carries rather than re-fetching to validate:
        # the suite is what asserts, and a second fetch per candidate would
        # double this script's traffic against a rate-sensitive marketplace.
        blob = m.group(1)
        print("    %s: postDate=%s description=%s"
              % (iid[:12], '"postDate"' in blob, '"description"' in blob))
        save("ou_item_%s.html" % iid[:40], r.text)
        saved += 1
        consecutive_failures = 0
    return saved > 0


# ------------------------------------------------------------------ mercari
def refresh_mercari():
    from curl_cffi import requests as cffi
    ctx = S._load_persisted_mercari_ctx()
    if not ctx:
        print("  no usable Mercari context — run capture_mercari_token.py first")
        return False
    imp = (os.getenv('MERCARI_TLS_IMPERSONATE') or 'chrome').strip()
    px = S._get_proxy('mercari')
    proxies = {'http': S._normalize_proxy(px), 'https': S._normalize_proxy(px)} if px else None
    saved = 0
    with cffi.Session() as sess:
        try:
            sess.cookies.update(ctx.get('cookies') or {})
        except Exception:
            pass
        url = S._mercari_api_url(ctx, 'nintendo switch', 8)
        r = sess.get(url, headers=ctx.get('headers') or {}, proxies=proxies,
                     impersonate=imp, timeout=45)
        print("  search API HTTP %s" % r.status_code)
        if r.status_code != 200:
            return False
        for it in S._mercari_items_from_api(r.json()):
            if saved >= PER_PLATFORM:
                break
            link = it.get('link') or ''
            if not link.startswith('http'):
                link = 'https://www.mercari.com' + link
            iid = S._mercari_item_id_from_link(link)
            if not iid:
                continue
            p = sess.get(link, headers=ctx.get('headers') or {}, proxies=proxies,
                         impersonate=imp, timeout=45)
            if p.status_code != 200 or 'Just a moment' in p.text:
                continue
            if not S._mercari_description_from_item_html(p.text, iid):
                continue
            save("mc_item_%s.html" % iid, p.text)
            saved += 1
    return saved > 0


PLATFORMS = {
    'craigslist': refresh_craigslist,
    'facebook': refresh_facebook,
    'offerup': refresh_offerup,
    'mercari': refresh_mercari,
}

if __name__ == '__main__':
    want = [a.lower() for a in sys.argv[1:]] or list(PLATFORMS)
    bad = [w for w in want if w not in PLATFORMS]
    if bad:
        print("unknown platform(s): %s\nknown: %s" % (bad, list(PLATFORMS)))
        sys.exit(2)
    failed = []
    for name in want:
        print("\n=== %s ===" % name)
        clear({'craigslist': 'cl_item_', 'facebook': 'fb_item_',
               'offerup': 'ou_item_', 'mercari': 'mc_item_'}[name])
        try:
            if not PLATFORMS[name]():
                failed.append(name)
        except Exception as e:
            print("  raised %s: %s" % (type(e).__name__, str(e)[:160]))
            failed.append(name)
    print("\n%s" % ("all fixtures refreshed" if not failed
                    else "FAILED for: %s" % failed))
    sys.exit(1 if failed else 0)
