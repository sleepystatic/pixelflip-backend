"""Lightweight schema helpers (idempotent DDL for local / prod without a migration runner)."""
import os
import threading


def schema_ensure_enabled():
    """
    False when SCHEMA_ENSURE=0. The test harness sets that, because one suite
    (smoke_scrape_lease) talks to the REAL database through the scraper's own
    get_db_connection — and on 2026-11-20 that applied a brand-new migration
    (018) to production before the code using it was deployed. Production and
    `python app.py` leave it unset, so migrations still apply on first use.
    """
    return (os.getenv('SCHEMA_ENSURE') or '1').strip().lower() not in ('0', 'false', 'no', 'off')

_buyer_prefs_lock = threading.Lock()
_buyer_prefs_applied = False

_push_lock = threading.Lock()
_push_applied = False

_term_excl_lock = threading.Lock()
_term_excl_applied = False

_tour_lock = threading.Lock()
_tour_applied = False

_listing_uniq_lock = threading.Lock()
_listing_uniq_applied = False

_priority_lock = threading.Lock()
_priority_applied = False

_term_interval_lock = threading.Lock()
_term_interval_applied = False

_scraper_state_lock = threading.Lock()
_scraper_state_applied = False

_term_include_lock = threading.Lock()
_term_include_applied = False

_scan_desc_lock = threading.Lock()
_scan_desc_applied = False
_scrape_lease_lock = threading.Lock()
_scrape_lease_applied = False
_alerted_lock = threading.Lock()
_alerted_applied = False

BUYER_DELIVERY_PREFS_DDL = """
ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS buyer_include_local BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS buyer_include_shipping BOOLEAN NOT NULL DEFAULT TRUE;
"""

PUSH_SUBSCRIPTION_DDL = """
ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS push_subscription JSONB DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_user_settings_push_not_null
ON user_settings(push_subscription)
WHERE push_subscription IS NOT NULL;
"""


PER_TERM_EXCLUSIONS_DDL = """
ALTER TABLE user_exclusions
  ADD COLUMN IF NOT EXISTS search_term TEXT;

CREATE INDEX IF NOT EXISTS idx_user_exclusions_user_term
ON user_exclusions (user_id, search_term);

ALTER TABLE user_search_terms ALTER COLUMN max_price DROP NOT NULL;
ALTER TABLE user_search_terms ALTER COLUMN min_price DROP NOT NULL;
"""


def ensure_per_term_exclusion_columns(conn):
    """Add per-term exclusions + nullable prices if migration 008 was not run yet."""
    global _term_excl_applied
    if _term_excl_applied:
        return
    with _term_excl_lock:
        if _term_excl_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(PER_TERM_EXCLUSIONS_DDL)
            conn.commit()
            _term_excl_applied = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


TOUR_PROGRESS_DDL = """
ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS has_seen_tour JSONB NOT NULL DEFAULT '{}'::jsonb;
"""


def ensure_tour_column(conn):
    """Add has_seen_tour if migration 009 was not run yet."""
    global _tour_applied
    if _tour_applied:
        return
    with _tour_lock:
        if _tour_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(TOUR_PROGRESS_DDL)
            conn.commit()
            _tour_applied = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


LISTING_UNIQUE_DDL = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'listings_link_key' AND conrelid = 'public.listings'::regclass
  ) THEN
    ALTER TABLE public.listings DROP CONSTRAINT listings_link_key;
  ELSIF EXISTS (
    SELECT 1 FROM pg_class WHERE relname = 'listings_link_key' AND relkind = 'i'
  ) THEN
    DROP INDEX public.listings_link_key;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS listings_user_link_unique
  ON public.listings (user_id, link);
"""


def ensure_listing_uniqueness_per_user(conn):
    """
    Apply migration 010 if it was not run yet.

    listings carried a global UNIQUE(link) as well as UNIQUE(user_id, link).
    save_listing's ON CONFLICT bound to the global one, so the first user to
    save a link made it unsavable for everyone else — no row and no alert.
    Must stay in sync with save_listing's ON CONFLICT (user_id, link) target:
    drop one without the other and inserts start raising.
    """
    global _listing_uniq_applied
    if _listing_uniq_applied:
        return
    with _listing_uniq_lock:
        if _listing_uniq_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(LISTING_UNIQUE_DDL)
            conn.commit()
            _listing_uniq_applied = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


PRIORITY_TERMS_DDL = """
ALTER TABLE user_search_terms
  ADD COLUMN IF NOT EXISTS is_priority BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE user_search_terms
  ADD COLUMN IF NOT EXISTS last_scraped_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_user_search_terms_user_priority
  ON user_search_terms (user_id, is_priority);
