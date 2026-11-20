# -*- coding: utf-8 -*-
"""
Launch checklist verifier: checks each manual launch step from the OUTSIDE,
so "is it done?" has a PASS/FAIL answer instead of a guess.

    .venv\\Scripts\\python.exe tools\\launch_check.py            # everything
    .venv\\Scripts\\python.exe tools\\launch_check.py --no-db    # public checks only

PUBLIC checks (no credentials, GET requests and DNS lookups only):
  deploy   backend /health: new code live, gunicorn, Python 3.12, scanner thread
           running and cycling, no preload, LIVE Stripe ids resolving, config
           warnings, Mercari token state            (handoff 11, 8a-2, 8a-4, 8a-5)
  dns      _dmarc TXT, exactly one root SPF incl. Private Email, Mailgun SPF (8a-6)
  landing  footer logo loads on a NESTED page; honest Pro copy is live (8a-14)

DATABASE checks — the DATABASE_URL in pixelFlipScraper/.env, which is
PRODUCTION. The session is READ-ONLY (set_session(readonly=True)); it prints
counts and 8-character id prefixes only, never an email, token or URL. Run
them only with Bryan's OK.
  entitlement  rows with is_pro but no plan_tier (8a-3), Stripe customers
  scanner      armed users vs cap, armed-but-unpaid, live leases
  platforms    newest listing per marketplace (proof a platform works)
  timing       scan duration D from the Scan timing: lines, NOT-scanned lines
  mercari      token expiry straight from scraper_state (when /health lacks it)

Exit code 0 when nothing FAILs; WARN and INFO never fail the run.
"""
import argparse
import base64
import json
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests

