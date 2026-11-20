-- 015: opt-in description scanning (Pro).
--
-- Keyword filters (per-term includes from 014, and exclusions) match the TITLE
-- only. Titles are short and sellers put the details a buyer actually filters on
-- — size, colour, storage, condition, faults — in the description. This lets a
-- user opt into matching both.
--
-- WHY IT IS OPT-IN AND PAID: descriptions are not in any platform's search
-- response, measured 2026-08-14.
--
--   Craigslist  free      the detail page is already fetched for photos + date
--   Mercari     one fetch the API returns `description` but empty on 8/8 items
--   OfferUp     one fetch not a field on ModularFeedListing at all
--   Facebook    one fetch feed carries only custom_sub_titles_with_rendering_flags
--
-- So enabling it costs one extra request per NEW listing on three of the four.
-- Only new listings pay — this sits after the known_links check — so steady
-- state is a handful per scan while a first scan is meaningfully slower. That
-- cost is the reason it is a Pro feature and the reason the UI warns before
-- turning it on.
--
-- Default FALSE so every existing user keeps exactly today's behaviour and
-- today's bandwidth.
--
-- NOTE: db_schema.py's ensure_scan_description_column() is what actually runs in
-- production. This file is the readable record. Changing one without the other
-- means the change silently does not apply.

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS scan_description BOOLEAN NOT NULL DEFAULT FALSE;