"""


def ensure_priority_term_columns(conn):
    """
    Apply migration 011 if it was not run yet.

    is_priority marks the (max 3) terms a Pro user wants at the 5-minute floor;
    everything else falls to the 15-minute floor. last_scraped_at is per TERM
    because the scheduler now has to decide due-ness per term rather than per
    user — see _due_terms_for_user in scraper_multi_user.py.
    """
    global _priority_applied
    if _priority_applied:
        return
    with _priority_lock:
        if _priority_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(PRIORITY_TERMS_DDL)
            conn.commit()
            _priority_applied = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


TERM_INTERVAL_DDL = """
ALTER TABLE user_search_terms
  ADD COLUMN IF NOT EXISTS interval_minutes INT NOT NULL DEFAULT 10;
"""


def ensure_term_interval_column(conn):
    """
    Apply migration 012 if it was not run yet.

    Scan cadence is per TERM now, not per user: interval_minutes replaces the
    combination of user_settings.check_interval_minutes and the is_priority
    boolean. ADD COLUMN ... DEFAULT 10 backfills existing rows to 10, which is
    what we want — there are no production users whose cadence needs preserving.

    is_priority stays, derived as (interval_minutes == 5), so a rollback or a
    half-deployed frontend degrades instead of breaking.

    Deliberately DDL-only. Everything in here runs on every boot, so a data
    UPDATE would re-apply on each deploy and reset every user's choices.
    """
    global _term_interval_applied
    if _term_interval_applied:
        return
    with _term_interval_lock:
        if _term_interval_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(TERM_INTERVAL_DDL)
            conn.commit()
            _term_interval_applied = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


SCRAPE_LEASE_DDL = """
ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS scrape_claimed_until TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_user_settings_scrape_claim
  ON user_settings (scrape_claimed_until);