try:
    sys.stdout.reconfigure(errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_API = 'https://pixelflip-backend.onrender.com'
DEFAULT_SITE = 'https://pixelflip.app'
DEFAULT_DOMAIN = 'pixelflip.app'
# A NESTED route: a relative logo src only breaks below the top level.
NESTED_PAGE = '/blog/how-to-find-underpriced-facebook-marketplace-listings'
OLD_PROD_COMMIT = 'd76d14f'
HEADERS = {'User-Agent': 'pixelflip-launch-check/1.0'}
PLATFORMS = ('craigslist', 'facebook', 'offerup', 'mercari')


# --------------------------------------------------------------------------- report
class Report(object):
    def __init__(self):
        self.counts = {}

    @staticmethod
    def _ascii(s):
        # A Windows console is cp1252; an em dash prints as a replacement glyph.
        return s.replace('—', '-')

    def section(self, title):
        print('\n== %s' % self._ascii(title))

    def add(self, status, item, message):
        self.counts[status] = self.counts.get(status, 0) + 1
        print('  %-4s  %-22s %s' % (status, item, self._ascii(message)))

    def exit_code(self):
        print('\n' + '  '.join('%s %d' % (k, self.counts.get(k, 0))
                               for k in ('PASS', 'FAIL', 'WARN', 'INFO', 'SKIP')))
        return 1 if self.counts.get('FAIL') else 0


# --------------------------------------------------------------------------- pure helpers
def jwt_exp(token):
    """`exp` from a JWT payload (no signature check), or None."""
    try:
        token = (token or '').replace('Bearer ', '').strip()
        parts = token.split('.')
        if len(parts) != 3:
            return None
        seg = parts[1]
        body = json.loads(base64.urlsafe_b64decode(seg + '=' * (-len(seg) % 4)))
        return float(body['exp']) if body.get('exp') else None
    except Exception:
        return None


def scan_seconds(message):
    """75.0 from 'Scan timing: 75s total — ...', else None."""
    m = re.match(r'\s*Scan timing:\s*([\d.]+)s total', message or '')
    return float(m.group(1)) if m else None


def parse_nslookup_txt(output):
    """
    TXT strings per record from nslookup output, each record's strings joined.

    Windows prints `name<TAB>text =` then each quoted string on its own line;
    Linux prints them on the header line. A record split into several strings
    is joined without separators, which is how SPF and DMARC read them.
    """
    records, current = [], None
    for line in (output or '').splitlines():
        if 'text =' in line:
            if current is not None:
                records.append(''.join(current))
            current = re.findall(r'"((?:[^"\\]|\\.)*)"', line.split('text =', 1)[1])
            continue
        if current is not None:
            found = re.findall(r'"((?:[^"\\]|\\.)*)"', line)
            if found and line[:1] in (' ', '\t'):
                current.extend(found)
            elif line.strip():
                records.append(''.join(current))
                current = None
    if current is not None:
        records.append(''.join(current))
    return records


def txt_records(name):
    """TXT records for `name` via dnspython if installed, else the system nslookup."""
    try:
        import dns.resolver  # noqa: F401
        try:
            answers = dns.resolver.resolve(name, 'TXT', lifetime=10)
        except Exception:
            return []
        return [b''.join(r.strings).decode('utf-8', 'replace') for r in answers]
    except ImportError:
        pass
    try:
        out = subprocess.run(['nslookup', '-type=TXT', name], capture_output=True,
                             text=True, timeout=20)
    except Exception:
        return None
    return parse_nslookup_txt(out.stdout)


def spf_verdict(records, required_include):
    spfs = [r for r in records if r.lower().startswith('v=spf1')]
    if not spfs:
        return 'FAIL', 'no SPF record (add TXT @ "v=spf1 include:%s ~all")' % required_include
    if len(spfs) > 1:
        return 'FAIL', ('%d SPF records — more than one is a permanent SPF error; merge them '
                        'into one' % len(spfs))
    if ('include:' + required_include) not in spfs[0].lower():
        return 'FAIL', 'SPF exists but lacks include:%s -> %s' % (required_include, spfs[0])
    return 'PASS', spfs[0]


def dmarc_verdict(records):
    d = [r for r in records if r.replace(' ', '').lower().startswith('v=dmarc1')]
    if not d:
        return 'FAIL', 'no _dmarc record (add TXT _dmarc "v=DMARC1; p=none; rua=mailto:...")'
    if len(d) > 1:
        return 'FAIL', 'more than one DMARC record — receivers ignore all of them'
    m = re.search(r'\bp\s*=\s*(\w+)', d[0], re.I)
    return 'PASS', 'policy p=%s' % (m.group(1).lower() if m else '?')


def find_logo_src(html):
    """src of the footer logo <img alt="PixelFlip Logo">, else None."""
    for tag in re.findall(r'<img\b[^>]*>', html or '', re.I):
        if re.search(r'alt="PixelFlip Logo"', tag, re.I):
            m = re.search(r'\bsrc="([^"]+)"', tag, re.I)
            if m:
                return m.group(1)
    return None


def local_head(repo):
    try:
        out = subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD'], capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def get(url, timeout=45):
    return requests.get(url, headers=HEADERS, timeout=timeout)


# --------------------------------------------------------------------------- public checks
def check_deploy(rep, api):
    rep.section('deploy — backend /health (handoff 11, 8a-2, 8a-4, 8a-5)')
    try:
        r = get(api.rstrip('/') + '/health', timeout=90)  # a cold free instance is slow
    except Exception as e:
        rep.add('FAIL', 'health', 'unreachable: %s' % type(e).__name__)
        return None
    try:
        body = r.json()
    except Exception:
        rep.add('FAIL', 'health', 'HTTP %s, not JSON' % r.status_code)
        return None
    checks = body.get('checks') or {}
    rep.add('PASS' if r.status_code == 200 else 'FAIL', 'health',
            'HTTP %s, database %s' % (r.status_code, checks.get('database')))
    ver = body.get('version')
    if not ver:
        rep.add('FAIL', 'new code live', 'no version block in /health — production still runs '
                'the old code (%s). Commit + push, then apply the Blueprint.' % OLD_PROD_COMMIT)
        mins = checks.get('scraper_last_cycle_age_seconds')
        rep.add('INFO', 'scanner (old code)', 'last cycle %s s ago' % mins if mins is not None
                else 'no cycle recorded')
        return body
    rep.add('PASS', 'new code live', 'commit %s, branch %s, started %s' % (
        ver.get('commit') or 'unknown', ver.get('branch') or '?', ver.get('started_at')))
    head = local_head(REPO)
    if ver.get('commit') and head:
        same = head.startswith(ver['commit']) or ver['commit'].startswith(head[:12])
        rep.add('PASS' if same else 'WARN', 'matches local HEAD',
                'yes' if same else 'deployed %s, local HEAD %s — unpushed commits or an '
                'auto-deploy still running?' % (ver['commit'][:12], head[:12]))
    server = ver.get('server') or 'unknown'
    rep.add('PASS' if server == 'gunicorn' else 'WARN', 'server',
            server if server == 'gunicorn' else
            '%s — Blueprint not applied yet (render.yaml moves it to gunicorn)' % server)
    py = ver.get('python') or ''
    rep.add('PASS' if py.startswith('3.12') else 'WARN', 'python',
            py if py.startswith('3.12') else '%s — .python-version pins 3.12' % py)
    preload = checks.get('gunicorn_preload')
    if preload is not None:
        rep.add('PASS' if not preload else 'WARN', 'gunicorn preload',
                'off' if not preload else 'ON — GUNICORN_CMD_ARGS still has --preload (Render '
                'default); scanning is safe but memory doubles. Re-apply render.yaml.')
    thread = checks.get('scraper_thread')
    rep.add('PASS' if thread == 'running' else 'FAIL', 'scanner thread', str(thread))
    age = checks.get('scraper_last_cycle_age_seconds')
    if age is None:
        rep.add('WARN', 'scanner cycling', 'no cycle recorded yet (fresh boot?) — re-run in a minute')
    else:
        rep.add('PASS' if age < 120 else 'FAIL', 'scanner cycling', 'last cycle %.0fs ago' % age)
    billing = checks.get('billing') or {}
    mode, verdict = billing.get('mode'), billing.get('check')
    if mode == 'live' and verdict == 'ok':
        rep.add('PASS', 'stripe', 'LIVE key; prices, coupon and webhook all resolve')
    elif verdict == 'pending':
        rep.add('WARN', 'stripe', 'live check still running — re-run shortly')
    else:
        rep.add('FAIL', 'stripe', 'mode=%s check=%s — read the "[boot] stripe" lines in the '
                'Render log' % (mode, verdict))
    for code in checks.get('config_warnings') or []:
        severe = code.startswith('stripe') or code in ('mailgun_missing', 'supabase_service_role_missing')
        rep.add('FAIL' if severe else 'WARN', 'config', code)
    if not checks.get('config_warnings'):
        rep.add('PASS', 'config', 'no feature-disabling gaps')
    tok = checks.get('mercari_token') or {}
    if tok:
        state, hours = tok.get('state'), tok.get('hours_left')
        if state == 'ok':
            rep.add('PASS' if (hours or 0) > 24 else 'WARN', 'mercari token',
                    'valid, %.0fh left (expires %s)' % (hours or 0, tok.get('expires_at')))
        else:
            rep.add('FAIL', 'mercari token', '%s%s — run the "Mercari token" workflow' % (
                state, (' since %s' % tok['expires_at']) if tok.get('expires_at') else ''))
    return body


def check_dns(rep, domain):
    rep.section('dns — email authentication (handoff 8a-6)')
    dmarc = txt_records('_dmarc.' + domain)
    if dmarc is None:
        rep.add('SKIP', 'dmarc', 'no DNS tool available (install dnspython or nslookup)')
        return
    status, msg = dmarc_verdict(dmarc)
    rep.add(status, 'dmarc', msg)
    status, msg = spf_verdict(txt_records(domain) or [], 'spf.privateemail.com')
    rep.add(status, 'root spf', msg)
    status, msg = spf_verdict(txt_records('mg.' + domain) or [], 'mailgun.org')
    rep.add('PASS' if status == 'PASS' else 'WARN', 'mailgun spf', msg)


def check_landing(rep, site):
    rep.section('landing — footer logo and Pro copy (handoff 8a-14)')
    page = site.rstrip('/') + NESTED_PAGE
    try:
        r = get(page)
    except Exception as e:
        rep.add('FAIL', 'nested page', 'unreachable: %s' % type(e).__name__)
        return
    if r.status_code != 200:
        rep.add('WARN', 'nested page', 'HTTP %s for %s' % (r.status_code, NESTED_PAGE))
        return
    src = find_logo_src(r.text)
    if not src:
        rep.add('WARN', 'footer logo', 'no <img alt="PixelFlip Logo"> found on %s' % NESTED_PAGE)
    else:
        url = urljoin(page, src)
        try:
            ok = get(url).status_code == 200
        except Exception:
            ok = False
        rep.add('PASS' if ok else 'FAIL', 'footer logo',
                ('%s loads' % src) if ok else
                '%s resolves to %s and does not load — the Footer fix is not deployed' % (src, url))
    try:
        pricing = get(site.rstrip('/') + '/pricing').text
    except Exception as e:
        rep.add('FAIL', 'pro copy', 'pricing page unreachable: %s' % type(e).__name__)
        return
    honest = '3 priority terms' in pricing
    overstated = 'Scans every 5 minutes' in pricing
    rep.add('PASS' if honest and not overstated else 'FAIL', 'pro copy',
            'honest "3 priority terms" copy is live' if honest and not overstated else
            'pricing still says "Scans every 5 minutes" for all of Pro — landing not deployed')


# --------------------------------------------------------------------------- database checks
def check_database(rep, env_path, need_mercari):
    rep.section('database — READ-ONLY production probe (handoff 8a-3, 7)')
    try:
        from dotenv import dotenv_values
        import psycopg2
    except ImportError as e:
        rep.add('SKIP', 'database', 'missing %s — run with the project .venv' % e.name)
        return
    url = (dotenv_values(env_path).get('DATABASE_URL') or '').strip()
    if not url:
        rep.add('SKIP', 'database', 'no DATABASE_URL in %s' % env_path)
        return
    conn = psycopg2.connect(url, connect_timeout=15)
    try:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '15s'")

        def q(sql, args=None, optional=False):
            cur.execute('SAVEPOINT q')
            try:
                cur.execute(sql, args)
                rows = cur.fetchall()
                cur.execute('RELEASE SAVEPOINT q')
                return rows
            except Exception as e:
                cur.execute('ROLLBACK TO SAVEPOINT q')
                if optional:
                    return None
                raise e

        cur.execute('SELECT 1')
        rep.add('PASS', 'read-only session', 'connected; this transaction cannot write')

        # Entitlement (handoff 8a-3)
        total, pro, basic, other, customers = q("""
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE LOWER(TRIM(COALESCE(plan_tier,''))) = 'pro'),
                   COUNT(*) FILTER (WHERE LOWER(TRIM(COALESCE(plan_tier,''))) = 'basic'),
                   COUNT(*) FILTER (WHERE LOWER(TRIM(COALESCE(plan_tier,''))) NOT IN ('pro','basic')),
                   COUNT(*) FILTER (WHERE COALESCE(stripe_customer_id,'') <> '')
            FROM user_settings""")[0]
        rep.add('INFO', 'accounts', '%d settings rows: %d pro, %d basic, %d other; %d Stripe customers'
                % (total, pro, basic, other, customers))
        auth_users = q('SELECT COUNT(*) FROM auth.users', optional=True)
        if auth_users:
            rep.add('INFO', 'auth users', str(auth_users[0][0]))
        waitlist = q('SELECT COUNT(*) FROM waitlist', optional=True)
        if waitlist:
            rep.add('INFO', 'waitlist', '%d sign-ups' % waitlist[0][0])
        amb = [r[0] for r in q("""
            SELECT LEFT(user_id::text, 8) FROM user_settings
            WHERE COALESCE(is_pro, FALSE) AND COALESCE(TRIM(plan_tier), '') = ''
            ORDER BY 1""")]
        rep.add('PASS' if not amb else 'WARN', 'is_pro w/o plan_tier',
                'none' if not amb else '%d row(s) resolve to BASIC: %s — set plan_tier '
                "explicitly ('pro' if they should stay Pro)" % (len(amb), ', '.join(amb)))

        # Scanner state
        armed, unpaid, leases = q("""
            SELECT COUNT(*) FILTER (WHERE is_active),
                   COUNT(*) FILTER (WHERE is_active
                       AND LOWER(TRIM(COALESCE(plan_tier,''))) NOT IN ('basic','pro')
                       AND NOT COALESCE(is_pro, FALSE)),
                   COUNT(*) FILTER (WHERE scrape_claimed_until > NOW())
            FROM user_settings""")[0]
        rep.add('INFO', 'scanners on', '%d armed, %d live lease(s)' % (armed, leases))
        rep.add('PASS' if not unpaid else 'WARN', 'armed but unpaid',
                'none' if not unpaid else '%d — the new scheduler switches these off within '
                'a cycle; old code does not' % unpaid)

        # Platforms (proof a marketplace works = recent rows, not console text)
        rows = q("""SELECT LOWER(platform), COUNT(*), MAX(created_at) FROM listings
                    WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY 1""")
        seen = {r[0]: r for r in rows}
        now = datetime.now(timezone.utc)
        for p in PLATFORMS:
            r = seen.get(p)
            if not r:
                rep.add('WARN' if armed else 'INFO', 'listings ' + p,
                        'none in 7 days' + ('' if armed else ' (nobody is armed)'))
                continue
            newest = r[2] if r[2].tzinfo else r[2].replace(tzinfo=timezone.utc)
            hours = (now - newest).total_seconds() / 3600.0
            rep.add('PASS' if hours < 24 else 'WARN', 'listings ' + p,
                    '%d in 7d, newest %.1fh ago' % (r[1], hours))

        # Scan duration D and failures (48h console retention)
        msgs = [r[0] for r in q("""SELECT message FROM scrape_logs
                    WHERE created_at > NOW() - INTERVAL '48 hours'
                      AND message LIKE 'Scan timing:%%'""")]
        secs = sorted(s for s in (scan_seconds(m) for m in msgs) if s is not None)
        if secs:
            p90 = secs[min(len(secs) - 1, int(round(0.9 * (len(secs) - 1))))]
            rep.add('INFO', 'scan duration D', 'n=%d median %.0fs p90 %.0fs max %.0fs'
                    % (len(secs), statistics.median(secs), p90, secs[-1]))
        else:
            rep.add('INFO', 'scan duration D', 'no "Scan timing:" lines in 48h')
        failed = q("""SELECT COUNT(*) FROM scrape_logs
                      WHERE created_at > NOW() - INTERVAL '48 hours'
                        AND message LIKE 'Marketplaces NOT scanned:%%'""")[0][0]
        rep.add('PASS' if not failed else 'WARN', 'not-scanned lines', '%d in 48h' % failed)

        # Mercari token, when /health could not say
        if need_mercari:
            row = q("SELECT value FROM scraper_state WHERE key = %s", ('mercari_api_ctx',))
            if not row:
                rep.add('FAIL', 'mercari token', 'absent — run the "Mercari token" workflow')
            else:
                payload = row[0][0]
                payload = json.loads(payload) if isinstance(payload, str) else payload
                ctx = (payload or {}).get('ctx') or {}
                exp = jwt_exp(((ctx.get('headers') or {}).get('authorization')) or '')
                if not exp:
                    rep.add('FAIL', 'mercari token', 'stored but unreadable')
                else:
                    left = (exp - now.timestamp()) / 3600.0
                    when = datetime.fromtimestamp(exp, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                    rep.add('PASS' if left > 24 else ('WARN' if left > 0 else 'FAIL'),
                            'mercari token', ('valid, %.0fh left' % left) if left > 0
                            else 'EXPIRED %s — run the "Mercari token" workflow' % when)
        conn.rollback()
    finally:
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--api', default=os.getenv('PIXELFLIP_API_URL', DEFAULT_API))
    ap.add_argument('--site', default=DEFAULT_SITE)
    ap.add_argument('--domain', default=DEFAULT_DOMAIN)
    ap.add_argument('--env', default=os.path.join(REPO, '.env'),
                    help='file holding DATABASE_URL (read by explicit path)')
    ap.add_argument('--no-db', action='store_true', help='skip the production database checks')
    args = ap.parse_args(argv)

    rep = Report()
    print('PixelFlip launch check  %s' % datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    health = None
    for fn, arg in ((check_deploy, args.api), (check_dns, args.domain), (check_landing, args.site)):
        try:
            out = fn(rep, arg)
            if fn is check_deploy:
                health = out
        except Exception as e:
            rep.add('WARN', fn.__name__, 'check failed to run: %s' % type(e).__name__)
    if args.no_db:
        rep.section('database — skipped (--no-db)')
    else:
        need_mercari = not ((health or {}).get('checks') or {}).get('mercari_token')
        try:
            check_database(rep, args.env, need_mercari)
        except Exception as e:
            rep.add('FAIL', 'database', 'probe failed: %s' % type(e).__name__)
    return rep.exit_code()


if __name__ == '__main__':
    sys.exit(main())
