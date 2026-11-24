-- Migration 018 (2026-11-20): free trial + repeat-trial guard. Applied automatically by
-- db_schema.ensure_trial_tracking on first use; this file is the readable record.

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS trial_used_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS trial_outcome TEXT,
  ADD COLUMN IF NOT EXISTS subscription_trial_end BIGINT;

CREATE TABLE IF NOT EXISTS trial_claims (
    id               BIGSERIAL PRIMARY KEY,
    user_id          TEXT NOT NULL,
    subscription_id  TEXT,
    checkout_session TEXT,
    email_hash       TEXT,
    ip_hash          TEXT,
    device_hash      TEXT,
    card_fingerprint TEXT,
    address_hash     TEXT,
    outcome          TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trial_claims_subscription ON trial_claims (subscription_id);
CREATE INDEX IF NOT EXISTS idx_trial_claims_email   ON trial_claims (email_hash);
CREATE INDEX IF NOT EXISTS idx_trial_claims_ip      ON trial_claims (ip_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_trial_claims_device  ON trial_claims (device_hash);
CREATE INDEX IF NOT EXISTS idx_trial_claims_card    ON trial_claims (card_fingerprint);
CREATE INDEX IF NOT EXISTS idx_trial_claims_address ON trial_claims (address_hash);
-- Hashes only, but still nobody's business: no access through the public keys.
ALTER TABLE trial_claims ENABLE ROW LEVEL SECURITY;
