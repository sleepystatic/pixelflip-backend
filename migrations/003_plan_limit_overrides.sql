-- Optional per-account limits (edit in Supabase SQL editor). NULL = use app defaults from plan_tier.

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS max_search_terms_override INTEGER,
  ADD COLUMN IF NOT EXISTS ai_image_allowed_override BOOLEAN;

COMMENT ON COLUMN user_settings.max_search_terms_override IS 'Optional max search terms; NULL = tier default (Basic 3, Pro 999)';
COMMENT ON COLUMN user_settings.ai_image_allowed_override IS 'Optional AI image toggle eligibility; NULL = tier default (Pro only)';
