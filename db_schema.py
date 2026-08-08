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
