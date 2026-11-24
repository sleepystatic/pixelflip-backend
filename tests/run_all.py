# -*- coding: utf-8 -*-
"""
Run every smoke suite. One exit code, one summary.

    .venv\\Scripts\\python.exe tests\\run_all.py

Each suite runs in its own process, because they monkeypatch module globals
(scrape functions, curl_cffi's Session, get_db_connection) and sharing an
interpreter would let one suite's patches leak into the next.

Exit codes per suite: 0 pass, 1 fail, 3 skipped for a missing fixture. A skip is
reported as SKIP and does NOT pass the run — a test that silently does nothing
is worse than a failing one.

For the same reason, a suite that exits 0 WITHOUT printing the harness's
"PASSED n   FAILED n" line is reported as NO-SUMMARY and does not pass either:
it never reached Suite.done(), so whatever it ran, nothing was counted.
"""
import codecs
import locale
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = [
    'smoke_pro_gate.py',
    'smoke_fb_description.py',
    'smoke_cl_description.py',
    'smoke_ou_description.py',
    'smoke_mc_description.py',
    'smoke_mc_proxy_route.py',
    'smoke_multiuser.py',
    'smoke_search_cache.py',
    'smoke_scan_timing.py',
    'smoke_digest_cap.py',
    'smoke_scrape_lease.py',
    'smoke_pacing.py',
    'smoke_offerup_browserless.py',
    'smoke_alert_dedup.py',
    'smoke_pwreset.py',
    'smoke_mercari_reason.py',
    'smoke_capacity_billing.py',
    'smoke_account_security.py',
    'smoke_boot.py',
    'smoke_launch_check.py',
    'smoke_trial.py',
    'smoke_includes.py',
    'smoke_coalesce.py',
    'smoke_term_intervals.py',
    'smoke_app_intervals.py',
    'smoke_ctx_persist.py',
    'smoke_mercari_capture.py',
    'smoke_stop_and_wall.py',
]


def _child_encoding():
    """
    The codec a child interpreter writes its piped stdout in. PYTHONIOENCODING
    is inherited, so when it is set it decides; otherwise a pipe gets the
    locale's preferred encoding (cp1252 on Windows, utf-8 in UTF-8 mode).
    """
    forced = (os.environ.get('PYTHONIOENCODING') or '').split(':')[0].strip()
    enc = forced or locale.getpreferredencoding(False)
    try:
        codecs.lookup(enc)
        return enc
    except LookupError:
        return 'utf-8'


def main():
    # Suite output is echoed from here, so this process must not be the one
    # that dies on a character its own console cannot encode.
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:
        pass
    enc = _child_encoding()
    results = []
    for name in SUITES:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            results.append((name, 'MISSING', 0, 0))
            continue
        print("\n" + "#" * 74)
        print("# %s" % name)
        print("#" * 74)
        # Bytes, decoded here, never text=True. text=True decodes strictly with
        # the locale codec, and on Windows it does so inside subprocess's reader
        # THREAD: under PYTHONIOENCODING=utf-8 (set by some terminals and agent
        # shells) smoke_scan_timing's ❌ arrived as UTF-8, byte 0x9D is undefined
        # in cp1252, the thread died, stdout came back None, and the suite was
        # scored PASS on its exit code alone, with no output and no count.
        # Measured 2026-10-13: 273 of 317 assertions reported, run "green".
        p = subprocess.run([sys.executable, path], capture_output=True)
        out = ((p.stdout or b'').decode(enc, 'replace') +
               (p.stderr or b'').decode(enc, 'replace'))
        sys.stdout.write(out)
        passed = failed = 0
        summary = False
        for line in out.splitlines():
            if ': PASSED ' in line and 'FAILED' in line:
                try:
                    parts = line.split()
                    passed = int(parts[parts.index('PASSED') + 1])
                    failed = int(parts[parts.index('FAILED') + 1])
                    summary = True
                except (ValueError, IndexError):
                    pass
        status = {0: 'PASS', 1: 'FAIL', 3: 'SKIP'}.get(p.returncode, 'ERROR(%d)' % p.returncode)
        if status == 'PASS' and not summary:
            # Exit 0 but no summary line: the suite never reached Suite.done(),
            # so nothing it ran was counted. That is not a pass.
            status = 'NO-SUMMARY'
        results.append((name, status, passed, failed))

    print("\n" + "=" * 74)
    print("SUMMARY")
    print("=" * 74)
    total_pass = total_fail = 0
    for name, status, passed, failed in results:
        total_pass += passed
        total_fail += failed
        detail = ("%d assertions" % passed) if passed else ''
        print("  %-10s %-28s %s" % (status, name, detail))
    print("-" * 74)
    print("  %d assertions passed, %d failed" % (total_pass, total_fail))
    bad = [r for r in results if r[1] not in ('PASS',)]
    if bad:
        print("\n  not green: %s" % ', '.join('%s (%s)' % (n, s) for n, s, _, _ in bad))
    print("=" * 74)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
