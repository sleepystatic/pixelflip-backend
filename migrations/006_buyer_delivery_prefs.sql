-- Optional delivery intent for UX + soft filtering heuristics in the scraper.
-- At least one of local / shipping should be true (enforced in app if columns default).

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS buyer_include_local BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS buyer_include_shipping BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN user_settings.buyer_include_local IS 'User wants listings that may be local pickup / meetup.';
COMMENT ON COLUMN user_settings.buyer_include_shipping IS 'User wants listings that may ship.';
