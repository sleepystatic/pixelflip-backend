-- 014: per-term INCLUDE keywords, the positive counterpart to user_exclusions.
--
-- WHY: the search term is the QUERY sent to each marketplace; includes are a
-- filter on what comes back. Those are different things, and some filters cannot
-- be expressed as a query at all. "iphone xr" with includes blue/red is one
-- search whose results are then narrowed; expressing it as search terms would
-- mean running two separate searches and paying twice.
--
-- SEMANTICS (deliberately different from exclusions):
--   * includes are OR — a listing survives if it contains ANY include keyword.
--     That is what makes 'size 6' / 'size 7' and 'blue' / 'red' behave the way a
--     user expects. AND would mean a shoe had to be both sizes at once.
--   * exclusions remain AND-NOT and OUTRANK includes: an excluded word rejects
--     even when an include matched. The narrower, more explicit rule wins.
--   * no includes on a term means everything passes, so this is purely additive
--     and existing terms behave exactly as before.
--
-- Matching is TITLE-ONLY for now, at Bryan's instruction. Descriptions are
-- deliberately not consulted and no scaffolding for them exists here: we only
-- have descriptions on some platforms, so including them would make the same
-- keyword behave differently per marketplace.
--
-- A separate table rather than a `kind` column on user_exclusions: a table
-- called "exclusions" holding inclusions is the kind of thing that reads fine
-- the day it is written and misleads everyone afterwards.
--
-- NOTE: db_schema.py's ensure_term_include_table() is what actually runs in
-- production. This file is the readable record. Changing one without the other
-- means the change silently does not apply.

CREATE TABLE IF NOT EXISTS user_includes (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    search_term TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Serves the only read: every include for one user, grouped by term.
CREATE INDEX IF NOT EXISTS idx_user_includes_user_term
  ON user_includes (user_id, search_term);
