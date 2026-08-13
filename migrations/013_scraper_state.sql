-- 013: shared scraper state, so credentials outlive a single process.
--
-- WHY: Mercari's search API needs an `authorization: Bearer <jwt>` that can only
-- be obtained by loading a real page and reading the header off the request the
-- app's JavaScript sends. That capture is the single most expensive thing the
-- scraper does — measured 2026-08-13 at 1,071MB peak across 11 Chrome processes,
-- against a 512MB Render instance.
--
-- The token itself is cheap to reuse: decoded, it is a JWT with a 10,080-minute
-- (7 day) `exp`, and measurement showed the replay needs NOTHING else except
-- Chrome's TLS fingerprint — not cf_clearance, not any cookie at all, and it
-- works from an IP different to the one that captured it.
--
-- Keeping it in a process global meant every restart paid a fresh capture.
-- Keeping it in a temp FILE was better but still per-instance: Render instances
-- do not share a disk, so each one captures its own. In the database it is
-- captured once and read by everything — including a capture run from a machine
-- that is NOT the web host, which is the point. A box too small to launch Chrome
-- can still scrape Mercari all week off a token minted elsewhere.
--
-- Deliberately a generic key/value table rather than mercari-specific columns.
-- The next credential of this shape (a rotating OfferUp token, a Facebook
-- session) should not need another migration.
--
-- NOTE: db_schema.py's ensure_scraper_state_table() is what actually runs in
-- production. This file is the readable record. Changing one without the other
-- means the change silently does not apply.

CREATE TABLE IF NOT EXISTS scraper_state (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
