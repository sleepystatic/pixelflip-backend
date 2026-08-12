-- 011: Pro priority terms — 3 fast terms, the rest slow.
--
-- Pro users get 3 "priority" terms scanned at the 5-minute floor; their
-- remaining terms fall back to a 15-minute floor. That is both a product
-- feature and a bandwidth control: most terms drop from 288 scans/day to 96.
--
-- last_scraped_at moves to the TERM, not just the user. The scheduler used to
-- ask one question ("is this user due?"); it now has to ask it per term,
-- because a user can be due for their priority terms and not for the others.
-- The user-level user_settings.last_scraped_at stays as-is — it still drives
-- the dashboard countdown and the "burned interval" rollback.
--
-- NOTE: db_schema.py's ensure_priority_term_columns() is what actually runs in
-- production. This file is the readable record. Changing one without the other
-- means the change silently does not apply.

ALTER TABLE user_search_terms
  ADD COLUMN IF NOT EXISTS is_priority BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE user_search_terms
  ADD COLUMN IF NOT EXISTS last_scraped_at TIMESTAMPTZ;

-- Serves the scheduler's per-term due check.
CREATE INDEX IF NOT EXISTS idx_user_search_terms_user_priority
  ON user_search_terms (user_id, is_priority);
