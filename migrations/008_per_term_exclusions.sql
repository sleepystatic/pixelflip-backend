-- Per-search-term exclusions + optional price bounds.
--
-- Exclusions used to be global: one keyword list applied to every search term,
-- which is too blunt. "case" should be excludable from "gameboy advance sp"
-- without also filtering it out of a search for phone cases.
--
-- search_term NULL keeps the old meaning (applies to every term), so existing
-- rows keep working untouched and users don't lose their filters.
ALTER TABLE user_exclusions
    ADD COLUMN IF NOT EXISTS search_term TEXT;

CREATE INDEX IF NOT EXISTS idx_user_exclusions_user_term
    ON user_exclusions (user_id, search_term);

-- Price bounds become optional. NULL max = "any price", so a user can track a
-- term without having to invent an upper bound just to save it.
ALTER TABLE user_search_terms
    ALTER COLUMN max_price DROP NOT NULL;

ALTER TABLE user_search_terms
    ALTER COLUMN min_price DROP NOT NULL;
