-- 010: listings uniqueness must be per-user, not global.
--
-- The table carried BOTH listings_link_key UNIQUE(link) and
-- listings_user_link_unique UNIQUE(user_id, link). save_listing used
-- ON CONFLICT (link), which binds to the global index — so the first user to
-- save a marketplace link made that link permanently unsavable by every other
-- user. The second user got no row and, because new_listings drives the digest,
-- no alert either. Orphaned rows from deleted accounts squatted on links too.
--
-- Paired with scraper_multi_user.save_listing switching to
-- ON CONFLICT (user_id, link). Drop one without the other and inserts break:
-- ON CONFLICT needs a unique index matching its target.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'listings_link_key' AND conrelid = 'public.listings'::regclass
  ) THEN
    ALTER TABLE public.listings DROP CONSTRAINT listings_link_key;
  ELSIF EXISTS (
    SELECT 1 FROM pg_class WHERE relname = 'listings_link_key' AND relkind = 'i'
  ) THEN
    DROP INDEX public.listings_link_key;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS listings_user_link_unique
  ON public.listings (user_id, link);
