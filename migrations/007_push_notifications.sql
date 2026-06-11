-- Migration 007: Web Push Notifications
-- Adds push subscription storage for users

ALTER TABLE user_settings
ADD COLUMN IF NOT EXISTS push_subscription JSONB DEFAULT NULL;

-- Index for quick lookup when sending notifications
CREATE INDEX IF NOT EXISTS idx_user_settings_push_not_null
ON user_settings(push_subscription)
WHERE push_subscription IS NOT NULL;
