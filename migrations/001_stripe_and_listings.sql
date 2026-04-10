-- Run once on your Supabase/Postgres DB (SQL editor or psql).
-- Stripe linkage + listing feed fields + cleanup/feedback support.

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT,
  ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT,
  ADD COLUMN IF NOT EXISTS subscription_current_period_end BIGINT,
  ADD COLUMN IF NOT EXISTS subscription_cancel_at_period_end BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS last_scrape_duration_ms INTEGER DEFAULT 0;

ALTER TABLE user_settings
  ALTER COLUMN subscription_cancel_at_period_end SET DEFAULT FALSE;

ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS image_url TEXT,
  ADD COLUMN IF NOT EXISTS location TEXT,
  ADD COLUMN IF NOT EXISTS title_fingerprint TEXT;

CREATE INDEX IF NOT EXISTS idx_listings_user_created ON listings (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_listings_user_fingerprint ON listings (user_id, title_fingerprint);

CREATE TABLE IF NOT EXISTS listings_feedback (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  link TEXT NOT NULL,
  title TEXT,
  price NUMERIC,
  title_fingerprint TEXT,
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_user_link_unique
  ON listings_feedback (user_id, link);
CREATE INDEX IF NOT EXISTS idx_feedback_user_fingerprint
  ON listings_feedback (user_id, title_fingerprint);
