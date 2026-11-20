-- PixelFlip base schema: a snapshot of PRODUCTION's public schema (structure only,
-- no rows). Generated 2026-11-13 20:50 UTC by tools/dump_schema.py from PostgreSQL 17.6.
--
-- USE: create a NEW, EMPTY Supabase project for development, open its SQL editor,
-- paste this whole file and run it. It is one transaction (all or nothing) and is
-- safe to re-run. Do not run it against production.
--
-- Extensions present in production (a new Supabase project already has these):
--   pg_stat_statements (extensions), pgcrypto (extensions), plpgsql (pg_catalog), supabase_vault (vault), uuid-ossp (extensions)
-- Grants are not dumped: Supabase's default privileges give new public objects
-- the same anon / authenticated / service_role grants production has.
--
-- Regenerate after any schema change:  .venv\Scripts\python.exe tools\dump_schema.py

BEGIN;

-- Sequences (serial columns)
CREATE SEQUENCE IF NOT EXISTS public.listings_feedback_id_seq AS bigint START 1 INCREMENT 1 MINVALUE 1 MAXVALUE 9223372036854775807 CACHE 1;
CREATE SEQUENCE IF NOT EXISTS public.listings_id_seq AS integer START 1 INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;
CREATE SEQUENCE IF NOT EXISTS public.scrape_logs_id_seq AS bigint START 1 INCREMENT 1 MINVALUE 1 MAXVALUE 9223372036854775807 CACHE 1;
CREATE SEQUENCE IF NOT EXISTS public.user_exclusions_id_seq AS integer START 1 INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;
CREATE SEQUENCE IF NOT EXISTS public.user_includes_id_seq AS integer START 1 INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;
CREATE SEQUENCE IF NOT EXISTS public.user_search_terms_id_seq AS integer START 1 INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;
CREATE SEQUENCE IF NOT EXISTS public.waitlist_id_seq AS integer START 1 INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

