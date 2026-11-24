# -*- coding: utf-8 -*-
"""
Tiny shared harness for the smoke suites.

Plain scripts rather than pytest, because pytest is not a dependency of this
project and adding one to run six files is not a trade worth making. Each suite
is runnable on its own (`.venv\\Scripts\\python.exe tests\\smoke_x.py`) and
exits non-zero on failure, so run_all.py and any CI both work without a plugin.

WHY THESE LIVE IN THE REPO NOW
They used to live in a session scratchpad, which is wiped between sessions. They
were rebuilt from scratch twice, and their HTML fixtures had to be re-fetched
from live marketplaces each time — traffic spent re-proving things that were
already proven. Fixtures are committed for the same reason: a test that needs
Facebook to be up is not a test you can run.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')

if REPO not in sys.path:
    sys.path.insert(0, REPO)


def bootstrap():
    """
    Make the scraper importable and its env loaded, from any working directory.

    Threads OFF before app.py can be imported by anything downstream: its
    cleanup thread deletes old production listings on a timer, which a test run
    must never trigger.
    """
    os.environ.setdefault('ENABLE_SCRAPER_THREAD', '0')
    os.environ['ENABLE_CLEANUP_THREAD'] = '0'
    # Never run migrations from a test. The local .env points at PRODUCTION, and
    # smoke_scrape_lease uses the real database: on 2026-11-20 the ensure chain
    # applied migration 018 there before the code that uses it had shipped.
    os.environ['SCHEMA_ENSURE'] = '0'
    # Never ask OfferUp for a listing's details from a test. Since 2026-11-23 an
    # OfferUp scan with description keywords looks listings up LIVE; the suites
    # that test the lookup switch it back on over a fake session.
    os.environ['OFFERUP_LISTING_API'] = '0'
    os.chdir(REPO)
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, '.env'))
    except Exception:
        pass


class Suite(object):
    """Counts assertions, prints failures as they happen, exits with a status."""

    def __init__(self, name):
        self.name = name
        self.passed = 0
        self.failures = []

    def section(self, title):
        print("\n" + "=" * 74)
        print(title)
        print("=" * 74)

    def ok(self, cond, label):
        if cond:
            self.passed += 1
        else:
            self.failures.append(label)
            print("  FAIL: %s" % label)

    def eq(self, got, want, label):
        self.ok(got == want, "%s (got %r, want %r)" % (label, got, want))

    def skip(self, why):
        """A missing fixture is a skip, never a silent pass."""
        print("\nSKIPPED: %s" % why)
        print("  refresh fixtures with: .venv\\Scripts\\python.exe tests\\refresh_fixtures.py")
        sys.exit(3)

    def done(self):
        print("\n" + "=" * 74)
        print("%s: PASSED %d   FAILED %d" % (self.name, self.passed, len(self.failures)))
        for f in self.failures:
            print("   - %s" % f)
        print("=" * 74)
        sys.exit(1 if self.failures else 0)


def fixture(name):
    """Read a committed fixture, or None when it is absent."""
    path = os.path.join(FIXTURES, name)
    if not os.path.exists(path):
        return None
    import io
    return io.open(path, encoding='utf-8').read()


def fixture_glob(pattern):
    import glob
    return sorted(glob.glob(os.path.join(FIXTURES, pattern)))


def body_only_word(title, body, min_len=4):
    """
    A word that appears in `body` but NOT in `title`.

    The description tests all hinge on a keyword the title cannot satisfy: that
    is what makes "title alone would drop this, the body rescues it" a real
    assertion. Taking simply the first long word from the body is not enough —
    real listings repeat their title in the body ("Nintendo DS Lite Coral Pink"
    under a title containing "Nintendo"), and the test then fails for a reason
    that has nothing to do with the code under test.

    Returns None when the body shares every word with the title, which callers
    should treat as "this fixture cannot exercise the seam".
    """
    import re
    t = (title or '').lower()
    for w in re.findall(r'[a-z]{%d,}' % min_len, (body or '').lower()):
        if w not in t:
            return w
    return None
