-- 017: make existing entitlements EXPLICIT, then stop guessing generously.
--
-- THE PROBLEM
-- Tier was resolved in three places with the same shape:
--
--     plan_tier if it is 'basic'/'pro', else 'pro' if is_pro else 'inactive'
--
-- so a row with is_pro = TRUE and NO plan_tier silently resolved to PRO. Stripe
-- does write plan_tier correctly, but any other path that sets is_pro — a
-- manual grant, a pre-beta flag, a restored backup, a hand-edited row — gave
-- away the most expensive tier. For a paid product the ambiguous case should
-- cost a support ticket, not the product.
--
-- As of 2026-10-13 every user in production was exactly that shape:
-- plan_tier NULL, is_pro TRUE. They have been treated as Pro all along.
--
-- THE FIX, IN TWO HALVES
--   1. (this file) Write down what those users already have, so the fallback
--      stops being load-bearing for anyone who exists today.
--   2. (code) Change the fallback from 'pro' to 'basic' in all three places.
--      'basic' rather than 'inactive' because is_pro = TRUE means they paid for
--      SOMETHING; cutting a paying customer off entirely is the opposite
--      mistake.
--
-- RUN ONCE. DO NOT PUT THIS IN AN ensure CHAIN.
-- An ensure runs on every boot, and this statement would then re-promote any
-- future is_pro-without-plan_tier row to Pro on the next restart — reinstating
-- the exact bug it exists to close. It is a one-time reconciliation of legacy
-- rows, not an invariant.
UPDATE user_settings
   SET plan_tier = 'pro'
 WHERE plan_tier IS NULL
   AND is_pro = TRUE;
