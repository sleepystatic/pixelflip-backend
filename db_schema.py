"""Lightweight schema helpers (idempotent DDL for local / prod without a migration runner)."""
import threading

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
