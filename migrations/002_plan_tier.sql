-- Plan tier (Basic vs Pro) for feature limits. Run once on Supabase.
-- Stripe webhooks / checkout should set plan_tier from price id.

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS plan_tier TEXT;

COMMENT ON COLUMN user_settings.plan_tier IS 'Subscription tier: basic | pro | inactive (NULL treated via legacy is_pro in app)';
