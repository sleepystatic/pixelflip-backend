"""Lightweight schema helpers (idempotent DDL for local / prod without a migration runner)."""
import threading

_buyer_prefs_lock = threading.Lock()
_buyer_prefs_applied = False

_push_lock = threading.Lock()
_push_applied = False

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
