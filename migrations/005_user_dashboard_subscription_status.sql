CREATE OR REPLACE VIEW public.user_dashboard_admin AS
SELECT
  au.id::text AS user_id,
  au.email AS email,
  COALESCE(us.subscription_status, 'inactive') AS subscription_status,
  COALESCE(us.plan_tier::text, 'inactive') AS plan_tier
FROM auth.users au
LEFT JOIN public.user_settings us ON us.user_id = au.id::text;