"""


def ensure_scrape_lease_column(conn):
    """
    Apply migration 016 if it was not run yet.

    The lease that lets more than one scraper process exist. Without it two
    workers both claim the same due user, both scrape, and both stamp
    last_scraped_at — burning the user's interval twice and sending a duplicate
    digest.
    """
    global _scrape_lease_applied
    if _scrape_lease_applied:
        return
    with _scrape_lease_lock:
        if _scrape_lease_applied:
            return
        # Split deliberately. Sent as one statement these exceeded Supabase's
        # statement_timeout, and because psycopg2 wraps the batch in a single
        # transaction the rollback took the ALTER with it — so the column that
        # IS required was lost to an index that merely helps.
        cur = conn.cursor()
        try:
            cur.execute(
                "ALTER TABLE user_settings "
                "ADD COLUMN IF NOT EXISTS scrape_claimed_until TIMESTAMPTZ;"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

        # Best effort. The scheduler works without it; it only keeps the
        # due-and-unclaimed lookup cheap as the user count grows.
        cur = conn.cursor()
        try:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_settings_scrape_claim "
                "ON user_settings (scrape_claimed_until);"
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[schema] scrape-claim index skipped: {e}", flush=True)
        finally:
            cur.close()

        _scrape_lease_applied = True


def ensure_alerted_listings_table(conn):
    """
    The persistent 'already alerted' log (dedup across scrapes).

    WHY THIS EXISTS, SEPARATELY FROM `listings`
    A match counts as "new" only when its (user_id, link) is not already in
    `listings` — and the cleanup thread deletes from `listings` after 7 days to
    keep it small. But a Facebook listing can stay live for up to 45 days, and an
    OfferUp listing has no reliable end date. So a listing still for sale when its
    `listings` row is purged re-inserts as "new" on the next scrape and the user
    is alerted about it AGAIN — once a week, for as long as it stays up.

    `alerted_listings` records what a user has ALREADY been told about and is kept
    far longer than `listings` (ALERTED_RETENTION_DAYS, default 60 — longer than
    Facebook's 45-day max), so the re-alert cannot happen. It is deliberately not
    the same table: `listings` is "what to show in the app right now" and is sized
    for that; this is "what we have notified", and the two have different lifetimes.
    Keyed on (user_id, link); `first_alerted_at` drives retention.
    """
    global _alerted_applied
    if _alerted_applied:
        return
    with _alerted_lock:
        if _alerted_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alerted_listings (
                  user_id          TEXT NOT NULL,
                  link             TEXT NOT NULL,
                  title_fingerprint TEXT,
                  first_alerted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  PRIMARY KEY (user_id, link)
                );
                """
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

        # Best effort: keeps the per-user retention lookup cheap as the table grows.
        cur = conn.cursor()
        try:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerted_user_time "
                "ON alerted_listings (user_id, first_alerted_at);"
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[schema] alerted_listings index skipped: {e}", flush=True)
        finally:
            cur.close()

        _alerted_applied = True


SCAN_DESCRIPTION_DDL = """
ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS scan_description BOOLEAN NOT NULL DEFAULT FALSE;
"""


def ensure_scan_description_column(conn):
    """
    Apply migration 015 if it was not run yet.

    Opt-in, Pro-only description matching. Default FALSE so existing users keep
    today's behaviour and today's bandwidth — enabling it costs one extra fetch
    per NEW listing on Mercari, OfferUp and Facebook (Craigslist is free, its
    detail page is already being fetched).
    """
    global _scan_desc_applied
    if _scan_desc_applied:
        return
    with _scan_desc_lock:
        if _scan_desc_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(SCAN_DESCRIPTION_DDL)
            conn.commit()
            _scan_desc_applied = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


TERM_INCLUDES_DDL = """
CREATE TABLE IF NOT EXISTS user_includes (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    search_term TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_includes_user_term
  ON user_includes (user_id, search_term);
"""


def ensure_term_include_table(conn):
    """
    Apply migration 014 if it was not run yet.

    Per-term include keywords: a listing survives if it matches ANY of them (OR),
    while exclusions stay AND-NOT and outrank them. No includes on a term means
    everything passes, so this is additive and cannot change existing behaviour.
    """
    global _term_include_applied
    if _term_include_applied:
        return
    with _term_include_lock:
        if _term_include_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(TERM_INCLUDES_DDL)
            conn.commit()
            _term_include_applied = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


SCRAPER_STATE_DDL = """
CREATE TABLE IF NOT EXISTS scraper_state (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def ensure_scraper_state_table(conn):
    """
    Apply migration 013 if it was not run yet.

    Shared key/value state for credentials that outlive one process. Currently
    Mercari's captured bearer, which is expensive to mint (a ~1GB browser
    capture) and cheap to reuse (a 7-day JWT that needs no cookies and is not
    IP-bound). In the database rather than a file because Render instances do
    not share a disk — and because it lets the capture run somewhere else
    entirely, on a machine with room for Chrome.
    """
    global _scraper_state_applied
    if _scraper_state_applied:
        return
    with _scraper_state_lock:
        if _scraper_state_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(SCRAPER_STATE_DDL)
            conn.commit()
            _scraper_state_applied = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def ensure_buyer_delivery_columns(conn):
    """Add buyer_include_* columns if migration 006 was not run yet."""
    global _buyer_prefs_applied
    if _buyer_prefs_applied:
        return
    with _buyer_prefs_lock:
        if _buyer_prefs_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(BUYER_DELIVERY_PREFS_DDL)
            conn.commit()
            _buyer_prefs_applied = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def ensure_push_subscription_column(conn):
    """Add push_subscription column if migration 007 was not run yet."""
    global _push_applied
    if _push_applied:
        return
    with _push_lock:
        if _push_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(PUSH_SUBSCRIPTION_DDL)
            conn.commit()
            _push_applied = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


_trial_lock = threading.Lock()
_trial_applied = False

# Migration 018 (2026-11-20): free trial + repeat-trial guard (trial_guard.py).
# trial_claims holds one row per completed checkout with KEYED HASHES of the
# signals that identify a person (email, IP, device, billing address) plus
# Stripe's own card fingerprint — never the raw values.
TRIAL_TRACKING_DDL = """
ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS trial_used_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS trial_outcome TEXT,
  ADD COLUMN IF NOT EXISTS subscription_trial_end BIGINT;

CREATE TABLE IF NOT EXISTS trial_claims (
    id               BIGSERIAL PRIMARY KEY,
    user_id          TEXT NOT NULL,
    subscription_id  TEXT,
    checkout_session TEXT,
    email_hash       TEXT,
    ip_hash          TEXT,
    device_hash      TEXT,
    card_fingerprint TEXT,
    address_hash     TEXT,
    outcome          TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trial_claims_subscription ON trial_claims (subscription_id);
CREATE INDEX IF NOT EXISTS idx_trial_claims_email   ON trial_claims (email_hash);
CREATE INDEX IF NOT EXISTS idx_trial_claims_ip      ON trial_claims (ip_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_trial_claims_device  ON trial_claims (device_hash);
CREATE INDEX IF NOT EXISTS idx_trial_claims_card    ON trial_claims (card_fingerprint);
CREATE INDEX IF NOT EXISTS idx_trial_claims_address ON trial_claims (address_hash);
-- Hashes only, but still nobody's business: no access through the public keys.
ALTER TABLE trial_claims ENABLE ROW LEVEL SECURITY;
"""


def ensure_trial_tracking(conn):
    """Apply migration 018 if it was not run yet (free trial + repeat-trial guard)."""
    global _trial_applied
    if _trial_applied:
        return
    with _trial_lock:
        if _trial_applied:
            return
        cur = conn.cursor()
        try:
            cur.execute(TRIAL_TRACKING_DDL)
            conn.commit()
            _trial_applied = True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