-- Tables, with their primary key / unique / check constraints
CREATE TABLE IF NOT EXISTS public.alerted_listings (
    user_id text NOT NULL,
    link text NOT NULL,
    title_fingerprint text,
    first_alerted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT alerted_listings_pkey PRIMARY KEY (user_id, link)
);
CREATE TABLE IF NOT EXISTS public.email_change_requests (
    user_id text NOT NULL,
    new_email text NOT NULL,
    code_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT email_change_requests_pkey PRIMARY KEY (user_id)
);
CREATE TABLE IF NOT EXISTS public.listings (
    id integer DEFAULT nextval('public.listings_id_seq'::regclass) NOT NULL,
    title text NOT NULL,
    price numeric(10,2),
    link text NOT NULL,
    platform text,
    console_type text,
    threshold numeric(10,2),
    created_at timestamp without time zone DEFAULT now(),
    user_id text,
    image_url text,
    location text,
    title_fingerprint text,
    listed_at timestamp with time zone,
    CONSTRAINT listings_pkey PRIMARY KEY (id),
    CONSTRAINT listings_user_link_unique UNIQUE (user_id, link)
);
CREATE TABLE IF NOT EXISTS public.listings_feedback (
    id bigint DEFAULT nextval('public.listings_feedback_id_seq'::regclass) NOT NULL,
    user_id text NOT NULL,
    link text NOT NULL,
    title text,
    price numeric,
    title_fingerprint text,
    reason text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT listings_feedback_pkey PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS public.password_reset_requests (
    email text NOT NULL,
    code_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT password_reset_requests_pkey PRIMARY KEY (email)
);
CREATE TABLE IF NOT EXISTS public.scrape_logs (
    id bigint DEFAULT nextval('public.scrape_logs_id_seq'::regclass) NOT NULL,
    user_id text NOT NULL,
    ts double precision NOT NULL,
    message text NOT NULL,
    log_type text DEFAULT 'info'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT scrape_logs_pkey PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS public.scraper_state (
    key text NOT NULL,
    value jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT scraper_state_pkey PRIMARY KEY (key)
);
CREATE TABLE IF NOT EXISTS public.user_exclusions (
    id integer DEFAULT nextval('public.user_exclusions_id_seq'::regclass) NOT NULL,
    user_id text NOT NULL,
    keyword text NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    search_term text,
    CONSTRAINT user_exclusions_pkey PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS public.user_includes (
    id integer DEFAULT nextval('public.user_includes_id_seq'::regclass) NOT NULL,
    user_id text NOT NULL,
    keyword text NOT NULL,
    search_term text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_includes_pkey PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS public.user_search_terms (
    id integer DEFAULT nextval('public.user_search_terms_id_seq'::regclass) NOT NULL,
    user_id text NOT NULL,
    search_term text NOT NULL,
    max_price numeric(10,2),
    min_price numeric(10,2),
    created_at timestamp without time zone DEFAULT now(),
    is_priority boolean DEFAULT false NOT NULL,
    last_scraped_at timestamp with time zone,
    interval_minutes integer DEFAULT 10 NOT NULL,
    CONSTRAINT user_search_terms_pkey PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS public.user_settings (
    user_id text NOT NULL,
    settings jsonb,
    updated_at timestamp without time zone DEFAULT now(),
    zip_code text,
    search_radius integer DEFAULT 25,
    platforms jsonb,
    ai_enabled boolean DEFAULT true,
    check_interval_minutes integer DEFAULT 10,
    ai_strictness text DEFAULT 'balanced'::text,
    subscription_status text DEFAULT 'inactive'::text,
    stripe_customer_id text,
    is_active boolean DEFAULT false,
    last_scraped_at timestamp with time zone,
    is_pro boolean DEFAULT false,
    stripe_subscription_id text,
    subscription_current_period_end bigint,
    subscription_cancel_at_period_end boolean DEFAULT false,
    last_scrape_duration_ms integer DEFAULT 0,
    plan_tier text,
    max_search_terms_override integer,
    ai_image_allowed_override boolean,
    notification_channels jsonb DEFAULT '{"sms": false, "push": false, "email": true}'::jsonb NOT NULL,
    contact_phone text,
    buyer_include_local boolean DEFAULT true NOT NULL,
    buyer_include_shipping boolean DEFAULT true NOT NULL,
    push_subscription jsonb,
    has_seen_tour jsonb DEFAULT '{}'::jsonb NOT NULL,
    scan_description boolean DEFAULT false NOT NULL,
    scrape_claimed_until timestamp with time zone,
    CONSTRAINT user_settings_pkey PRIMARY KEY (user_id)
);
CREATE TABLE IF NOT EXISTS public.waitlist (
    id integer DEFAULT nextval('public.waitlist_id_seq'::regclass) NOT NULL,
    email text NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    CONSTRAINT waitlist_pkey PRIMARY KEY (id),
    CONSTRAINT waitlist_email_key UNIQUE (email)
);

-- Serial sequences belong to their columns
ALTER SEQUENCE public.listings_feedback_id_seq OWNED BY public.listings_feedback.id;
ALTER SEQUENCE public.listings_id_seq OWNED BY public.listings.id;
ALTER SEQUENCE public.scrape_logs_id_seq OWNED BY public.scrape_logs.id;
ALTER SEQUENCE public.user_exclusions_id_seq OWNED BY public.user_exclusions.id;
ALTER SEQUENCE public.user_includes_id_seq OWNED BY public.user_includes.id;
ALTER SEQUENCE public.user_search_terms_id_seq OWNED BY public.user_search_terms.id;
ALTER SEQUENCE public.waitlist_id_seq OWNED BY public.waitlist.id;

-- Indexes (other than those behind constraints)
CREATE INDEX IF NOT EXISTS idx_alerted_user_time ON public.alerted_listings USING btree (user_id, first_alerted_at);
CREATE INDEX IF NOT EXISTS idx_feedback_user_fingerprint ON public.listings_feedback USING btree (user_id, title_fingerprint);
CREATE INDEX IF NOT EXISTS idx_listings_created_at ON public.listings USING btree (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_listings_link ON public.listings USING btree (link);
CREATE INDEX IF NOT EXISTS idx_listings_platform ON public.listings USING btree (platform);
CREATE INDEX IF NOT EXISTS idx_listings_user_created ON public.listings USING btree (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_listings_user_fingerprint ON public.listings USING btree (user_id, title_fingerprint);
CREATE INDEX IF NOT EXISTS idx_listings_user_id ON public.listings USING btree (user_id);
CREATE INDEX IF NOT EXISTS idx_scrape_logs_created ON public.scrape_logs USING btree (created_at);
CREATE INDEX IF NOT EXISTS idx_scrape_logs_user_ts ON public.scrape_logs USING btree (user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_user_exclusions_user_id ON public.user_exclusions USING btree (user_id);
CREATE INDEX IF NOT EXISTS idx_user_exclusions_user_term ON public.user_exclusions USING btree (user_id, search_term);
CREATE INDEX IF NOT EXISTS idx_user_includes_user_term ON public.user_includes USING btree (user_id, search_term);
CREATE INDEX IF NOT EXISTS idx_user_search_terms_user_id ON public.user_search_terms USING btree (user_id);
CREATE INDEX IF NOT EXISTS idx_user_search_terms_user_priority ON public.user_search_terms USING btree (user_id, is_priority);
CREATE INDEX IF NOT EXISTS idx_user_settings_push_not_null ON public.user_settings USING btree (push_subscription) WHERE (push_subscription IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_user_settings_scrape_claim ON public.user_settings USING btree (scrape_claimed_until);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_user_link_unique ON public.listings_feedback USING btree (user_id, link);

-- Row level security. Every table has it ON, with NO policies: the anon and
-- authenticated roles (the public Supabase keys) see nothing, while the backend
-- connects as the table owner and is unaffected.
ALTER TABLE public.alerted_listings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.email_change_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.listings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.listings_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.password_reset_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scrape_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scraper_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_exclusions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_includes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_search_terms ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.waitlist ENABLE ROW LEVEL SECURITY;

-- Functions
CREATE OR REPLACE FUNCTION public.handle_new_user()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
  INSERT INTO public.user_settings (
    user_id, 
    zip_code, 
    search_radius, 
    check_interval_minutes, 
    is_active, 
    is_pro, 
    subscription_status
  )
  VALUES (
    new.id, 
    '95210', -- Default Stockton Zip
    25,      -- Default 25 miles
    10,      -- Default 10 mins
    FALSE,   -- Scraper starts OFF
    FALSE,   -- User starts as FREE/LOCKED
    'inactive' -- Payment status starts as INACTIVE
  );
  RETURN new;
END;
$function$;

CREATE OR REPLACE FUNCTION public.rls_auto_enable()
 RETURNS event_trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        EXECUTE format('alter table if exists %s enable row level security', cmd.object_identity);
        RAISE LOG 'rls_auto_enable: enabled RLS on %', cmd.object_identity;
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     ELSE
        RAISE LOG 'rls_auto_enable: skip % (either system schema or not in enforced list: %.)', cmd.object_identity, cmd.schema_name;
     END IF;
  END LOOP;
END;
$function$;

-- Views. KEEP security_invoker: without it a Supabase view runs as its owner,
-- bypasses RLS, and anyone holding the public anon key can read it.
CREATE OR REPLACE VIEW public.user_dashboard_admin WITH (security_invoker=on) AS
 SELECT au.email,
    us.is_pro,
    us.is_active,
    us.zip_code,
    us.last_scraped_at,
    us.user_id
   FROM auth.users au
     JOIN public.user_settings us ON au.id = us.user_id::uuid
  ORDER BY us.last_scraped_at DESC NULLS LAST;

-- Triggers (incl. the signup trigger on auth.users)
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- Custom event triggers (Supabase's own built-in ones exist in every project)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'ensure_rls') THEN
    CREATE EVENT TRIGGER ensure_rls ON ddl_command_end WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO') EXECUTE FUNCTION public.rls_auto_enable();
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'ensure_rls not created; every table above already has RLS on, and new tables need it enabled by hand (or turn on automatic RLS in the dashboard) (%)', SQLERRM;
END
$$;

COMMIT;
