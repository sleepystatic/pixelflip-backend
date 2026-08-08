-- Guided tour progress.
--
-- JSONB rather than a boolean so new tour sections can be added later and
-- shown only to users who haven't seen that specific section. A boolean would
-- mean either replaying the whole tour or never showing new content.
--   {"intro": true, "first_scan": false}
ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS has_seen_tour JSONB NOT NULL DEFAULT '{}'::jsonb;
