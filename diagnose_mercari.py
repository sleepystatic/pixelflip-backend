"""
Mercari connectivity diagnostic — run this ON the machine that is failing.

    python diagnose_mercari.py

Every Mercari fix so far has been a hypothesis formed on a laptop where Mercari
works. This tests each layer separately on the machine that actually fails, so
the answer is a measurement rather than a guess:

    1. what binaries exist, and where
    2. does each engine LAUNCH
    3. can it reach a trivial HTTPS site (rules out TLS / missing libs)
    4. can it reach mercari.com (rules out Cloudflare / proxy)
    5. what exit IP does the outside world see

Read-only. Touches no database and saves nothing.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

OK, BAD, INFO = '  [ok ]', '  [FAIL]', '  [info]'


def section(t):
    print(f'\n{"=" * 68}\n{t}\n{"=" * 68}')


section('1. ENVIRONMENT')
print(f'{INFO} python            : {sys.executable}')
print(f'{INFO} platform          : {sys.platform}')
for var in ('MERCARI_CHROME_PATH', 'MERCARI_CHROME_CHANNEL', 'MERCARI_BUNDLED_CHROMIUM',
            'PLAYWRIGHT_BROWSERS_PATH', 'PATCHRIGHT_PROFILE_DIR'):
    val = os.getenv(var)
    print(f'{INFO} {var:26s}: {val if val else "(unset)"}')
for var in ('MERCARI_PROXY_URL', 'PROXY_URL'):
    val = os.getenv(var)
    print(f'{INFO} {var:26s}: {"SET (" + str(len(val)) + " chars)" if val else "(unset)"}')

chrome_path = (os.getenv('MERCARI_CHROME_PATH') or '').strip()
if chrome_path:
    exists = os.path.isfile(chrome_path)
    print(f'{OK if exists else BAD} MERCARI_CHROME_PATH exists on disk: {exists}')
    if exists:
        # Missing shared libraries are the classic rootless-unpack failure and
        # they do NOT stop Chrome from starting — only from doing TLS.
        try:
            import subprocess
            out = subprocess.run(['ldd', chrome_path], capture_output=True,
                                 text=True, timeout=30).stdout
            missing = [l.strip() for l in out.splitlines() if 'not found' in l]
            if missing:
                print(f'{BAD} MISSING SHARED LIBRARIES ({len(missing)}):')
                for m in missing[:15]:
                    print(f'        {m}')
                print('        -> this Chrome can start but cannot load HTTPS.')
                print('        -> do NOT set MERCARI_CHROME_PATH; set '
                      'MERCARI_BUNDLED_CHROMIUM=1')
            else:
                print(f'{OK} no missing shared libraries')
        except FileNotFoundError:
            print(f'{INFO} ldd unavailable (not Linux) — skipping library check')
        except Exception as e:
            print(f'{INFO} ldd check failed: {e}')

section('2. PATCHRIGHT / BROWSER AVAILABILITY')
try:
    import patchright
    print(f'{OK} patchright importable ({getattr(patchright, "__file__", "?")})')
except Exception as e:
    print(f'{BAD} patchright NOT importable: {e}')
    print('        -> Mercari cannot work at all. Check requirements.txt install.')
    sys.exit(1)

try:
    from patchright.sync_api import sync_playwright as pw_sync
    with pw_sync() as p:
        try:
            path = p.chromium.executable_path
            exists = bool(path) and os.path.exists(path)
            print(f'{OK if exists else BAD} patchright bundled chromium: {path}')
            if not exists:
                print('        -> run: patchright install chromium')
                print('        -> NOTE build installs it only in the fallback branch')
        except Exception as e:
            print(f'{INFO} could not resolve bundled chromium: {e}')
except Exception as e:
    print(f'{BAD} patchright sync_api failed: {e}')

section('3. LAUNCH + NAVIGATE, PER ENGINE')


def try_engine(label, kwargs, url, use_proxy):
    from patchright.sync_api import sync_playwright
    import tempfile
    prof = os.path.join(tempfile.gettempdir(), f'diag_{label.replace(" ", "_")}')
    args = ['--headless=new', '--window-size=1280,800']
    if not sys.platform.startswith('win'):
        args += ['--no-sandbox', '--disable-dev-shm-usage']
    launch = dict(headless=False, no_viewport=True, args=args, **kwargs)
    if use_proxy:
        px = (os.getenv('MERCARI_PROXY_URL') or os.getenv('PROXY_URL') or '').strip()
        if px:
            try:
                from scraper_multi_user import _pw_proxy_dict
                launch['proxy'] = _pw_proxy_dict(px)
            except Exception as e:
                print(f'{INFO}   proxy parse failed: {e}')
    t0 = time.time()
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(prof, **launch)
            try:
                page = ctx.new_page()
                page.goto(url, wait_until='domcontentloaded', timeout=40000)
                title = (page.title() or '')[:46]
                print(f'{OK} {label:34s} {time.time()-t0:5.1f}s  {url[:30]:30s} '
                      f'title={title!r}')
                return True
            finally:
                ctx.close()
    except Exception as e:
        msg = str(e).split('\n')[0][:100]
        print(f'{BAD} {label:34s} {time.time()-t0:5.1f}s  {url[:30]:30s} {msg}')
        return False


engines = [('bundled chromium', {})]
if chrome_path and os.path.isfile(chrome_path):
    engines.append(('real chrome (path)', {'executable_path': chrome_path}))
else:
    engines.append(('real chrome (channel)',
                    {'channel': os.getenv('MERCARI_CHROME_CHANNEL', 'chrome')}))

print('\n-- example.com (proves TLS + libs, no Cloudflare involved) --')
tls_ok = {}
for label, kw in engines:
    tls_ok[label] = try_engine(label, kw, 'https://example.com', use_proxy=False)

print('\n-- mercari.com, NO proxy --')
for label, kw in engines:
    if tls_ok.get(label):
        try_engine(label, kw, 'https://www.mercari.com/robots.txt', use_proxy=False)
    else:
        print(f'{INFO} {label:34s} skipped — it could not load example.com')

if (os.getenv('MERCARI_PROXY_URL') or '').strip():
    print('\n-- mercari.com, THROUGH MERCARI_PROXY_URL --')
    for label, kw in engines:
        if tls_ok.get(label):
            try_engine(label, kw, 'https://www.mercari.com/robots.txt', use_proxy=True)

section('4. EXIT IP (what Cloudflare sees)')
try:
    import requests
    for label, px in (('direct', None),
                      ('via MERCARI_PROXY_URL', os.getenv('MERCARI_PROXY_URL'))):
        if label != 'direct' and not px:
            continue
        proxies = None
        if px:
            try:
                from scraper_multi_user import _normalize_proxy
                n = _normalize_proxy(px)
                proxies = {'http': n, 'https': n}
            except Exception:
                proxies = {'http': px, 'https': px}
        try:
            t0 = time.time()
            d = requests.get('https://ipinfo.io/json', proxies=proxies, timeout=40).json()
            print(f'{OK} {label:22s} {time.time()-t0:5.1f}s  ip={d.get("ip")} '
                  f'org={str(d.get("org"))[:40]}')
        except Exception as e:
            print(f'{BAD} {label:22s} {type(e).__name__}: {str(e)[:70]}')
except Exception as e:
    print(f'{INFO} requests unavailable: {e}')

section('HOW TO READ THIS')
print("""  example.com fails, mercari.com untested
      -> the browser cannot do TLS. Missing shared libraries (see section 1).
         Fix: unset MERCARI_CHROME_PATH, set MERCARI_BUNDLED_CHROMIUM=1

  example.com OK, mercari.com fails WITHOUT proxy but works WITH it
      -> Cloudflare is blocking the datacenter IP. Keep the proxy.

  example.com OK, mercari.com fails BOTH ways
      -> Cloudflare is blocking this browser fingerprint, not the network.
         Check that patchright (not playwright) is the one launching.

  everything OK here but the scraper still fails
      -> the failure is in the capture step, not connectivity. Look for
         '[mercari] api capture navigation failed' in the logs.""")
