-- 016: cross-instance scrape leases.
--
-- WHY
-- Until now the only guard against two scrapes of the same user at once was
-- SCRAPING_USERS — a dict in the process, protected by a threading.Lock. That
-- works perfectly within one process and not at all between two. worker.py's
-- docstring says "RUN EXACTLY ONE" for precisely this reason:
--
--   two workers  ->  both pick the same due user
--                ->  both scrape every marketplace for them
--                ->  both stamp last_scraped_at
--                ->  the user's interval is burned twice and they get a
--                    duplicate alert digest
--
-- which is the "ghost scrape" failure from the single-process days, promoted to
-- infrastructure level. It is also the single thing that makes the scraper
-- impossible to scale horizontally, so it caps throughput at whatever ONE box
-- can do no matter how many users there are.
--
-- HOW
-- A lease, not a lock. `scrape_claimed_until` is a timestamp in the future that
-- one instance writes atomically; anyone else reading a live claim skips that
-- user. The claim is taken with a single conditional UPDATE, so there is no
-- read-then-write window for two instances to both pass.
--
-- IT MUST EXPIRE. A worker that is OOM-killed mid-scrape never runs its release,
-- and a plain boolean "is_scraping" flag would strand that user forever — the
-- exact bug is_user_scraping() already had to grow a timeout for in-process. A
-- timestamp self-heals: once it passes, the next cycle claims normally.
--
-- The lease is set well beyond the worst observed scrape (184-247s measured in
-- production) so a slow-but-healthy run is never stolen mid-flight.
ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS scrape_claimed_until TIMESTAMPTZ;

-- The scheduler asks "who is due AND unclaimed" every cycle.
CREATE INDEX IF NOT EXISTS idx_user_settings_scrape_claim
  ON user_settings (scrape_claimed_until);
