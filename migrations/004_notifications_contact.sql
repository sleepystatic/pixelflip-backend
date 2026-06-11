-- Notification channel preferences + optional SMS phone (E.164 or formatted digits).
-- Apply in Supabase SQL editor or psql against the same DB as DATABASE_URL.

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS notification_channels JSONB
    NOT NULL DEFAULT '{"email": true, "sms": false, "push": false}'::jsonb,
  ADD COLUMN IF NOT EXISTS contact_phone TEXT;

COMMENT ON COLUMN user_settings.notification_channels IS
  'JSON: {"email":bool,"sms":bool,"push":bool} — channels to use when alerting on new listings.';
COMMENT ON COLUMN user_settings.contact_phone IS
  'Optional phone for SMS alerts; keep consent/terms in product copy.';
